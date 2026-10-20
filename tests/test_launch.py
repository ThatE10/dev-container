"""Unit tests for launch.py — preset → command construction and preset removal.

launch.py imports only the stdlib, so these run without any stubbing.
"""
import json
import os
from pathlib import Path

import pytest

import launch


def _preset(**over):
    base = {
        "name": "SAE Lens A100",
        "image": launch.DEFAULT_IMAGE,
        "gpu": "A100-80GB",
        "cpu": 8.0,
        "memory": 32768,
        "repo": None,
        "ref": None,
        "hours": 8,
        "code_server": True,
        "marimo": True,
        "settings_repo": "ThatE10/claude-code-settings",
        "use_gh_token": False,
        "extra_secrets": ["huggingface-secret"],
        "persist": True,
        "volume": None,
        "count": 0,
    }
    base.update(over)
    return base


def _feed(monkeypatch, answers):
    """Script builtins.input with a fixed sequence of answers. Running out of
    answers raises StopIteration, failing any test that prompts unexpectedly."""
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(it))


class TestSlugify:
    def test_spaces_and_case(self):
        assert launch.slugify("SAE Lens A100") == "sae-lens-a100"

    def test_strips_symbols_and_edges(self):
        assert launch.slugify("  weird__Name!! ") == "weird-name"

    def test_already_slug(self):
        assert launch.slugify("already-good") == "already-good"

    def test_empty_falls_back(self):
        assert launch.slugify("") == "dev-box"
        assert launch.slugify("###") == "dev-box"


class TestPresetVolume:
    def test_default_persistent_uses_name(self):
        assert launch.preset_volume(_preset(name="My Box", volume=None)) == "my-box"

    def test_explicit_volume_wins(self):
        assert launch.preset_volume(_preset(volume="shared")) == "shared"

    def test_ephemeral_returns_none(self):
        assert launch.preset_volume(_preset(persist=False)) is None

    def test_legacy_preset_without_keys_defaults_persistent(self):
        legacy = {"name": "old preset"}  # predates persist/volume keys
        assert launch.preset_volume(legacy) == "old-preset"


class TestKnownVolumes:
    def test_groups_presets_by_volume(self):
        presets = [_preset(name="alpha", volume="shared"),
                   _preset(name="beta", volume="shared"),
                   _preset(name="gamma", volume=None)]  # falls back to slug(name)
        assert launch.known_volumes(presets) == {
            "shared": ["alpha", "beta"],
            "gamma": ["gamma"],
        }

    def test_skips_ephemeral_presets(self):
        assert launch.known_volumes([_preset(name="alpha", persist=False)]) == {}

    def test_empty(self):
        assert launch.known_volumes([]) == {}


class TestChooseVolume:
    """Picker options for these presets: 1) alpha  2) shared  3) [new]  4) [ephemeral]."""

    @pytest.fixture
    def presets(self):
        return [_preset(name="alpha", volume="alpha"),
                _preset(name="beta", volume="shared"),
                _preset(name="gamma", volume="shared")]

    def test_pick_existing_by_number(self, presets, monkeypatch):
        _feed(monkeypatch, ["2"])
        assert launch.choose_volume(presets) == (True, "shared")

    def test_enter_picks_default_volume(self, presets, monkeypatch):
        _feed(monkeypatch, [""])
        assert launch.choose_volume(presets, default="shared") == (True, "shared")

    def test_new_name_is_slugified(self, presets, monkeypatch):
        _feed(monkeypatch, ["3", "My Vol"])
        assert launch.choose_volume(presets) == (True, "my-vol")

    def test_ephemeral_option(self, presets, monkeypatch):
        _feed(monkeypatch, ["4"])
        assert launch.choose_volume(presets) == (False, None)

    def test_typed_name_accepted_directly(self, presets, monkeypatch):
        _feed(monkeypatch, ["Shared Storage!"])
        assert launch.choose_volume(presets) == (True, "shared-storage")

    def test_symbol_only_input_rejected(self, presets, monkeypatch):
        _feed(monkeypatch, ["!!!", "4"])  # garbage re-prompts instead of 'dev-box'
        assert launch.choose_volume(presets) == (False, None)

    def test_unknown_default_flows_to_new_name_prompt(self, presets, monkeypatch):
        # default not among known volumes → [new volume name] preselected, and
        # accepting the name prompt's default keeps it.
        _feed(monkeypatch, ["", ""])
        assert launch.choose_volume(presets, default="fresh") == (True, "fresh")

    def test_no_presets_defaults_to_ephemeral(self, monkeypatch):
        _feed(monkeypatch, [""])
        assert launch.choose_volume([]) == (False, None)

    def test_no_presets_new_name(self, monkeypatch):
        _feed(monkeypatch, ["1", "box"])
        assert launch.choose_volume([]) == (True, "box")

    def test_shared_pick_warns_about_other_users(self, presets, monkeypatch, capsys):
        _feed(monkeypatch, ["2"])
        launch.choose_volume(presets, current_preset="beta")
        out = capsys.readouterr().out
        assert "shares storage with: gamma" in out
        assert "beta" not in out.split("shares storage with:")[1].splitlines()[0]

    def test_own_volume_pick_has_no_warning(self, presets, monkeypatch, capsys):
        _feed(monkeypatch, ["1"])
        launch.choose_volume(presets, default="alpha", current_preset="alpha")
        assert "shares storage with" not in capsys.readouterr().out


class TestOverrideVolumeFlow:
    @pytest.fixture
    def presets(self):
        return [_preset(name="alpha", volume="alpha"),
                _preset(name="beta", volume="shared")]

    def test_one_off_override_leaves_preset_untouched(self, presets, monkeypatch):
        _feed(monkeypatch, ["2", "n"])  # pick 'shared', don't save
        assert launch.override_volume_flow(presets, presets[0]) == (True, "shared")
        assert presets[0]["volume"] == "alpha"
        assert presets[0]["persist"] is True

    def test_save_updates_preset(self, presets, monkeypatch):
        _feed(monkeypatch, ["2", "y"])  # pick 'shared', save to preset
        assert launch.override_volume_flow(presets, presets[0]) == (True, "shared")
        assert presets[0]["volume"] == "shared"
        assert presets[0]["persist"] is True

    def test_unchanged_choice_skips_save_prompt(self, presets, monkeypatch):
        _feed(monkeypatch, ["1"])  # re-pick own volume; no save prompt expected
        assert launch.override_volume_flow(presets, presets[0]) == (True, "alpha")
        assert presets[0]["volume"] == "alpha"

    def test_ephemeral_override(self, presets, monkeypatch):
        _feed(monkeypatch, ["4", ""])  # ephemeral, decline save (default no)
        assert launch.override_volume_flow(presets, presets[0]) == (False, None)
        assert presets[0]["volume"] == "alpha"
        assert presets[0].get("persist", True) is True


class TestMenuVolumeSuffix:
    """End-to-end through menu(): 'Nv' opens the picker, plain N doesn't."""

    @pytest.fixture
    def env(self, monkeypatch):
        presets = [_preset(name="alpha", volume="alpha", count=5),
                   _preset(name="beta", volume="shared")]
        launched = []
        monkeypatch.setattr(launch, "launch", lambda p: launched.append(p) or 0)
        monkeypatch.setattr(launch, "save_presets", lambda p: None)
        return presets, launched

    def test_suffix_launches_on_picked_volume(self, env, monkeypatch):
        presets, launched = env
        _feed(monkeypatch, ["1v", "2", "n"])  # preset 1, volume 'shared', one-off
        with pytest.raises(SystemExit) as e:
            launch.menu(presets)
        assert e.value.code == 0
        assert launch.preset_volume(launched[0]) == "shared"
        assert presets[0]["volume"] == "alpha"  # stored preset untouched
        assert presets[0]["count"] == 6

    def test_bare_v_means_default_preset_with_picker(self, env, monkeypatch):
        presets, launched = env
        _feed(monkeypatch, ["v", ""])  # default preset, keep its own volume
        with pytest.raises(SystemExit) as e:
            launch.menu(presets)
        assert e.value.code == 0
        assert launch.preset_volume(launched[0]) == "alpha"

    def test_plain_choice_never_prompts_for_volume(self, env, monkeypatch):
        presets, launched = env
        _feed(monkeypatch, ["1"])  # any further prompt would raise StopIteration
        with pytest.raises(SystemExit) as e:
            launch.menu(presets)
        assert e.value.code == 0
        assert launch.preset_volume(launched[0]) == "alpha"


class TestCmdVolumes:
    def test_lists_volumes_and_users(self, capsys):
        presets = [_preset(name="alpha", volume="shared"),
                   _preset(name="beta", volume="shared"),
                   _preset(name="solo", volume=None)]
        launch.cmd_volumes(presets)
        out = capsys.readouterr().out
        assert "shared" in out and "alpha, beta" in out
        assert "solo" in out

    def test_no_volumes(self, capsys):
        launch.cmd_volumes([_preset(persist=False)])
        assert "no persistent volumes" in capsys.readouterr().out


class TestBuildCmd:
    def test_persistent_forwards_volume_name(self):
        cmd = launch.build_cmd(_preset(name="rspd-pre"))
        assert "--volume-name" in cmd
        assert cmd[cmd.index("--volume-name") + 1] == "rspd-pre"
        assert "--no-persist" not in cmd
        # The old redundant flag must be gone.
        assert "--volume" not in cmd

    def test_ephemeral_forwards_no_persist(self):
        cmd = launch.build_cmd(_preset(persist=False))
        assert "--no-persist" in cmd
        assert "--volume-name" not in cmd

    def test_service_optouts(self):
        cmd = launch.build_cmd(_preset(code_server=False, marimo=False))
        assert "--no-code-server" in cmd
        assert "--no-marimo" in cmd

    def test_default_services_have_no_optout(self):
        cmd = launch.build_cmd(_preset(code_server=True, marimo=True))
        assert "--no-code-server" not in cmd
        assert "--no-marimo" not in cmd

    def test_empty_secrets_clears_defaults(self):
        cmd = launch.build_cmd(_preset(extra_secrets=[]))
        i = cmd.index("--secret")
        assert cmd[i + 1] == ""

    def test_secrets_forwarded(self):
        cmd = launch.build_cmd(_preset(extra_secrets=["huggingface-secret", "openai-secret"]))
        assert cmd.count("--secret") == 2
        assert "openai-secret" in cmd

    def test_default_extra_secrets_includes_wandb(self):
        # No "extra_secrets" key at all -> build_cmd falls back to
        # DEFAULT_EXTRA_SECRETS, which now includes wandb-secret by default
        # alongside huggingface-secret and github-secret.
        preset = _preset()
        del preset["extra_secrets"]
        cmd = launch.build_cmd(preset)
        assert cmd.count("--secret") == len(launch.DEFAULT_EXTRA_SECRETS)
        assert "wandb-secret" in cmd
        assert "huggingface-secret" in cmd
        assert "github-secret" in cmd

    def test_duplicate_secrets_are_not_deduped_by_build_cmd(self):
        # build_cmd forwards the preset's extra_secrets list verbatim; it is
        # the preset data (or the user) that is responsible for not listing
        # the same secret twice. Document that behavior explicitly so a
        # future default-list change doesn't silently start double-attaching
        # a secret that's both a default and an explicit extra.
        cmd = launch.build_cmd(_preset(extra_secrets=["wandb-secret", "wandb-secret"]))
        assert cmd.count("wandb-secret") == 2

    def test_repo_and_ref(self):
        cmd = launch.build_cmd(_preset(repo="owner/repo", ref="dev"))
        assert cmd[cmd.index("--repo") + 1] == "owner/repo"
        assert cmd[cmd.index("--ref") + 1] == "dev"

    def test_no_repo_omits_repo_flag(self):
        cmd = launch.build_cmd(_preset(repo=None))
        assert "--repo" not in cmd

    def test_gpu_forwarded_and_omitted(self):
        assert "--gpu" in launch.build_cmd(_preset(gpu="L40S"))
        assert "--gpu" not in launch.build_cmd(_preset(gpu=None))


class TestSummarize:
    def test_persistent_shows_volume(self):
        assert "vol:my-box" in launch.summarize(_preset(name="My Box", volume=None))

    def test_ephemeral_shows_ephemeral(self):
        assert "ephemeral" in launch.summarize(_preset(persist=False))


class TestDeletePreset:
    def test_delete_returns_and_mutates(self):
        presets = [_preset(name="a"), _preset(name="b"), _preset(name="c")]
        removed = launch.delete_preset_at(presets, 2)
        assert removed["name"] == "b"
        assert [p["name"] for p in presets] == ["a", "c"]

    def test_delete_first_and_last(self):
        presets = [_preset(name="a"), _preset(name="b"), _preset(name="c")]
        assert launch.delete_preset_at(presets, 1)["name"] == "a"
        assert launch.delete_preset_at(presets, 2)["name"] == "c"
        assert [p["name"] for p in presets] == ["b"]

    def test_delete_out_of_range_raises(self):
        presets = [_preset(name="a")]
        with pytest.raises(IndexError):
            launch.delete_preset_at(presets, 2)
        with pytest.raises(IndexError):
            launch.delete_preset_at(presets, 0)


class TestRemoveFlow:
    def test_removes_selected(self, monkeypatch):
        presets = [_preset(name="a"), _preset(name="b"), _preset(name="c")]
        monkeypatch.setattr("builtins.input", lambda *a, **k: "2")
        saved = []
        monkeypatch.setattr(launch, "save_presets", lambda p: saved.append([x["name"] for x in p]))
        launch.remove_flow(presets)
        assert [p["name"] for p in presets] == ["a", "c"]
        assert saved == [["a", "c"]]  # persisted once

    def test_blank_input_cancels(self, monkeypatch):
        presets = [_preset(name="a"), _preset(name="b")]
        monkeypatch.setattr("builtins.input", lambda *a, **k: "")
        saved = []
        monkeypatch.setattr(launch, "save_presets", lambda p: saved.append(p))
        launch.remove_flow(presets)
        assert [p["name"] for p in presets] == ["a", "b"]
        assert not saved  # nothing persisted on cancel

    def test_invalid_index_is_noop(self, monkeypatch):
        presets = [_preset(name="a")]
        monkeypatch.setattr("builtins.input", lambda *a, **k: "9")
        saved = []
        monkeypatch.setattr(launch, "save_presets", lambda p: saved.append(p))
        launch.remove_flow(presets)
        assert [p["name"] for p in presets] == ["a"]
        assert not saved


class TestCostEstimates:
    def test_gpu_rate_from_table(self):
        assert launch.gpu_hourly("H100") == pytest.approx(launch.GPU_HOURLY["H100"])

    def test_gpu_count_multiplies(self):
        assert launch.gpu_hourly("A100-80GB:2") == pytest.approx(
            launch.GPU_HOURLY["A100-80GB"] * 2)

    def test_no_gpu_is_free(self):
        assert launch.gpu_hourly(None) == 0.0

    def test_unknown_gpu_is_unknown_not_zero(self):
        assert launch.gpu_hourly("B200") is None
        assert launch.hourly_cost(_preset(gpu="B200")) is None
        assert launch.session_cost(_preset(gpu="B200")) is None
        assert "unknown" in launch.cost_note(_preset(gpu="B200"))

    def test_hourly_adds_cpu_and_memory(self):
        p = _preset(gpu=None, cpu=4.0, memory=8192)
        assert launch.hourly_cost(p) == pytest.approx(
            4 * launch.CPU_CORE_HOURLY + 8 * launch.MEM_GIB_HOURLY)

    def test_session_cost_is_rate_times_hours(self):
        p = _preset(gpu="T4", cpu=2.0, memory=4096, hours=6)
        assert launch.session_cost(p) == pytest.approx(launch.hourly_cost(p) * 6)

    def test_cost_note_shows_rate_and_total(self):
        note = launch.cost_note(_preset(gpu="T4", cpu=2.0, memory=4096, hours=6))
        assert "/h" in note and "for 6h" in note

    def test_summarize_carries_the_cost(self):
        assert "/h" in launch.summarize(_preset(gpu="T4"))

    def test_cheaper_gpu_estimates_less(self):
        cheap = launch.session_cost(_preset(gpu="T4"))
        dear = launch.session_cost(_preset(gpu="H100"))
        assert cheap < dear


class TestChooseGpuDefaults:
    def test_default_preselects_stored_gpu(self, monkeypatch):
        _feed(monkeypatch, ["", ""])  # keep the type, keep the count
        assert launch.choose_gpu("L40S") == "L40S"

    def test_default_keeps_multi_gpu_count(self, monkeypatch):
        _feed(monkeypatch, ["", ""])
        assert launch.choose_gpu("A100-80GB:2") == "A100-80GB:2"

    def test_unknown_stored_gpu_falls_back_to_first_option(self, monkeypatch):
        _feed(monkeypatch, [""])  # option 1 is 'none (CPU only)'
        assert launch.choose_gpu("B200") is None

    def test_no_default_keeps_cpu_only(self, monkeypatch):
        _feed(monkeypatch, [""])
        assert launch.choose_gpu() is None


class TestPromptHelpers:
    def test_blank_keeps_current(self, monkeypatch):
        _feed(monkeypatch, [""])
        assert launch.prompt_optional("repo", "owner/repo") == "owner/repo"

    def test_dash_clears(self, monkeypatch):
        _feed(monkeypatch, ["-"])
        assert launch.prompt_optional("repo", "owner/repo") is None

    def test_blank_with_no_current_is_none(self, monkeypatch):
        _feed(monkeypatch, [""])
        assert launch.prompt_optional("repo", None) is None

    def test_list_keeps_current(self, monkeypatch):
        _feed(monkeypatch, [""])
        assert launch.prompt_list("secrets", ["a", "b"]) == ["a", "b"]

    def test_list_dash_empties(self, monkeypatch):
        _feed(monkeypatch, ["-"])
        assert launch.prompt_list("secrets", ["a", "b"]) == []

    def test_list_splits_and_strips(self, monkeypatch):
        _feed(monkeypatch, ["a , b"])
        assert launch.prompt_list("secrets", None) == ["a", "b"]


class TestEditPreset:
    """Answers, in prompt order: name, image, gpu, [gpu count], cpu, memory,
    repo, [ref], hours, code_server, marimo, settings_repo, [use_gh_token],
    with_launcher, extra_repos, extra_secrets, volume."""

    @pytest.fixture
    def saved(self, monkeypatch):
        out = []
        monkeypatch.setattr(launch, "save_presets", lambda p: out.append(p))
        return out

    def test_all_blank_answers_keep_every_field(self, saved, monkeypatch):
        p = _preset(name="alpha", volume="alpha", repo="owner/repo", ref="dev",
                    gpu="A100-80GB", count=7, extra_repos=["x/y"])
        before = dict(p)
        _feed(monkeypatch, [""] * 17)
        launch.edit_preset([p], p)
        for key in before:
            assert p[key] == before[key], key
        assert saved  # persisted

    def test_changes_are_applied_in_place(self, saved, monkeypatch):
        p = _preset(name="alpha", volume="alpha", gpu="A100-80GB", hours=8)
        presets = [p]
        _feed(monkeypatch, [
            "",       # name unchanged
            "",       # image unchanged
            "2", "",  # GPU → T4, 1 of them
            "4",      # cpu
            "8192",   # memory
            "",       # repo (none)
            "2",      # hours
            "n",      # code_server off
            "",       # marimo
            "",       # settings repo
            "y",      # with_launcher
            "",       # extra repos
            "",       # extra secrets
            "",       # volume — keeps 'alpha'
        ])
        launch.edit_preset(presets, p)
        assert presets[0] is p  # same object, edited in place
        assert p["gpu"] == "T4"
        assert p["cpu"] == 4.0 and p["memory"] == 8192 and p["hours"] == 2
        assert p["code_server"] is False and p["marimo"] is True
        assert p["with_launcher"] is True
        assert launch.preset_volume(p) == "alpha"

    def test_repo_can_be_cleared_and_skips_ref_prompt(self, saved, monkeypatch):
        p = _preset(name="alpha", volume="alpha", repo="owner/repo", ref="dev",
                    gpu=None, use_gh_token=True)
        # No ref/gh-token prompts once the repo is cleared; a stray answer would
        # desync the rest of the script and fail the assertions below.
        _feed(monkeypatch, ["", "", "", "", "", "-", "", "", "", "", "", "", "", ""])
        launch.edit_preset([p], p)
        assert p["repo"] is None and p["ref"] is None
        assert p["use_gh_token"] is False

    def test_unknown_keys_survive_an_edit(self, saved, monkeypatch):
        p = _preset(name="alpha", volume="alpha", gpu=None)
        p["launcher_ref"] = "feature-branch"
        p["modal_secret"] = ""
        _feed(monkeypatch, [""] * 16)
        launch.edit_preset([p], p)
        assert p["launcher_ref"] == "feature-branch"
        assert p["modal_secret"] == ""

    def test_editing_adopts_a_catalog_preset(self, saved, monkeypatch):
        p = _preset(name="alpha", volume="alpha", gpu=None)
        p[launch.SOURCE_KEY] = "builtin"
        _feed(monkeypatch, [""] * 16)
        launch.edit_preset([p], p)
        assert p[launch.SOURCE_KEY] == "user"

    def test_rename_defaults_volume_to_the_new_slug(self, saved, monkeypatch):
        p = _preset(name="alpha", volume=None, gpu=None)  # volume follows the name
        _feed(monkeypatch, ["New Box"] + [""] * 15)
        launch.edit_preset([p], p)
        assert p["name"] == "New Box"
        assert launch.preset_volume(p) == "new-box"

    def test_ephemeral_preset_stays_ephemeral_on_blank_answers(self, saved, monkeypatch):
        p = _preset(name="alpha", persist=False, volume=None, gpu=None)
        _feed(monkeypatch, [""] * 16)
        launch.edit_preset([p], p)
        assert launch.preset_volume(p) is None


class TestEditFlow:
    def _presets(self):
        return [_preset(name="a", volume="a", gpu=None),
                _preset(name="b", volume="b", gpu=None)]

    def test_edits_selected(self, monkeypatch):
        presets = self._presets()
        monkeypatch.setattr(launch, "save_presets", lambda p: None)
        edited = []
        monkeypatch.setattr(launch, "edit_preset",
                            lambda ps, p: edited.append(p["name"]))
        _feed(monkeypatch, ["2"])
        launch.edit_flow(presets)
        assert edited == ["b"]

    def test_blank_cancels(self, monkeypatch):
        monkeypatch.setattr(launch, "edit_preset",
                            lambda *a: pytest.fail("should not edit"))
        _feed(monkeypatch, [""])
        launch.edit_flow(self._presets())

    def test_invalid_index_is_noop(self, monkeypatch):
        monkeypatch.setattr(launch, "edit_preset",
                            lambda *a: pytest.fail("should not edit"))
        _feed(monkeypatch, ["9"])
        launch.edit_flow(self._presets())

    def test_no_presets(self, capsys):
        launch.edit_flow([])
        assert "no presets to edit" in capsys.readouterr().out


class TestMenuEditSuffix:
    @pytest.fixture
    def env(self, monkeypatch):
        presets = [_preset(name="alpha", volume="alpha", count=5),
                   _preset(name="beta", volume="shared")]
        monkeypatch.setattr(launch, "launch", lambda p: 0)
        monkeypatch.setattr(launch, "save_presets", lambda p: None)
        edited = []
        monkeypatch.setattr(launch, "edit_preset",
                            lambda ps, p: edited.append(p["name"]))
        return presets, edited

    def test_suffix_edits_then_redraws_and_launches(self, env, monkeypatch):
        presets, edited = env
        _feed(monkeypatch, ["2e", "1"])  # edit beta, then launch alpha
        with pytest.raises(SystemExit) as e:
            launch.menu(presets)
        assert e.value.code == 0
        assert edited == ["beta"]

    def test_edit_menu_entry(self, env, monkeypatch):
        presets, edited = env
        _feed(monkeypatch, ["4", "1", "1"])  # [edit a preset] → #1, then launch #1
        with pytest.raises(SystemExit):
            launch.menu(presets)
        assert edited == ["alpha"]

    def test_remove_entry_moved_after_edit(self, env, monkeypatch):
        presets, _ = env
        removed = []
        monkeypatch.setattr(launch, "remove_flow", lambda ps: removed.append(True))
        _feed(monkeypatch, ["5", "1"])  # [remove a preset], then launch
        with pytest.raises(SystemExit):
            launch.menu(presets)
        assert removed == [True]


class TestEnsureSSHKey:
    def test_detects_existing_key(self, tmp_path, monkeypatch):
        (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 EXISTING\n")
        assert launch.has_ssh_key(tmp_path) is True
        # ssh-keygen must NOT run when a key already exists.
        monkeypatch.setattr(launch.subprocess, "run",
                            lambda *a, **k: pytest.fail("ssh-keygen should not run"))
        assert launch.ensure_ssh_key(tmp_path) is True

    def test_detects_rsa_key(self, tmp_path):
        (tmp_path / "id_rsa.pub").write_text("ssh-rsa EXISTING\n")
        assert launch.has_ssh_key(tmp_path) is True

    def test_no_key_reports_false_dir(self, tmp_path):
        assert launch.has_ssh_key(tmp_path) is False

    def test_generates_when_missing(self, tmp_path, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            # Simulate ssh-keygen writing the keypair.
            Path(cmd[cmd.index("-f") + 1] + ".pub").write_text("ssh-ed25519 NEW\n")
            return None

        monkeypatch.setattr(launch.subprocess, "run", fake_run)
        assert launch.ensure_ssh_key(tmp_path) is True
        assert len(calls) == 1
        assert calls[0][0] == "ssh-keygen"
        assert "-N" in calls[0] and calls[0][calls[0].index("-N") + 1] == ""  # no passphrase
        assert (tmp_path / "id_ed25519.pub").exists()

    def test_generation_failure_returns_false(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError("ssh-keygen not installed")

        monkeypatch.setattr(launch.subprocess, "run", boom)
        assert launch.ensure_ssh_key(tmp_path) is False


class TestPresetCatalogs:
    """load_presets merges built-in, project and user catalogs by name."""

    def _write(self, path, entries):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries))
        return path

    def _p(self, name, **over):
        return {"name": name, "gpu": None, "cpu": 2, "memory": 8192,
                "hours": 4, "repo": None, **over}

    @pytest.fixture
    def isolated(self, tmp_path, monkeypatch):
        """Point every catalog at tmp_path so the developer's real
        ~/.config/claude-code-modal/presets.json can't leak into a test."""
        monkeypatch.setattr(launch, "BUILTIN_PRESETS_FILE", tmp_path / "builtin.json")
        monkeypatch.setattr(launch, "PRESETS_FILE", tmp_path / "user.json")
        monkeypatch.setattr(launch, "CONFIG_DIR", tmp_path)
        monkeypatch.delenv("DEVCONTAINER_PRESETS", raising=False)
        return tmp_path

    def test_missing_catalogs_are_not_an_error(self, isolated):
        assert launch.load_presets() == []

    def test_builtin_presets_are_available_with_no_user_file(self, isolated):
        self._write(isolated / "builtin.json", [self._p("gpu-l40s", gpu="L40S")])
        got = launch.load_presets()
        assert [p["name"] for p in got] == ["gpu-l40s"]
        assert got[0][launch.SOURCE_KEY] == "builtin"

    def test_user_preset_overrides_builtin_of_the_same_name(self, isolated):
        self._write(isolated / "builtin.json", [self._p("gpu-l40s", hours=4)])
        self._write(isolated / "user.json", [self._p("gpu-l40s", hours=99)])
        got = launch.load_presets()
        assert len(got) == 1
        assert got[0]["hours"] == 99
        assert got[0][launch.SOURCE_KEY] == "user"

    def test_project_catalog_sits_between_builtin_and_user(self, isolated):
        self._write(isolated / "builtin.json", [self._p("x", hours=1)])
        proj = self._write(isolated / "proj.json", [self._p("x", hours=2)])
        got = launch.load_presets([str(proj)])
        assert got[0]["hours"] == 2 and got[0][launch.SOURCE_KEY] == "project"
        self._write(isolated / "user.json", [self._p("x", hours=3)])
        assert launch.load_presets([str(proj)])[0]["hours"] == 3

    def test_project_catalog_from_env_var(self, isolated, monkeypatch):
        proj = self._write(isolated / "proj.json", [self._p("from-env")])
        monkeypatch.setenv("DEVCONTAINER_PRESETS", str(proj))
        assert [p["name"] for p in launch.load_presets()] == ["from-env"]

    def test_multiple_env_catalogs_are_pathsep_separated(self, isolated, monkeypatch):
        a = self._write(isolated / "a.json", [self._p("a")])
        b = self._write(isolated / "b.json", [self._p("b")])
        monkeypatch.setenv("DEVCONTAINER_PRESETS", f"{a}{os.pathsep}{b}")
        assert {p["name"] for p in launch.load_presets()} == {"a", "b"}

    def test_malformed_catalog_warns_and_is_skipped(self, isolated, capsys):
        (isolated / "builtin.json").write_text("{not json")
        self._write(isolated / "user.json", [self._p("ok")])
        assert [p["name"] for p in launch.load_presets()] == ["ok"]
        assert "warning" in capsys.readouterr().err

    def test_entries_without_a_name_are_dropped(self, isolated):
        self._write(isolated / "user.json", [{"gpu": "L40S"}, self._p("good")])
        assert [p["name"] for p in launch.load_presets()] == ["good"]

    def test_save_does_not_fork_catalog_presets_into_the_user_file(self, isolated):
        """A shared catalog stays the source of truth until a preset is adopted."""
        self._write(isolated / "builtin.json", [self._p("gpu-l40s")])
        presets = launch.load_presets()
        presets.append({**self._p("mine"), launch.SOURCE_KEY: "user"})
        launch.save_presets(presets)
        saved = json.loads((isolated / "user.json").read_text())
        assert [p["name"] for p in saved] == ["mine"]

    def test_saved_presets_carry_no_internal_source_key(self, isolated):
        launch.save_presets([{**self._p("mine"), launch.SOURCE_KEY: "user"}])
        saved = json.loads((isolated / "user.json").read_text())
        assert launch.SOURCE_KEY not in saved[0]

    def test_adopting_a_catalog_preset_persists_it(self, isolated):
        """menu() flips _source to 'user' before saving — the adoption rule."""
        self._write(isolated / "builtin.json", [self._p("gpu-l40s")])
        presets = launch.load_presets()
        presets[0][launch.SOURCE_KEY] = "user"
        presets[0]["count"] = 1
        launch.save_presets(presets)
        assert [p["name"] for p in json.loads((isolated / "user.json").read_text())] \
            == ["gpu-l40s"]

    def test_shipped_builtin_catalog_is_valid_and_loadable(self):
        """The real presets.default.json must parse and satisfy build_cmd."""
        entries = json.loads(launch.BUILTIN_PRESETS_FILE.read_text())
        assert entries, "shipped catalog should not be empty"
        for p in entries:
            for key in ("name", "gpu", "cpu", "memory", "hours", "repo"):
                assert key in p, f"{p.get('name')} missing {key}"
            launch.build_cmd(p)  # must not raise
