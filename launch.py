#!/usr/bin/env python3
"""
Interactive launcher for claude_code_modal.py.

Saves presets (GPU/CPU/RAM/repo/ref/hours) to
~/.config/claude-code-modal/presets.json and shows them sorted by
usage count so your most-used config is always at the top.

  python launch.py            # menu
  python launch.py --list     # print presets, exit
  python launch.py --forget N # delete preset #N
"""
import argparse
import json
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
    use_gh_token = yesno("use `gh auth token` (private repo)?", False) if repo else False
    secrets_raw = prompt("extra Modal secrets (comma-separated, blank = none)",
                         ",".join(DEFAULT_EXTRA_SECRETS))
    extra_secrets = [s.strip() for s in (secrets_raw or "").split(",") if s.strip()]
    return {
        "name": name,
        "image": image,
        "gpu": gpu,
        "cpu": float(cpu),
        "memory": int(memory),
        "repo": repo,
        "ref": ref,
        "hours": int(hours),
        "use_gh_token": use_gh_token,
        "extra_secrets": extra_secrets,
        "count": 0,
    }


def summarize(p: dict) -> str:
    gpu = p["gpu"] or "cpu-only"
    img = p.get("image", DEFAULT_IMAGE)
    img_short = "slim" if img == "slim" else "dev-container" if img == DEFAULT_IMAGE else img
    repo = f" · {p['repo']}@{p.get('ref') or 'main'}" if p["repo"] else ""
    used = p.get("count", 0)
    return (f"{p['name']:<22} [{img_short}] {gpu:<12} {p['cpu']}c {p['memory']}Mi · "
            f"{p['hours']}h{repo}  (used {used}×)")


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


def cmd_forget(presets: list[dict], idx: int) -> None:
    presets.sort(key=lambda p: -p.get("count", 0))
    if not 1 <= idx <= len(presets):
        print(f"no preset #{idx}.")
        sys.exit(1)
    removed = presets.pop(idx - 1)
    save_presets(presets)
    print(f"forgot preset: {removed['name']}")


def menu(presets: list[dict]) -> None:
    presets.sort(key=lambda p: -p.get("count", 0))

    print("Claude Code on Modal — launcher\n")
    for i, p in enumerate(presets, 1):
        print(f"  {i}) {summarize(p)}")
    new_idx = len(presets) + 1
    print(f"  {new_idx}) [new preset]")

    if not presets:
        choice = str(new_idx)
    else:
        choice = input(f"\nchoose [1-{new_idx}] (default 1): ").strip() or "1"

    try:
        idx = int(choice) - 1
    except ValueError:
        print("invalid choice.")
        sys.exit(1)

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
