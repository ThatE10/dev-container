#!/usr/bin/env python3
"""
Interactive launcher for claude_code_modal.py.

Saves presets (GPU/CPU/RAM/repo/ref/hours + VS Code / Marimo / settings-repo +
persistent volume) to ~/.config/claude-code-modal/presets.json and shows them
sorted by usage count so your most-used config is always at the top.

Presets are persistent by default: each one is backed by a named set of Modal
Volumes (workspace, ~/.local, ~/.claude, HF cache), so relaunching the same
preset restores its files, editor extensions, shell history, and Claude state —
no more ephemeral sandboxes. The volume name defaults to the preset name.

  python launch.py            # menu (also offers [new preset] / [remove a preset])
  python launch.py --list     # print presets, exit
  python launch.py --forget N # delete preset #N
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "claude-code-modal"
PRESETS_FILE = CONFIG_DIR / "presets.json"
SCRIPT = Path(__file__).resolve().parent / "claude_code_modal.py"

DEFAULT_IMAGE = "ghcr.io/thate10/dev-container:latest"
IMAGE_CHOICES = [
    ("dev-container (GPU, vLLM, torch pre-installed)", DEFAULT_IMAGE),
    ("slim (debian_slim — fast, CPU-only)", "slim"),
]
DEFAULT_EXTRA_SECRETS = ["huggingface-secret", "github-secret"]

GPU_CHOICES = [
    ("none (CPU only)", None),
    ("T4",           "T4"),
    ("L4",           "L4"),
    ("A10G",         "A10G"),
    ("L40S",         "L40S"),
    ("A100-40GB",    "A100-40GB"),
    ("A100-80GB",    "A100-80GB"),
    ("H100",         "H100"),
    ("H200",         "H200"),
]


def slugify(name: str) -> str:
    """Turn a preset name into a valid Modal Volume base name."""
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return slug or "dev-box"


def preset_volume(p: dict) -> str | None:
    """The persistent-volume base name for a preset, or None if ephemeral.

    Presets are persistent by default; a missing ``persist`` key (older presets)
    is treated as True so they gain persistence keyed to their name. The volume
    name defaults to the slugified preset name.
    """
    if not p.get("persist", True):
        return None
    return p.get("volume") or slugify(p["name"])


def load_presets() -> list[dict]:
    if not PRESETS_FILE.exists():
        return []
    return json.loads(PRESETS_FILE.read_text())


def save_presets(presets: list[dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PRESETS_FILE.write_text(json.dumps(presets, indent=2))


def prompt(label: str, default: str | None = None) -> str | None:
    hint = f" [{default}]" if default is not None else ""
    value = input(f"  {label}{hint}: ").strip()
    return value if value else default


def yesno(label: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    v = input(f"  {label} [{d}]: ").strip().lower()
    if not v:
        return default
    return v.startswith("y")


def choose_image() -> str:
    print("\n  base image:")
    for i, (label, _) in enumerate(IMAGE_CHOICES, 1):
        print(f"    {i}) {label}")
    while True:
        raw = input(f"    choose [1-{len(IMAGE_CHOICES)}] (default 1): ").strip() or "1"
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(IMAGE_CHOICES):
                return IMAGE_CHOICES[idx][1]
        except ValueError:
            pass
        print("    invalid choice.")


def choose_gpu() -> str | None:
    print("\n  GPU:")
    for i, (label, _) in enumerate(GPU_CHOICES, 1):
        print(f"    {i}) {label}")
    while True:
        raw = input(f"    choose [1-{len(GPU_CHOICES)}] (default 1): ").strip() or "1"
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(GPU_CHOICES):
                base = GPU_CHOICES[idx][1]
                break
        except ValueError:
            pass
        print("    invalid choice.")
    if base is None:
        return None
    n = prompt("how many GPUs", "1")
    return f"{base}:{n}" if n and n != "1" else base


def create_preset() -> dict:
    print("\n── new preset ──")
    name = prompt("name (e.g. 'sae-lens-a100')")
    if not name:
        print("aborted.")
        sys.exit(1)
    image = choose_image()
    gpu = choose_gpu()
    cpu = prompt("cpu cores", "2")
    memory = prompt("memory MiB", "4096")
    repo = prompt("repo (owner/repo, blank = none)", "") or None
    ref = prompt("ref (branch/tag/sha)", "main") if repo else None
    hours = prompt("session hours", "4")
    code_server = yesno("browser VS Code (code-server)?", True)
    marimo = yesno("Marimo notebooks?", True)
    settings_repo = prompt("global ~/.claude settings repo (blank = none)",
                           "ThatE10/claude-code-settings") or None
    use_gh_token = yesno("use `gh auth token` (private repo)?", False) if repo else False
    secrets_raw = prompt("extra Modal secrets (comma-separated, blank = none)",
                         ",".join(DEFAULT_EXTRA_SECRETS))
    extra_secrets = [s.strip() for s in (secrets_raw or "").split(",") if s.strip()]
    persist = yesno("persistent storage (survives relaunch)?", True)
    volume = None
    if persist:
        default_vol = slugify(name)
        volume = prompt("volume name (reuse across presets to share storage)",
                        default_vol) or default_vol
        volume = slugify(volume)
    return {
        "name": name,
        "image": image,
        "gpu": gpu,
        "cpu": float(cpu),
        "memory": int(memory),
        "repo": repo,
        "ref": ref,
        "hours": int(hours),
        "code_server": code_server,
        "marimo": marimo,
        "settings_repo": settings_repo,
        "use_gh_token": use_gh_token,
        "extra_secrets": extra_secrets,
        "persist": persist,
        "volume": volume,
        "count": 0,
    }


def summarize(p: dict) -> str:
    gpu = p["gpu"] or "cpu-only"
    img = p.get("image", DEFAULT_IMAGE)
    img_short = "slim" if img == "slim" else "dev-container" if img == DEFAULT_IMAGE else img
    repo = f" · {p['repo']}@{p.get('ref') or 'main'}" if p["repo"] else ""
    svc = [s for s, on in (("code", p.get("code_server", True)),
                           ("marimo", p.get("marimo", True))) if on]
    svc_s = "+" + "+".join(svc) if svc else "terminal-only"
    vol = preset_volume(p)
    store = f" · vol:{vol}" if vol else " · ephemeral"
    used = p.get("count", 0)
    return (f"{p['name']:<22} [{img_short}] {gpu:<12} {p['cpu']}c {p['memory']}Mi · "
            f"{p['hours']}h {svc_s}{store}{repo}  (used {used}×)")


def build_cmd(preset: dict) -> list[str]:
    cmd = [sys.executable, str(SCRIPT),
           "--image", preset.get("image", DEFAULT_IMAGE),
           "--cpu", str(preset["cpu"]),
           "--memory", str(preset["memory"]),
           "--hours", str(preset["hours"])]
    if preset["gpu"]:
        cmd += ["--gpu", preset["gpu"]]
    # Explicit --secret list; if empty, pass one empty --secret to clear defaults.
    extra_secrets = preset.get("extra_secrets", DEFAULT_EXTRA_SECRETS)
    if not extra_secrets:
        cmd += ["--secret", ""]
    else:
        for name in extra_secrets:
            cmd += ["--secret", name]
    # Browser VS Code / Marimo — on by default; pass the opt-out flags only.
    if not preset.get("code_server", True):
        cmd += ["--no-code-server"]
    if not preset.get("marimo", True):
        cmd += ["--no-marimo"]
    # Global ~/.claude settings repo — empty string disables the sync downstream.
    settings_repo = preset.get("settings_repo", "ThatE10/claude-code-settings")
    cmd += ["--claude-settings-repo", settings_repo or ""]
    # Persistent storage — each preset maps to its own Modal Volume set, keyed by
    # the preset's volume name, so relaunching a preset resumes its workspace /
    # ~/.claude / ~/.local and two different presets never race on one volume.
    # preset_volume() returns None for an explicitly ephemeral preset.
    vol = preset_volume(preset)
    if vol:
        cmd += ["--volume-name", vol]
    else:
        cmd += ["--no-persist"]
    if preset["repo"]:
        cmd += ["--repo", preset["repo"]]
        if preset.get("ref"):
            cmd += ["--ref", preset["ref"]]
        if preset.get("use_gh_token"):
            token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
            cmd += ["--github-token", token]
    return cmd


def launch(preset: dict) -> int:
    cmd = build_cmd(preset)
    # Don't echo the gh token if present.
    printable = [("<gh-token>" if i > 0 and cmd[i - 1] == "--github-token" else a)
                 for i, a in enumerate(cmd)]
    print("\n$ " + " ".join(printable))
    return subprocess.run(cmd).returncode


def cmd_list(presets: list[dict]) -> None:
    if not presets:
        print("no presets yet.")
        return
    for i, p in enumerate(sorted(presets, key=lambda x: -x.get("count", 0)), 1):
        print(f"  {i}) {summarize(p)}")


def delete_preset_at(presets: list[dict], idx: int) -> dict:
    """Remove the preset shown at 1-based position ``idx`` (in the list's current
    order) and return it. Raises IndexError if ``idx`` is out of range."""
    if not 1 <= idx <= len(presets):
        raise IndexError(idx)
    return presets.pop(idx - 1)


def cmd_forget(presets: list[dict], idx: int) -> None:
    presets.sort(key=lambda p: -p.get("count", 0))
    try:
        removed = delete_preset_at(presets, idx)
    except IndexError:
        print(f"no preset #{idx}.")
        sys.exit(1)
    save_presets(presets)
    print(f"forgot preset: {removed['name']}")


def remove_flow(presets: list[dict]) -> None:
    """Interactive removal: ask which preset to delete, then delete it. Operates
    on ``presets`` in its current (already-sorted) order so the numbers shown in
    the menu match. No-op on blank/invalid input."""
    if not presets:
        print("  no presets to remove.")
        return
    raw = input(f"  remove which? [1-{len(presets)}] (blank = cancel): ").strip()
    if not raw:
        print("  cancelled.")
        return
    try:
        removed = delete_preset_at(presets, int(raw))
    except (ValueError, IndexError):
        print(f"  no preset #{raw}.")
        return
    save_presets(presets)
    print(f"  forgot preset: {removed['name']}")


def menu(presets: list[dict]) -> None:
    # Loops so that removing a preset redraws the menu instead of exiting;
    # picking or creating a preset launches and exits.
    while True:
        presets.sort(key=lambda p: -p.get("count", 0))

        print("Claude Code on Modal — launcher\n")
        for i, p in enumerate(presets, 1):
            print(f"  {i}) {summarize(p)}")
        new_idx = len(presets) + 1
        print(f"  {new_idx}) [new preset]")
        remove_idx = new_idx + 1 if presets else None
        if remove_idx:
            print(f"  {remove_idx}) [remove a preset]")

        if not presets:
            choice = str(new_idx)
        else:
            choice = input(f"\nchoose [1-{remove_idx}] (default 1): ").strip() or "1"

        try:
            idx = int(choice) - 1
        except ValueError:
            print("invalid choice.")
            sys.exit(1)

        if remove_idx is not None and idx == remove_idx - 1:
            remove_flow(presets)
            print()  # spacer before the menu redraws
            continue

        if idx == len(presets):
            preset = create_preset()
            presets.append(preset)
        elif 0 <= idx < len(presets):
            preset = presets[idx]
        else:
            print("invalid choice.")
            sys.exit(1)

        preset["count"] = preset.get("count", 0) + 1
        save_presets(presets)
        sys.exit(launch(preset))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="Print presets and exit")
    parser.add_argument("--forget", type=int, metavar="N", help="Delete preset #N")
    args = parser.parse_args()

    presets = load_presets()
    if args.list:
        cmd_list(presets)
    elif args.forget is not None:
        cmd_forget(presets, args.forget)
    else:
        menu(presets)


if __name__ == "__main__":
    main()
