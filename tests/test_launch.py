"""Unit tests for launch.py — preset → command construction and preset removal.

launch.py imports only the stdlib, so these run without any stubbing.
"""
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
        cmd = launch.build_cmd(_preset(extra_secrets=["huggingface-secret", "wandb-secret"]))
        assert cmd.count("--secret") == 2
        assert "wandb-secret" in cmd

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
