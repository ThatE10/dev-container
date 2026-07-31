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
                  repo=None, ref="main", clone_dir=None)
        kw.update(over)
        return ccm.build_startup(kw["base_dir"], kw["work_dir"], kw["code_server"],
                                 kw["marimo"], kw["ssh"], kw["settings_repo"],
                                 kw["settings_branch"], kw["repo"], kw["ref"], kw["clone_dir"])

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
        assert "git clone" in s
        assert "/root/workspace/code/.git" in s  # idempotent: skip if already cloned

    def test_no_repo_means_no_clone(self):
        assert "git clone" not in self._startup(repo=None, clone_dir=None)

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
        ccm.main(**kw)
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
