"""Unit tests for claude_code_modal.py.

The `modal` package is replaced by a lightweight stub (see the repo-root
conftest.py) that records Volume/Secret creation and captures the kwargs handed
to modal.Sandbox.create — so these tests exercise the real control flow of
main() without touching Modal infrastructure.
"""
import modal  # the stub installed by conftest.py
import claude_code_modal as ccm


class TestVolumeSlug:
    def test_lowercases_and_dashes_slashes(self):
        assert ccm.volume_slug("ThatE10/rspd") == "thate10-rspd"

    def test_collapses_and_trims(self):
        assert ccm.volume_slug("  My  Box!! ") == "my-box"

    def test_none_and_blank_fall_back_to_default(self):
        assert ccm.volume_slug(None) == "default"
        assert ccm.volume_slug("   ") == "default"
        assert ccm.volume_slug("///") == "default"

    def test_preserves_existing_dashes(self):
        assert ccm.volume_slug("rspd-pre") == "rspd-pre"


class TestResolveSSHKey:
    def test_raw_text_passthrough(self):
        assert ccm.resolve_ssh_public_key("ssh-ed25519 AAAA") == "ssh-ed25519 AAAA"

    def test_reads_from_path(self, tmp_path):
        p = tmp_path / "id.pub"
        p.write_text("ssh-rsa KEYDATA\n")
        assert ccm.resolve_ssh_public_key(str(p)) == "ssh-rsa KEYDATA"

    def test_autodetect_from_home(self, tmp_path, monkeypatch):
        ssh = tmp_path / ".ssh"
        ssh.mkdir()
        (ssh / "id_ed25519.pub").write_text("ssh-ed25519 AUTO\n")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert ccm.resolve_ssh_public_key(None) == "ssh-ed25519 AUTO"

    def test_autodetect_none_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))  # empty home dir, no keys
        assert ccm.resolve_ssh_public_key(None) is None


class TestBuildStartup:
    def _startup(self, **over):
        kw = dict(base_dir="/root/workspace", work_dir="/root/workspace",
                  code_server=True, marimo=True, ssh=True,
                  settings_repo=None, settings_branch="main",
                  repo=None, ref="main", clone_dir=None,
                  post_clone=None, run_cmd=None, exit_on_finish=False)
        kw.update(over)
        return ccm.build_startup(kw["base_dir"], kw["work_dir"], kw["code_server"],
                                 kw["marimo"], kw["ssh"], kw["settings_repo"],
                                 kw["settings_branch"], kw["repo"], kw["ref"], kw["clone_dir"],
                                 post_clone=kw["post_clone"], run_cmd=kw["run_cmd"],
                                 exit_on_finish=kw["exit_on_finish"])

    def test_writes_credentials(self):
        assert ".credentials.json" in self._startup()

    def test_ssh_runs_sshd_in_foreground_with_ttyd_background(self):
        s = self._startup(ssh=True)
        assert "exec /usr/sbin/sshd -D" in s
        assert "ttyd -W -p" in s          # background terminal still runs
        assert "exec ttyd" not in s        # ttyd is NOT the foreground process

    def test_no_ssh_runs_ttyd_in_foreground(self):
        s = self._startup(ssh=False)
        assert "exec ttyd -W -p" in s
        assert "sshd" not in s

    def test_repo_clone_is_guarded(self):
        s = self._startup(repo="owner/repo", clone_dir="/root/workspace/code")
        assert "git init" in s and "fetch -q --depth 1 origin" in s
        assert "/root/workspace/code/.git" in s  # idempotent: skip if already cloned

    def test_no_repo_means_no_clone(self):
        assert "git init" not in self._startup(repo=None, clone_dir=None)

    def test_sha_ref_is_checked_out_detached(self):
        """A pinned commit can't be `clone --branch`-ed; init+fetch handles it,
        and a 40-hex ref must land detached rather than as a branch of that name."""
        s = self._startup(repo="owner/repo", clone_dir="/root/workspace/code",
                          ref="1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b")
        assert "checkout -q --detach FETCH_HEAD" in s
        assert "checkout -q -B" in s  # the branch-name arm is still there

    def test_branch_ref_still_lands_on_a_local_branch(self):
        s = self._startup(repo="owner/repo", clone_dir="/root/workspace/code",
                          ref="claude/some-feature")
        assert "checkout -q -B" in s

    def test_settings_repo_sync_present(self):
        assert "claude-settings" in self._startup(settings_repo="ThatE10/claude-code-settings")

    def test_settings_repo_absent_when_none(self):
        assert "claude-settings" not in self._startup(settings_repo=None)

    def test_code_server_toggle(self):
        assert "code-server" in self._startup(code_server=True)
        assert "code-server" not in self._startup(code_server=False)

    def test_marimo_toggle(self):
        assert "marimo edit" in self._startup(marimo=True)
        assert "marimo edit" not in self._startup(marimo=False)


class TestBatchMode:
    """--run / --exit-on-finish: the Slurm-style commission-run-die path."""

    def _startup(self, **over):
        kw = dict(base_dir="/root/workspace", work_dir="/root/workspace/code",
                  code_server=False, marimo=False, ssh=False,
                  settings_repo=None, settings_branch="main",
                  repo="owner/repo", ref="main", clone_dir="/root/workspace/code",
                  post_clone=None, run_cmd=None, exit_on_finish=False)
        kw.update(over)
        return ccm.build_startup(**kw)

    def test_no_run_cmd_keeps_the_keepalive(self):
        s = self._startup(ssh=True)
        assert "exec /usr/sbin/sshd -D" in s
        assert "[batch]" not in s

    def test_run_cmd_is_executed_in_the_work_dir(self):
        s = self._startup(run_cmd="python main_toy.py --config experiments/e1.yaml")
        assert "python main_toy.py --config experiments/e1.yaml" in s
        assert "cd '/root/workspace/code'" in s
        assert ".last_run.log" in s

    def test_exit_on_finish_drops_the_keepalive_and_propagates_status(self):
        s = self._startup(run_cmd="true", exit_on_finish=True)
        assert 'exit "$CCM_RC"' in s
        assert "exec /usr/sbin/sshd" not in s
        assert "exec ttyd" not in s

    def test_without_exit_on_finish_the_sandbox_stays_up(self):
        """'Run it, but leave me a shell to poke at the wreckage.'"""
        s = self._startup(run_cmd="true", exit_on_finish=False, ssh=True)
        assert "[batch] exit=" in s
        assert "exec /usr/sbin/sshd -D" in s  # keep-alive survives the run

    def test_run_cmd_quoting_cannot_escape_the_startup_script(self):
        """The heredoc delimiter is quoted, so the body is copied verbatim
        instead of being interpolated into a command line."""
        s = self._startup(run_cmd="echo 'it\\'s fine' && rm -rf /nope")
        assert "<<'CCM_BATCH_EOF'" in s
        assert "CCM_BATCH_EOF" in s.split("<<'CCM_BATCH_EOF'", 1)[1]

    def test_pipestatus_not_tee_status_is_reported(self):
        """`| tee` would otherwise mask a failing command with tee's own 0."""
        s = self._startup(run_cmd="false", exit_on_finish=True)
        assert "set -o pipefail" in s
        assert "CCM_RC=${PIPESTATUS[0]}" in s

    def test_post_clone_runs_after_the_checkout_and_before_the_command(self):
        s = self._startup(post_clone="docker/link_env.sh", run_cmd="true")
        assert s.index("fetch -q --depth 1 origin") < s.index("[post-clone]")
        assert s.index("[post-clone]") < s.index("[batch] running in")

    def test_post_clone_resolves_relative_to_the_checkout(self):
        s = self._startup(post_clone="docker/link_env.sh")
        assert "/root/workspace/code/docker/link_env.sh" in s

    def test_post_clone_absolute_path_is_left_alone(self):
        s = self._startup(post_clone="/opt/setup.sh")
        assert "'/opt/setup.sh'" in s
        assert "/root/workspace/code/opt/setup.sh" not in s

    def test_post_clone_failure_warns_but_does_not_abort(self):
        s = self._startup(post_clone="setup.sh")
        assert "WARNING" in s


class TestMain:
    """Drive main() end-to-end against the modal stub and inspect what it would
    have passed to Sandbox.create."""

    def _run(self, **over):
        modal.reset()
        kw = dict(repo=None, ref="main", hours=4, github_token=None,
                  gpu=None, cpu=None, memory=None, image_base="slim",
                  registry_secret="ghcr-secret", extra_secrets=[],
                  code_server=True, marimo=True, settings_repo=None,
                  settings_branch="main", ssh=True, ssh_public_key="ssh-ed25519 X",
                  ssh_password=None, persist=True, volume_name="rspd-pre",
                  hf_cache_volume="claude-code-hf-cache")
        kw.update(over)
        self.rc = ccm.main(**kw)
        return modal.LAST_SANDBOX_KWARGS

    def test_persist_mounts_the_four_volumes(self):
        vols = self._run(persist=True, volume_name="rspd-pre")["volumes"]
        assert set(vols) == {"/root/workspace", "/root/.claude", "/root/.local",
                             "/root/.cache/huggingface"}
        assert vols["/root/workspace"].name == "ccm-rspd-pre-workspace"
        assert vols["/root/.claude"].name == "ccm-rspd-pre-claude"
        assert vols["/root/.local"].name == "ccm-rspd-pre-local"
        assert vols["/root/.cache/huggingface"].name == "claude-code-hf-cache"

    def test_all_volumes_created_if_missing(self):
        self._run(persist=True)
        assert modal.CREATED_VOLUMES  # something was created
        assert all(create_if_missing for _, create_if_missing in modal.CREATED_VOLUMES)

    def test_no_persist_mounts_nothing(self):
        assert self._run(persist=False)["volumes"] == {}

    def test_ssh_exposes_unencrypted_port_22(self):
        assert self._run(ssh=True).get("unencrypted_ports") == [ccm.PORT_SSH]

    def test_no_ssh_has_no_unencrypted_port(self):
        assert "unencrypted_ports" not in self._run(ssh=False)

    def test_workdir_is_base_without_repo(self):
        assert self._run(repo=None)["workdir"] == ccm.BASE_DIR

    def test_workdir_is_clone_dir_with_repo(self):
        k = self._run(repo="owner/repo")
        assert k["workdir"] == f"{ccm.BASE_DIR}/code"

    def test_slug_falls_back_to_repo_when_no_volume_name(self):
        vols = self._run(volume_name=None, repo="ThatE10/rspd")["volumes"]
        assert vols["/root/workspace"].name == "ccm-thate10-rspd-workspace"

    def test_encrypted_ports_include_terminal_and_services(self):
        k = self._run(code_server=True, marimo=True)
        assert ccm.PORT_TERMINAL in k["encrypted_ports"]
        assert ccm.PORT_CODE in k["encrypted_ports"]
        assert ccm.PORT_MARIMO in k["encrypted_ports"]
        # SSH is a raw TCP tunnel, never an encrypted port.
        assert ccm.PORT_SSH not in k["encrypted_ports"]

    def test_resource_kwargs_forwarded(self):
        k = self._run(gpu="L40S", cpu=2.0, memory=65294)
        assert k["gpu"] == "L40S"
        assert k["cpu"] == 2.0
        assert k["memory"] == 65294

    def test_timeout_is_hours_in_seconds(self):
        assert self._run(hours=16)["timeout"] == 16 * 3600


class TestMainBatch:
    """main()'s run-then-die path: no services, extra mounts, exit status."""

    def _run(self, returncode=0, **over):
        modal.reset()
        modal.NEXT_RETURNCODE = returncode  # after reset(), which clears it
        kw = dict(repo="ThatE10/rspd", ref="main", hours=4, github_token=None,
                  gpu=None, cpu=None, memory=None, image_base="slim",
                  registry_secret="ghcr-secret", extra_secrets=[],
                  code_server=True, marimo=True, settings_repo=None,
                  settings_branch="main", ssh=True, ssh_public_key="ssh-ed25519 X",
                  ssh_password=None, persist=True, volume_name="rspd-run",
                  hf_cache_volume="claude-code-hf-cache",
                  run_cmd="python main_toy.py --config experiments/e1.yaml",
                  exit_on_finish=True)
        kw.update(over)
        self.rc = ccm.main(**kw)
        return modal.LAST_SANDBOX_KWARGS

    def test_batch_exit_disables_the_interactive_services(self):
        """A sandbox that dies when the job dies has no use for VS Code/SSH,
        and there'd be no tunnels to print anyway."""
        k = self._run(code_server=True, marimo=True, ssh=True)
        assert k["encrypted_ports"] == []
        assert "unencrypted_ports" not in k
        assert "code-server" not in k
        startup = modal.LAST_SANDBOX_ARGS[-1]
        assert "code-server --auth none" not in startup
        assert "exec /usr/sbin/sshd" not in startup

    def test_run_without_exit_on_finish_keeps_the_services(self):
        k = self._run(exit_on_finish=False)
        assert ccm.PORT_CODE in k["encrypted_ports"]
        assert k["unencrypted_ports"] == [ccm.PORT_SSH]

    def test_batch_waits_and_returns_the_command_status(self):
        self._run(returncode=0)
        assert self.rc == 0
        self._run(returncode=17)
        assert modal.LAST_SANDBOX.waited
        assert self.rc == 17

    def test_detach_does_not_wait(self):
        self._run(returncode=17, detach=True)
        assert not modal.LAST_SANDBOX.waited
        assert self.rc == 0

    def test_interactive_launch_returns_zero(self):
        self._run(run_cmd=None, exit_on_finish=False)
        assert self.rc == 0

    def test_extra_volumes_are_mounted_alongside_the_standard_set(self):
        vols = self._run(extra_volumes={"/root/artifacts": "rspd-artifacts"})["volumes"]
        assert vols["/root/artifacts"].name == "rspd-artifacts"
        # The standard trio + HF cache are untouched.
        assert vols["/root/workspace"].name == "ccm-rspd-run-workspace"
        assert vols["/root/.cache/huggingface"].name == "claude-code-hf-cache"

    def test_extra_volumes_are_created_if_missing(self):
        self._run(extra_volumes={"/root/artifacts": "rspd-artifacts"})
        assert ("rspd-artifacts", True) in modal.CREATED_VOLUMES

    def test_extra_volumes_work_without_persist(self):
        """An ephemeral workspace can still write to a shared artifacts volume —
        that split is the whole point of the mount flag."""
        vols = self._run(persist=False,
                         extra_volumes={"/root/artifacts": "rspd-artifacts"})["volumes"]
        assert set(vols) == {"/root/artifacts"}

    def test_post_clone_reaches_the_startup_script(self):
        self._run(post_clone="docker/link_env.sh")
        assert "[post-clone] running docker/link_env.sh" in modal.LAST_SANDBOX_ARGS[-1]
