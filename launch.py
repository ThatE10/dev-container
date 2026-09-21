#!/usr/bin/env python3
"""
Interactive launcher for claude_code_modal.py.

Saves presets (GPU/CPU/RAM/repo/ref/hours + VS Code / Marimo / settings-repo +
persistent volume) to ~/.config/claude-code-modal/presets.json and shows them
sorted by usage count so your most-used config is always at the top.

Presets are persistent by default: each one is backed by a named set of Modal
Volumes (workspace, ~/.local, ~/.claude, HF cache), so relaunching the same
preset restores its files, editor extensions, shell history, and Claude state —
no more ephemeral sandboxes. The volume name defaults to the preset name, but
any preset can point at any volume: creating a preset opens a volume picker
listing every volume your presets already use (so two presets can share one
set of storage volumes), and appending 'v' to a menu choice (e.g. '1v')
relaunches that preset on a different volume — one-off, or saved back to the
preset if you say so.

Presets come from three catalogs, each overriding the last by name: the
built-in presets.default.json shipped here, any project catalog named by
--presets / $DEVCONTAINER_PRESETS (so a repo can version the hardware profiles
its work needs), and your own presets.json, which always wins. Launching a
catalog preset adopts a local copy; until then the catalog stays its source of
truth and is never written back to.

Presets are editable: pick [edit a preset] from the menu (or append 'e' to a
menu choice, e.g. '1e', or run --edit N) to walk back through the same
questions that created it, with the current values as defaults — Enter keeps a
field, '-' clears an optional one.

Every preset line, the GPU picker and the launch confirmation carry an
estimated cost (hourly rate, plus the ceiling if the session runs its full
length) computed from Modal's list prices in GPU_HOURLY / CPU_CORE_HOURLY /
MEM_GIB_HOURLY — edit those if your account's rates differ.

  python launch.py            # menu ([new preset] / [edit a preset] / [remove a preset])
  python launch.py --list     # print presets, exit
  python launch.py --volumes  # print storage volumes and which presets use them
  python launch.py --edit N   # edit preset #N
  python launch.py --forget N # delete preset #N
  python launch.py --presets P  # merge in an extra preset catalog
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "claude-code-modal"
PRESETS_FILE = CONFIG_DIR / "presets.json"
SCRIPT = Path(__file__).resolve().parent / "claude_code_modal.py"
# Generic starting-point presets shipped with the launcher, so a fresh machine
# has something to launch before anyone has defined a preset of their own.
BUILTIN_PRESETS_FILE = Path(__file__).resolve().parent / "presets.default.json"
# Which catalog a preset came from. Internal — stripped before persisting.
SOURCE_KEY = "_source"

DEFAULT_IMAGE = "ghcr.io/thate10/dev-container:latest"
IMAGE_CHOICES = [
    ("dev-container (GPU, vLLM, torch pre-installed)", DEFAULT_IMAGE),
    ("slim (debian_slim — fast, CPU-only)", "slim"),
]
DEFAULT_EXTRA_SECRETS = ["huggingface-secret", "github-secret", "wandb-secret"]

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


# Modal's published on-demand rates (USD/hour), as of 2026-08. These drive the
# launcher's cost estimates only — Modal bills per second for what actually
# runs, and prices change; https://modal.com/pricing is the authority. Override
# a rate here if yours differ (committed/enterprise pricing).
GPU_HOURLY = {
    "T4": 0.59,
    "L4": 0.80,
    "A10G": 1.10,
    "L40S": 1.95,
    "A100-40GB": 2.10,
    "A100-80GB": 2.50,
    "H100": 3.95,
    "H200": 4.54,
}
CPU_CORE_HOURLY = 0.047     # $0.0000131 per core-second
MEM_GIB_HOURLY = 0.008      # $0.00000222 per GiB-second


def gpu_hourly(gpu: str | None) -> float | None:
    """Hourly GPU cost for a preset's ``gpu`` string ("H100", "A100-80GB:2"),
    None if the type isn't in the table (so callers can say "unknown" rather
    than quote a wrong number)."""
    if not gpu:
        return 0.0
    kind, _, count = gpu.partition(":")
    rate = GPU_HOURLY.get(kind)
    if rate is None:
        return None
    try:
        n = int(count) if count else 1
    except ValueError:
        n = 1
    return rate * n


def hourly_cost(p: dict) -> float | None:
    """Estimated USD/hour for a preset while its sandbox is running, or None if
    any component's rate is unknown."""
    gpu = gpu_hourly(p.get("gpu"))
    if gpu is None:
        return None
    return (gpu
            + float(p.get("cpu", 0)) * CPU_CORE_HOURLY
            + float(p.get("memory", 0)) / 1024 * MEM_GIB_HOURLY)


def session_cost(p: dict) -> float | None:
    """Estimated cost of running a preset for its full session length — the
    ceiling, since a sandbox that exits early stops billing."""
    rate = hourly_cost(p)
    return None if rate is None else rate * float(p.get("hours", 0))


def cost_note(p: dict) -> str:
    """One-line cost estimate for a preset, e.g. '~$2.79/h · ~$22.29 for 8h'."""
    rate, total = hourly_cost(p), session_cost(p)
    if rate is None:
        return "cost: unknown GPU rate"
    return f"~${rate:,.2f}/h · ~${total:,.2f} for {p.get('hours', 0)}h"


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


def known_volumes(presets: list[dict]) -> dict[str, list[str]]:
    """Map each persistent-volume name to the presets that use it."""
    vols: dict[str, list[str]] = {}
    for p in presets:
        v = preset_volume(p)
        if v:
            vols.setdefault(v, []).append(p["name"])
    return vols


# Public keys claude_code_modal.py auto-detects for sandbox SSH access.
SSH_PUBKEY_NAMES = ("id_ed25519.pub", "id_rsa.pub")


def has_ssh_key(ssh_dir: Path | None = None) -> bool:
    """True if a public key the sandbox will auto-detect already exists."""
    d = ssh_dir or (Path.home() / ".ssh")
    return any((d / name).exists() for name in SSH_PUBKEY_NAMES)


def ensure_ssh_key(ssh_dir: Path | None = None) -> bool:
    """Make sure a usable SSH public key exists so the sandbox's sshd accepts
    logins. If none is found, generate a passwordless ed25519 keypair. Returns
    True if a key is present (found or freshly created), False if generation
    failed (SSH login is then unavailable — the web terminal / `modal shell`
    still work). SSH auth is key-based; no password is set."""
    d = ssh_dir or (Path.home() / ".ssh")
    if has_ssh_key(d):
        return True
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    key_path = d / "id_ed25519"
    print(f"  no SSH key found — generating {key_path} for sandbox SSH access...")
    try:
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "",
             "-C", "claude-code-modal"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        detail = (getattr(e, "stderr", "") or str(e)).strip()
        print(f"  ⚠️  couldn't generate an SSH key ({detail}); SSH login will be "
              f"unavailable — use the web terminal or `modal shell` instead.")
        return False
    print(f"  created {key_path}.pub — it'll be installed as an authorized key (no password).")
    return True


def read_catalog(path: Path, source: str) -> list[dict]:
    """Read one preset catalog. A missing file is normal (no catalog is
    required); a malformed one warns and is skipped rather than taking the
    launcher down over a stray comma in a shared file."""
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  warning: ignoring preset catalog {path} ({e})", file=sys.stderr)
        return []
    if not isinstance(entries, list):
        print(f"  warning: ignoring preset catalog {path} (expected a JSON list)",
              file=sys.stderr)
        return []
    out = []
    for p in entries:
        if isinstance(p, dict) and p.get("name"):
            out.append({**p, SOURCE_KEY: source})
    return out


def catalog_paths(extra_paths: list[str] | None = None) -> list[tuple[Path, str]]:
    """Catalogs in increasing order of precedence.

    Built-ins ship with the launcher; project catalogs come from $DEVCONTAINER_PRESETS
    (os.pathsep-separated) or --presets, so a repo can version the hardware profiles
    its work actually needs; the user's own file always wins.
    """
    paths = [(BUILTIN_PRESETS_FILE, "builtin")]
    env = os.environ.get("DEVCONTAINER_PRESETS", "")
    for raw in env.split(os.pathsep) + list(extra_paths or []):
        if raw.strip():
            paths.append((Path(raw.strip()).expanduser(), "project"))
    paths.append((PRESETS_FILE, "user"))
    return paths


def load_presets(extra_paths: list[str] | None = None) -> list[dict]:
    """Merge every catalog by preset name; later sources override earlier ones."""
    merged: dict[str, dict] = {}
    for path, source in catalog_paths(extra_paths):
        for p in read_catalog(path, source):
            merged[p["name"]] = p
    return list(merged.values())


def save_presets(presets: list[dict]) -> None:
    """Persist only the user's own presets.

    Catalog presets are owned by the file they came from — writing them back
    would fork a private copy that silently stops tracking the shared catalog.
    Launching one *adopts* it (see menu()), which is the deliberate way to get a
    local copy you can edit.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    own = [{k: v for k, v in p.items() if k != SOURCE_KEY}
           for p in presets if p.get(SOURCE_KEY, "user") == "user"]
    PRESETS_FILE.write_text(json.dumps(own, indent=2))


def prompt(label: str, default: str | None = None) -> str | None:
    hint = f" [{default}]" if default is not None else ""
    value = input(f"  {label}{hint}: ").strip()
    return value if value else default


def prompt_optional(label: str, current: str | None) -> str | None:
    """Prompt for a value that may legitimately be empty.

    Enter keeps ``current``; typing ``-`` clears it. Without the explicit clear
    token an editable field could only ever be changed, never emptied, since a
    blank line already means "keep".
    """
    hint = f"{label} ('-' = none)" if current else label
    value = prompt(hint, current or "")
    return None if not value or value == "-" else value


def prompt_list(label: str, current: list[str] | None) -> list[str]:
    """Comma-separated list field. Enter keeps ``current``; ``-`` empties it."""
    raw = prompt_optional(label, ", ".join(current or []) or None)
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


def yesno(label: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    v = input(f"  {label} [{d}]: ").strip().lower()
    if not v:
        return default
    return v.startswith("y")


def _default_index(choices: list[tuple[str, str | None]], value: str | None) -> int:
    """1-based menu position of ``value`` in ``choices``, or 1 if it isn't there
    (an image typed by hand, say, or a GPU the table no longer lists)."""
    for i, (_, v) in enumerate(choices, 1):
        if v == value:
            return i
    return 1


def choose_image(default: str | None = None) -> str:
    print("\n  base image:")
    for i, (label, _) in enumerate(IMAGE_CHOICES, 1):
        print(f"    {i}) {label}")
    default_idx = _default_index(IMAGE_CHOICES, default)
    while True:
        raw = input(f"    choose [1-{len(IMAGE_CHOICES)}] "
                    f"(default {default_idx}): ").strip() or str(default_idx)
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(IMAGE_CHOICES):
                return IMAGE_CHOICES[idx][1]
        except ValueError:
            pass
        print("    invalid choice.")


def choose_gpu(default: str | None = None) -> str | None:
    # A stored GPU may carry a count ("A100-80GB:2"); split it so the menu can
    # preselect the type and the count prompt can preselect the number.
    kind, _, count = (default or "").partition(":")
    print("\n  GPU:")
    for i, (label, value) in enumerate(GPU_CHOICES, 1):
        rate = GPU_HOURLY.get(value)
        price = f"  ~${rate:,.2f}/h" if rate else ""
        print(f"    {i}) {label}{price}")
    default_idx = _default_index(GPU_CHOICES, kind or None)
    while True:
        raw = input(f"    choose [1-{len(GPU_CHOICES)}] "
                    f"(default {default_idx}): ").strip() or str(default_idx)
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
    n = prompt("how many GPUs", count if base == kind and count else "1")
    return f"{base}:{n}" if n and n != "1" else base


def choose_volume(presets: list[dict], default: str | None = None,
                  title: str = "storage volume",
                  current_preset: str | None = None) -> tuple[bool, str | None]:
    """Interactive picker for a preset's persistent storage.

    Lists every volume referenced by existing presets so storage can be reused
    across presets, plus an option to type a fresh name and one for no
    persistence at all. Also accepts a volume name typed directly at the
    prompt. Returns ``(persist, volume)``: ``(True, <slug>)``, or
    ``(False, None)`` for an ephemeral box.

    ``default`` preselects a volume name (None preselects ephemeral);
    ``current_preset`` is excluded from the "shares storage with" note so a
    preset relaunching on its own volume isn't warned about itself.
    """
    vols = known_volumes(presets)
    names = sorted(vols)

    def _pick(name: str) -> tuple[bool, str]:
        sharers = [u for u in vols.get(name, []) if u != current_preset]
        if sharers:
            print(f"    ↳ shares storage with: {', '.join(sharers)} — don't run "
                  f"two sandboxes on one volume at the same time.")
        return True, name

    print(f"\n  {title}:")
    for i, n in enumerate(names, 1):
        print(f"    {i}) {n}  (used by: {', '.join(vols[n])})")
    new_idx = len(names) + 1
    eph_idx = new_idx + 1
    print(f"    {new_idx}) [new volume name]")
    print(f"    {eph_idx}) [ephemeral — no persistent storage]")

    if default is None:
        default_idx = eph_idx
    elif default in names:
        default_idx = names.index(default) + 1
    else:
        default_idx = new_idx

    while True:
        raw = input(f"    choose [1-{eph_idx}] or type a name "
                    f"(default {default_idx}): ").strip() or str(default_idx)
        try:
            choice = int(raw)
        except ValueError:
            if not any(c.isalnum() for c in raw):  # would slugify to the fallback
                print("    invalid choice.")
                continue
            return _pick(slugify(raw))
        if 1 <= choice <= len(names):
            return _pick(names[choice - 1])
        if choice == new_idx:
            fallback = default if (default and default not in names) else None
            name = prompt("volume name", fallback)
            if name:
                return _pick(slugify(name))
            print("    a volume name is required (or pick the ephemeral option).")
            continue
        if choice == eph_idx:
            return False, None
        print("    invalid choice.")


def override_volume_flow(presets: list[dict], preset: dict) -> tuple[bool, str | None]:
    """Pick the storage volume for one launch of ``preset``.

    Shows the volume picker and returns the ``(persist, volume)`` pair to
    launch with. If the choice differs from the preset's stored storage,
    offers to save it back to the preset (the caller persists via
    ``save_presets``); declining keeps the override one-off.
    """
    current = preset_volume(preset)
    picked = choose_volume(
        presets, default=current, current_preset=preset["name"],
        title=(f"storage volume for this '{preset['name']}' launch "
               f"(currently: {'vol:' + current if current else 'ephemeral'})"))
    if picked != (current is not None, current):
        if yesno(f"save this storage choice to preset '{preset['name']}'?", False):
            preset["persist"], preset["volume"] = picked
    return picked


def preset_form(presets: list[dict], base: dict | None = None) -> dict:
    """Ask the full set of preset questions, offering ``base``'s values as
    defaults.

    Shared by creation (``base=None``) and editing, so a preset is retuned with
    exactly the questions that built it: Enter keeps a field, '-' clears an
    optional one. Keys ``base`` carries that aren't asked about here (usage
    count, launcher_ref, modal_secret, ...) are preserved.
    """
    cur = base or {}
    name = prompt("name (e.g. 'sae-lens-a100')", cur.get("name"))
    if not name:
        print("aborted.")
        sys.exit(1)
    image = choose_image(cur.get("image", DEFAULT_IMAGE) if base else None)
    gpu = choose_gpu(cur.get("gpu"))
    cpu = prompt("cpu cores", str(cur.get("cpu", 2)))
    memory = prompt("memory MiB", str(cur.get("memory", 4096)))
    repo = prompt_optional("repo (owner/repo, blank = none)", cur.get("repo"))
    ref = prompt("ref (branch/tag/sha)", cur.get("ref") or "main") if repo else None
    hours = prompt("session hours", str(cur.get("hours", 4)))
    code_server = yesno("browser VS Code (code-server)?", cur.get("code_server", True))
    marimo = yesno("Marimo notebooks?", cur.get("marimo", True))
    settings_repo = prompt_optional(
        "global ~/.claude settings repo (blank = none)",
        cur.get("settings_repo", "ThatE10/claude-code-settings") if not base
        else cur.get("settings_repo"))
    use_gh_token = (yesno("use `gh auth token` (private repo)?",
                          cur.get("use_gh_token", False)) if repo else False)
    with_launcher = yesno(
        "clone this launcher into the sandbox, so an agent inside can launch "
        "its own containers/jobs? (adds Modal credentials)",
        cur.get("with_launcher", False))
    extra_repos = prompt_list("other repos to clone alongside (owner/repo[@ref], "
                              "comma-separated, blank = none)", cur.get("extra_repos"))
    extra_secrets = prompt_list("extra Modal secrets (comma-separated, blank = none)",
                                cur.get("extra_secrets", DEFAULT_EXTRA_SECRETS))
    # An explicitly ephemeral preset keeps that as its default; everything else
    # preselects its own volume (or the new name's slug).
    vol_default = (None if base and not cur.get("persist", True)
                   else cur.get("volume") or slugify(name))
    persist, volume = choose_volume(
        presets, default=vol_default, current_preset=cur.get("name"),
        title="storage volume (reuse an existing one to share storage between presets)")
    return {
        **cur,
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
        "with_launcher": with_launcher,
        "extra_repos": extra_repos,
        "persist": persist,
        "volume": volume,
        "count": cur.get("count", 0),
    }


def create_preset(presets: list[dict]) -> dict:
    print("\n── new preset ──")
    return preset_form(presets)


def edit_preset(presets: list[dict], preset: dict) -> dict:
    """Re-run the preset questions over ``preset`` and update it in place.

    Editing adopts a shared catalog preset the same way launching does — the
    edits have to live somewhere, and that somewhere is the user's own file.
    """
    print(f"\n── edit preset: {preset['name']} ──")
    print("  (Enter keeps the current value; '-' clears an optional one)")
    updated = preset_form(presets, preset)
    updated[SOURCE_KEY] = "user"
    preset.clear()
    preset.update(updated)
    save_presets(presets)
    print(f"  saved preset: {preset['name']}")
    return preset


def edit_flow(presets: list[dict]) -> None:
    """Interactive edit: ask which preset to edit, then edit it. Operates on
    ``presets`` in its current (already-sorted) order so the numbers shown in
    the menu match. No-op on blank/invalid input."""
    if not presets:
        print("  no presets to edit.")
        return
    raw = input(f"  edit which? [1-{len(presets)}] (blank = cancel): ").strip()
    if not raw:
        print("  cancelled.")
        return
    try:
        idx = int(raw)
        if not 1 <= idx <= len(presets):
            raise IndexError(idx)
    except (ValueError, IndexError):
        print(f"  no preset #{raw}.")
        return
    edit_preset(presets, presets[idx - 1])


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
    src = p.get(SOURCE_KEY, "user")
    origin = "" if src == "user" else f" [{src}]"
    # Worth surfacing: these presets can spawn further sandboxes, which is both
    # a capability and a cost.
    extras = []
    if p.get("with_launcher"):
        extras.append("+launcher")
    if p.get("extra_repos"):
        extras.append(f"+{len(p['extra_repos'])} repo(s)")
    extra_s = " " + " ".join(extras) if extras else ""
    return (f"{p['name']:<22} [{img_short}] {gpu:<12} {p['cpu']}c {p['memory']}Mi · "
            f"{p['hours']}h {svc_s}{store}{repo}{extra_s}  ({cost_note(p)}) "
            f"(used {used}×){origin}")


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
    # Self-clone: put this launcher in the sandbox so an agent inside can
    # commission its own containers. Downstream this also attaches the Modal
    # credentials secret and sets $DEV_CONTAINER_DIR.
    if preset.get("with_launcher"):
        cmd += ["--with-launcher"]
        if preset.get("launcher_ref"):
            cmd += ["--launcher-ref", preset["launcher_ref"]]
        # A preset may opt out of the credential secret by storing "".
        if "modal_secret" in preset:
            cmd += ["--modal-secret", preset["modal_secret"] or ""]
    for spec in preset.get("extra_repos", []):
        cmd += ["--extra-repo", spec]
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
    # SSH is on by default downstream; make sure a key exists so login works
    # (auto-generates one if the machine has none). Skipped if the preset
    # disables SSH.
    if preset.get("ssh", True):
        ensure_ssh_key()
    cmd = build_cmd(preset)
    # Cost is the thing most worth seeing right before committing to a sandbox:
    # the hourly rate is what you pay while it runs, the total is the ceiling if
    # it runs the whole session. Stopping the sandbox early stops the meter.
    print(f"\n  estimated cost: {cost_note(preset)} (Modal list prices; "
          f"billed per second — stop the sandbox early to pay less)")
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


def cmd_volumes(presets: list[dict]) -> None:
    vols = known_volumes(presets)
    if not vols:
        print("no persistent volumes referenced by any preset.")
        return
    for name in sorted(vols):
        print(f"  {name:<24} used by: {', '.join(vols[name])}")
    print("\n(each name maps to Modal Volumes ccm-<name>-{workspace,claude,local})")


def delete_preset_at(presets: list[dict], idx: int) -> dict:
    """Remove the preset shown at 1-based position ``idx`` (in the list's current
    order) and return it. Raises IndexError if ``idx`` is out of range."""
    if not 1 <= idx <= len(presets):
        raise IndexError(idx)
    return presets.pop(idx - 1)


def cmd_edit(presets: list[dict], idx: int) -> None:
    presets.sort(key=lambda p: -p.get("count", 0))
    if not 1 <= idx <= len(presets):
        print(f"no preset #{idx}.")
        sys.exit(1)
    edit_preset(presets, presets[idx - 1])


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
        edit_idx = new_idx + 1 if presets else None
        remove_idx = new_idx + 2 if presets else None
        if presets:
            print(f"  {edit_idx}) [edit a preset]")
            print(f"  {remove_idx}) [remove a preset]")

        if not presets:
            choice = str(new_idx)
        else:
            choice = input(f"\nchoose [1-{remove_idx}] (default 1; append 'v' to pick "
                           f"the storage volume or 'e' to edit, e.g. '1v', "
                           f"'1e'): ").strip() or "1"

        # 'Nv' launches preset N on a volume you pick (one-off unless saved);
        # 'Ne' edits preset N instead of launching it. A bare suffix means the
        # default preset.
        pick_volume = choice.lower().endswith("v")
        edit_choice = choice.lower().endswith("e")
        if pick_volume or edit_choice:
            choice = choice[:-1].strip() or "1"

        try:
            idx = int(choice) - 1
        except ValueError:
            print("invalid choice.")
            sys.exit(1)

        if edit_choice:
            if 0 <= idx < len(presets):
                edit_preset(presets, presets[idx])
            else:
                print("invalid choice.")
            print()  # spacer before the menu redraws
            continue

        if edit_idx is not None and idx == edit_idx - 1:
            edit_flow(presets)
            print()
            continue

        if remove_idx is not None and idx == remove_idx - 1:
            remove_flow(presets)
            print()  # spacer before the menu redraws
            continue

        launch_volume = None  # (persist, volume) override for this launch only
        if idx == len(presets):
            preset = create_preset(presets)  # volume picked during creation
            presets.append(preset)
        elif 0 <= idx < len(presets):
            preset = presets[idx]
            if pick_volume:
                launch_volume = override_volume_flow(presets, preset)
        else:
            print("invalid choice.")
            sys.exit(1)

        # Launching a shared catalog preset adopts a local copy: it's now yours
        # to retune, and its usage count has somewhere to live. Until then the
        # catalog stays the single source of truth for it.
        preset[SOURCE_KEY] = "user"
        preset["count"] = preset.get("count", 0) + 1
        save_presets(presets)
        run = dict(preset)
        if launch_volume is not None:
            run["persist"], run["volume"] = launch_volume
        sys.exit(launch(run))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="Print presets and exit")
    parser.add_argument("--volumes", action="store_true",
                        help="Print storage volumes and which presets use them, then exit")
    parser.add_argument("--edit", type=int, metavar="N",
                        help="Edit preset #N (same questions that created it)")
    parser.add_argument("--forget", type=int, metavar="N", help="Delete preset #N")
    parser.add_argument("--presets", action="append", default=None, metavar="PATH",
                        help="Extra preset catalog to merge in, e.g. a catalog versioned "
                             "in a project repo (repeatable). Also read from "
                             "$DEVCONTAINER_PRESETS. Your own presets.json always wins.")
    args = parser.parse_args()

    presets = load_presets(args.presets)
    if args.list:
        cmd_list(presets)
    elif args.volumes:
        cmd_volumes(presets)
    elif args.edit is not None:
        cmd_edit(presets, args.edit)
    elif args.forget is not None:
        cmd_forget(presets, args.forget)
    else:
        menu(presets)


if __name__ == "__main__":
    main()
