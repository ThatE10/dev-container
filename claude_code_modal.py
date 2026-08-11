#!/usr/bin/env python3
"""
Claude Code server on Modal — browser VS Code + Marimo + a web terminal,
pre-authenticated via your Claude.ai subscription. No API keys. Uses the OAuth
credentials stored in your local macOS Keychain.

This mirrors the Docker dev container: the same three surfaces (code-server,
Marimo, terminal) and the same optional ~/.claude settings-repo sync. Usually
driven by launch.py (saved presets), but works standalone too.

One-time setup:
  pip install modal
  modal setup

  # Pull credentials straight from your macOS Keychain and store in Modal:
  modal secret create claude-code-secret \
    CLAUDE_CREDENTIALS="$(security find-generic-password -s 'Claude Code-credentials' -w)"

  # Required to pull the private dev-container image from ghcr.io (default base):
  modal secret create ghcr-secret \
    REGISTRY_USERNAME=<your-github-user> REGISTRY_PASSWORD=$(gh auth token)

  # Optional — for private GitHub repo clones:
  modal secret create github-secret GH_TOKEN=$(gh auth token)

Usage:
  python claude_code_modal.py                       # dev-container base (default)
  python claude_code_modal.py --image slim          # fast debian_slim base, no CUDA
  python claude_code_modal.py --repo owner/repo
  python claude_code_modal.py --gpu A100-80GB --cpu 8 --memory 32768
  python claude_code_modal.py --no-code-server --no-marimo   # terminal only

SSH (default on): sshd is exposed as a raw TCP tunnel so a plain `ssh` client
works directly — more stable for long sessions than `modal shell` or the ttyd
web terminal, which remain available as fallbacks. Auto-detects a local
~/.ssh/id_ed25519.pub or id_rsa.pub; override with --ssh-public-key /
--ssh-password, or disable with --no-ssh.

Persistence (default on): /root/workspace, /root/.claude, /root/.local, and a
shared HF cache are mounted as Modal Volumes, keyed by --volume-name (default:
derived from --repo, else "default") so re-launching the same name resumes
the same disk. Use --no-persist for a fully ephemeral sandbox.
"""
import argparse
import os
import re
import sys
from pathlib import Path

import modal

# ── Ports (each gets its own Modal HTTPS tunnel, except SSH — see below) ──────
PORT_TERMINAL = 7681   # ttyd web terminal
PORT_CODE     = 8443   # code-server (browser VS Code)
PORT_MARIMO   = 2719   # Marimo
PORT_SSH      = 22     # sshd — forwarded as a raw TCP (unencrypted) tunnel, not HTTPS

DEFAULT_IMAGE = "ghcr.io/thate10/dev-container:latest"

# This launcher's own repo. --with-launcher clones it into the sandbox so an
# agent working in there can commission further sandboxes instead of being
# limited to the one it is sitting in.
LAUNCHER_REPO = "ThatE10/dev-container"

# Layers added on top of any base — Node, Claude Code CLI, ttyd, code-server,
# sshd. code-server installs under /usr/local (on PATH); harmless if the base
# (the dev-container image) already ships it. Marimo is added via pip_install
# below. openssh-server + sshd_config tweaks are applied here (not just relied
# on from the dev-container Dockerfile) so `--image slim` and arbitrary
# `--image <tag>` bases get working SSH too.
_LAYER_CMDS = (
    "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -",
    "apt-get install -y nodejs openssh-server",
    "npm install -g @anthropic-ai/claude-code",
    "curl -fsSL https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64"
    " -o /usr/local/bin/ttyd && chmod +x /usr/local/bin/ttyd",
    "curl -fsSL https://code-server.dev/install.sh"
    " | sh -s -- --method standalone --prefix /usr/local",
    "mkdir -p /var/run/sshd",
    "sed -i "
    "-e 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' "
    "-e 's/#PasswordAuthentication yes/PasswordAuthentication yes/' "
    "-e 's/PasswordAuthentication no/PasswordAuthentication yes/' "
    "/etc/ssh/sshd_config",
    "printf '\\nX11Forwarding yes\\nAllowTcpForwarding yes\\n' >> /etc/ssh/sshd_config",
)


# Where build_startup writes a batch command before running it. Outside the
# workspace volume on purpose — it's per-launch scratch, not something to
# resurrect on the next sandbox that mounts the same volume.
BATCH_SCRIPT = "/tmp/ccm-batch.sh"


def _shq(s: str) -> str:
    """Single-quote a string for safe interpolation into the startup script."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def split_repo_ref(spec: str, default_ref: str = "main") -> tuple[str, str]:
    """Split an ``owner/repo[@ref]`` spec. Ref defaults to *default_ref*.

    rsplit on '@' so an SSH-style or token-bearing prefix can't confuse it —
    only the last '@' separates the ref.
    """
    if "@" in spec:
        repo, _, ref = spec.rpartition("@")
        if repo and ref:
            return repo, ref
    return spec, default_ref


def clone_block(repo: str, ref: str, clone_dir: str, label: str = "repo") -> list[str]:
    """Bash that clones *repo* at *ref* into *clone_dir*, idempotently.

    Runs at sandbox startup (not image build) so the checkout lands on the
    mounted persistent volume and survives restarts. Only clones once — on a
    persistent volume, later launches just resume the existing checkout.

    init + fetch-by-ref rather than `clone --branch`: `--branch` only accepts a
    branch or tag name, so a pinned commit SHA (what a reproducible batch run
    wants) can't be cloned that way. A shallow `fetch origin <ref>` accepts
    branches, tags *and* SHAs against GitHub, which serves both cases.
    """
    return [
        f'if [ ! -d "{clone_dir}/.git" ]; then',
        f'  CC_REF={_shq(ref)}',
        '  if [ -n "$GH_TOKEN" ]; then',
        f'    CC_URL="https://oauth2:${{GH_TOKEN}}@github.com/{repo}.git"',
        '  else',
        f'    CC_URL="https://github.com/{repo}.git"',
        '  fi',
        f'  mkdir -p "{clone_dir}"',
        f'  if GIT_ASKPASS=echo GIT_TERMINAL_PROMPT=0 sh -c \''
        f'git init -q "$1" && git -C "$1" remote add origin "$2" && '
        f'git -C "$1" fetch -q --depth 1 origin "$3"\' _ "{clone_dir}" "$CC_URL" "$CC_REF"; then',
        # A 40-hex ref is a commit: check it out detached. Anything else is a
        # branch/tag name, so materialise a local branch of that name — an
        # interactive sandbox expects to be *on* the branch it asked for.
        "    if printf '%s' \"$CC_REF\" | grep -qE '^[0-9a-f]{7,40}$'; then",
        f'      git -C "{clone_dir}" checkout -q --detach FETCH_HEAD',
        '    else',
        f'      git -C "{clone_dir}" checkout -q -B "$CC_REF" FETCH_HEAD',
        '    fi',
        f'    echo "[{label}] cloned {repo}@$CC_REF -> {clone_dir} ('
        f'$(git -C "{clone_dir}" rev-parse --short HEAD))"',
        '  else',
        f'    echo "[{label}] ERROR: fetch of {repo}@$CC_REF failed — check the ref '
        f'exists and GH_TOKEN has access" >&2',
        '  fi',
        'else',
        f'  echo "[{label}] {clone_dir} already present (persistent volume) — resuming as-is"',
        'fi',
    ]


def build_image(base: str, registry_secret_name: str | None,
                interactive_layers: bool = True) -> modal.Image:
    """Build the sandbox image.

    base = 'slim' → debian_slim (fast, CPU-only, no CUDA userspace).
    base = 'ghcr.io/...' or any tag → pull that image; needs a Modal registry secret
    for private registries. dev-container's ENTRYPOINT is stripped so we control
    startup directly instead of going through its SSH/banner setup — sshd is
    started explicitly at sandbox startup (see build_startup) instead.

    ``interactive_layers`` controls whether _LAYER_CMDS + Marimo are baked in.
    They exist solely for the interactive surfaces (code-server, ttyd, sshd, the
    Claude Code CLI), so a run-then-die batch job — which only executes its
    ``--run`` command — needs none of them: skipping them saves ~46s of build on
    every launch. It also avoids a hard failure on bases that ship no ``python``:
    ``.pip_install`` runs the *base image's* interpreter as ``python`` (we pull
    with ``add_python=None``), so an unconditional Marimo layer breaks any base
    that only provides ``python3`` — e.g. the vLLM-derived RSPD runtime.
    """
    if base == "slim":
        img = modal.Image.debian_slim().apt_install("curl", "git", "bash", "python3")
    else:
        secret = modal.Secret.from_name(registry_secret_name) if registry_secret_name else None
        img = (
            modal.Image.from_registry(base, add_python=None, secret=secret)
            .dockerfile_commands("ENTRYPOINT []")  # bypass dev-container's SSH+banner entrypoint
        )
    if interactive_layers:
        img = (
            img.run_commands(*_LAYER_CMDS)
            .pip_install("marimo")  # no-op if the base already has it (dev-container does)
        )
    return img.env({"TERM": "xterm-256color", "COLORTERM": "truecolor"})


def _not_found_error() -> type[BaseException]:
    """modal.exception.NotFoundError, or a type that never matches.

    Looked up rather than imported so the module still loads against an SDK
    (or the test stub) that doesn't expose it.
    """
    exc = getattr(modal, "exception", None)
    found = getattr(exc, "NotFoundError", None) if exc else None
    if isinstance(found, type) and issubclass(found, BaseException):
        return found

    class _Never(BaseException):
        pass

    return _Never


def _missing_secret_help(error: BaseException, attached: list[str]) -> str:
    """Turn 'Secret X not found' into something the reader can act on."""
    text = str(error)
    m = re.search(r"Secret '([^']+)' not found", text)
    name = m.group(1) if m else None

    lines = [f"\nModal could not find a secret this launch needs:\n  {text}\n"]
    if name:
        hint = ""
        if name == "modal-secret":
            # The one --with-launcher adds, and the one people are most likely
            # not to have: it is only needed so an agent *inside* the sandbox
            # can drive Modal, which is a niche thing to have set up already.
            hint = ("\n  This is the credential --with-launcher attaches so an agent inside\n"
                    "  the sandbox can launch its own containers. Note this is a Modal\n"
                    "  *Secret* — a named, server-side object — which is not the same as\n"
                    "  the local CLI credentials `modal token set` writes.\n"
                    "  Re-run without --with-launcher, or with --modal-secret '', to skip it.")
        lines.append(
            f"Create it with:\n"
            f"  modal secret create {name} KEY=value [KEY=value ...]\n"
            + (f"\nFor {name} specifically:\n"
               f"  modal secret create {name} \\\n"
               f"      MODAL_TOKEN_ID=ak-... MODAL_TOKEN_SECRET=as-...\n" if name == "modal-secret" else "")
            + hint
        )
    lines.append(f"\nSecrets attached to this launch: {', '.join(attached) or '(none)'}")
    lines.append("Drop one with --secret '' (clears the defaults) or name a different "
                 "one with --secret NAME.")
    return "\n".join(lines)


def resolve_ssh_public_key(raw: str | None) -> str | None:
    """raw may be actual key text, a path to a .pub file, or None (auto-detect
    the caller's local ~/.ssh/id_ed25519.pub / id_rsa.pub)."""
    if raw:
        p = Path(raw).expanduser()
        return p.read_text().strip() if p.exists() else raw.strip()
    for name in ("id_ed25519.pub", "id_rsa.pub"):
        p = Path.home() / ".ssh" / name
        if p.exists():
            return p.read_text().strip()
    return None


def build_startup(base_dir: str, work_dir: str, code_server: bool, marimo: bool, ssh: bool,
                  settings_repo: str | None, settings_branch: str,
                  repo: str | None, ref: str, clone_dir: str | None,
                  post_clone: str | None = None, run_cmd: str | None = None,
                  exit_on_finish: bool = False,
                  extra_repos: list[tuple[str, str, str]] | None = None,
                  extra_env: dict[str, str] | None = None) -> str:
    """Bash run at sandbox start: restore creds, import secrets, clone the repo
    (idempotent — skipped if already present on a persistent volume), optionally
    sync ~/.claude, bring up sshd, launch VS Code + Marimo in the background.

    Two shapes of sandbox come out of this:

    * **interactive** (``run_cmd=None``) — the historical behaviour. The last
      line is a foreground keep-alive (``exec sshd``, or ``exec ttyd`` with
      ``--no-ssh``) so the sandbox lives until its timeout.
    * **batch** (``run_cmd`` set) — ``run_cmd`` runs in ``work_dir`` with its
      output teed to ``<base_dir>/.last_run.log``. With ``exit_on_finish`` the
      script then exits with the command's status and the sandbox tears itself
      down (Slurm-style: commission, run, die). Without it, the run happens and
      *then* the normal keep-alive takes over — "run it, but leave me a shell to
      poke at the wreckage".

    ``post_clone`` is a repo-provided setup script (path relative to
    ``work_dir``, or absolute) run after the checkout and before anything else —
    the hook this launcher previously lacked, for things like linking a venv or
    an artifacts mount into the tree.

    ``extra_repos`` is a list of ``(repo, ref, dir)`` checked out beside the
    primary one; ``extra_env`` is exported both into this script and into
    ``~/.bashrc``, so an interactive shell or an agent started later sees it too.
    """
    lines = [
        f'mkdir -p "{base_dir}" ~/.claude',
        # Credentials: raw JSON blob from the macOS Keychain. On Linux without a
        # keychain, Claude Code falls back to ~/.claude/.credentials.json.
        "printf '%s' \"$CLAUDE_CREDENTIALS\" > ~/.claude/.credentials.json",
        # gh CLI / import-secrets.sh only check GITHUB_TOKEN; a github-secret that
        # exports GH_TOKEN would otherwise silently skip `gh auth login`.
        'export GITHUB_TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"',
        'export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"',
        # dev-container base ships this importer; slim doesn't (|| true keeps going).
        # Among other things this turns MODAL_TOKEN_ID/MODAL_TOKEN_SECRET into
        # ~/.modal.toml, which is what lets an agent inside the sandbox drive
        # Modal itself.
        "([ -x /opt/scripts/import-secrets.sh ] && /opt/scripts/import-secrets.sh || true)",
    ]

    # Exported here for the startup script *and* appended to ~/.bashrc, because
    # the thing that usually needs these is an agent or a shell started later —
    # long after this script's environment is gone.
    for key, value in (extra_env or {}).items():
        lines.append(f'export {key}={_shq(value)}')
        lines.append(
            f'grep -qxF "export {key}={value}" ~/.bashrc 2>/dev/null '
            f'|| echo "export {key}={value}" >> ~/.bashrc')

    if (repo and clone_dir) or extra_repos:
        lines.append("git config --global advice.detachedHead false")

    if repo and clone_dir:
        lines += clone_block(repo, ref, clone_dir, label="repo")

    # Side-by-side checkouts, cloned next to the primary one under base_dir.
    # The usual reason is a tool the agent inside needs to *run*, not just read
    # — see --with-launcher, which puts this launcher's own source here so an
    # agent in the sandbox can commission further sandboxes.
    for extra_repo, extra_ref, extra_dir in (extra_repos or []):
        lines += clone_block(extra_repo, extra_ref, extra_dir, label="extra-repo")

    if post_clone:
        # Relative paths resolve inside the checkout, so a repo can ship its own
        # setup script and name it as `--post-clone scripts/setup.sh`.
        path = post_clone if post_clone.startswith("/") else f"{work_dir}/{post_clone}"
        lines += [
            f'if [ -f {_shq(path)} ]; then',
            f'  echo "[post-clone] running {post_clone}"',
            f'  ( cd {_shq(work_dir)} && bash {_shq(path)} ) '
            f'|| echo "[post-clone] WARNING: {post_clone} exited non-zero" >&2',
            'else',
            f'  echo "[post-clone] WARNING: {post_clone} not found — skipping" >&2',
            'fi',
        ]

    if ssh:
        lines += [
            "mkdir -p /var/run/sshd",
            "ssh-keygen -A > /dev/null 2>&1 || true",  # idempotent: skips existing host keys
            'if [ -n "${SSH_PUBLIC_KEY:-}" ]; then',
            '  mkdir -p /root/.ssh && chmod 700 /root/.ssh',
            '  touch /root/.ssh/authorized_keys',
            '  grep -qxF "$SSH_PUBLIC_KEY" /root/.ssh/authorized_keys'
            ' || printf \'%s\\n\' "$SSH_PUBLIC_KEY" >> /root/.ssh/authorized_keys',
            '  chmod 600 /root/.ssh/authorized_keys',
            '  echo "[ssh] public key installed"',
            'fi',
            'if [ -n "${SSH_PASSWORD:-}" ]; then',
            '  printf \'root:%s\' "$SSH_PASSWORD" | chpasswd',
            "  sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/'"
            " /etc/ssh/sshd_config",
            '  echo "[ssh] password auth enabled"',
            'fi',
            'if [ -z "${SSH_PUBLIC_KEY:-}" ] && [ -z "${SSH_PASSWORD:-}" ]; then',
            '  echo "[ssh] WARNING: no SSH_PUBLIC_KEY or SSH_PASSWORD — sshd will start'
            ' but no login will succeed." >&2',
            'fi',
        ]

    if settings_repo:
        # Make ~/.claude a checkout of the settings repo and keep it fresh.
        # Best-effort: failures never block startup.
        lines += [
            f'CS_REPO="{settings_repo}"',
            f'CS_BRANCH="{settings_branch}"',
            'if [ -n "$GH_TOKEN" ]; then '
            'CS_URL="https://oauth2:$GH_TOKEN@github.com/$CS_REPO.git"; '
            'else CS_URL="https://github.com/$CS_REPO.git"; fi',
            'git config --global --add safe.directory ~/.claude 2>/dev/null || true',
            'if [ ! -d ~/.claude/.git ]; then '
            'T=$(mktemp -d); '
            'if git clone -q --branch "$CS_BRANCH" --no-checkout "$CS_URL" "$T/r" 2>/dev/null; then '
            'mv "$T/r/.git" ~/.claude/.git && git -C ~/.claude checkout -f "$CS_BRANCH" 2>/dev/null '
            '&& echo "[claude-settings] cloned $CS_REPO"; fi; rm -rf "$T"; fi',
            # Re-point origin + branch on EVERY launch, not just the first clone,
            # so changing --claude-settings-repo/--claude-settings-branch on a
            # reused --volume-name actually takes effect instead of silently
            # sticking to whatever origin/branch this volume's ~/.claude was
            # first populated with.
            'if [ -d ~/.claude/.git ]; then '
            'git -C ~/.claude remote set-url origin "$CS_URL" 2>/dev/null '
            '|| git -C ~/.claude remote add origin "$CS_URL" 2>/dev/null; '
            'git -C ~/.claude fetch -q origin "$CS_BRANCH" 2>/dev/null '
            '&& git -C ~/.claude checkout -q -B "$CS_BRANCH" "origin/$CS_BRANCH" 2>/dev/null '
            '&& echo "[claude-settings] synced ~/.claude to $CS_REPO@$CS_BRANCH" '
            '|| echo "[claude-settings] WARNING: could not sync $CS_REPO@$CS_BRANCH'
            ' (keeping existing checkout)" >&2; fi',
        ]

    if code_server:
        lines.append(
            f"code-server --auth none --bind-addr 0.0.0.0:{PORT_CODE} "
            f"--disable-telemetry {work_dir} > /tmp/code-server.log 2>&1 &"
        )
    if marimo:
        lines.append(
            f"( cd {work_dir} && marimo edit --headless --host 0.0.0.0 "
            f"--port {PORT_MARIMO} --no-token > /tmp/marimo.log 2>&1 ) &"
        )

    if run_cmd:
        # The command is written to a file via a quoted heredoc rather than
        # interpolated into a command line: the delimiter being quoted means
        # bash copies the body through verbatim (no expansion at *write* time,
        # while the script still expands normally when it runs), so arbitrary
        # quoting in run_cmd can't break out into the startup script.
        lines += [
            f"cat > {BATCH_SCRIPT} <<'CCM_BATCH_EOF'",
            run_cmd,
            "CCM_BATCH_EOF",
            "set -o pipefail",
            f'echo "[batch] running in {work_dir}"',
            f'( cd {_shq(work_dir)} && bash {BATCH_SCRIPT} ) 2>&1 '
            f'| tee {_shq(base_dir + "/.last_run.log")}',
            "CCM_RC=${PIPESTATUS[0]}",
            'echo "[batch] exit=$CCM_RC"',
        ]
        if exit_on_finish:
            # Ends the only foreground process, so Modal tears the sandbox down
            # and stops billing. The exit status becomes the sandbox's.
            lines.append('exit "$CCM_RC"')
        else:
            lines.append('echo "[batch] --exit-on-finish not set — '
                         'holding the sandbox open for inspection"')

    # Exactly one process stays in the foreground to keep the sandbox alive.
    # sshd is the default (more stable than the ttyd terminal or `modal shell`
    # for long-running work) — ttyd still runs in the background for the
    # browser terminal. With --no-ssh, ttyd takes over the foreground instead.
    # A batch run that exits on finish deliberately has no keep-alive at all.
    if not (run_cmd and exit_on_finish):
        if ssh:
            lines.append(f"ttyd -W -p {PORT_TERMINAL} bash > /tmp/ttyd.log 2>&1 &")
            lines.append(f"exec /usr/sbin/sshd -D -e -p {PORT_SSH}")
        else:
            lines.append(f"exec ttyd -W -p {PORT_TERMINAL} bash")
    return "\n".join(lines)


# Modal secrets attached by default (in addition to claude-code-secret which is
# always attached). Missing secrets will cause sandbox creation to fail with a
# clear error — remove from the list or create via `modal secret create`.
DEFAULT_SECRETS = ["huggingface-secret", "github-secret"]

# ── Persistent storage ────────────────────────────────────────────────────────
# When --persist is on (default) main() mounts a Modal Volume trio, mirroring the
# Docker dev container: BASE_DIR (workspace), /root/.claude, and /root/.local,
# plus a shared HuggingFace weights cache. They're named ccm-<slug>-<suffix>, so
# reusing the same --volume-name resumes the same disks across restarts; Modal
# background-commits them while the sandbox runs and on shutdown.
#
# Base directory for the persistent workspace volume. Repo clones land in
# BASE_DIR/code; everything else in BASE_DIR is yours to use as scratch space
# that survives sandbox restarts (unlike the rest of the filesystem).
BASE_DIR = "/root/workspace"


def volume_slug(raw: str | None) -> str:
    """Normalize a volume-name (or --repo / 'default' fallback) into a slug.

    Lowercases, turns every non-alphanumeric character (including the '/' in
    'owner/repo') into '-', collapses the resulting runs, and trims stray
    dashes. Modal Volume names allow [A-Za-z0-9._-]; we stick to alphanumerics
    and single dashes for readability. Empty/blank input falls back to 'default'.
    """
    slug = (raw or "").strip().lower()
    slug = "".join(c if (c.isalnum() or c == "-") else "-" for c in slug)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "default"


def main(
    repo: str | None,
    ref: str,
    hours: int,
    github_token: str | None,
    gpu: str | None = None,
    cpu: float | None = None,
    memory: int | None = None,
    image_base: str = DEFAULT_IMAGE,
    registry_secret: str | None = "ghcr-secret",
    extra_secrets: list[str] | None = None,
    code_server: bool = True,
    marimo: bool = True,
    settings_repo: str | None = "ThatE10/claude-code-settings",
    settings_branch: str = "main",
    ssh: bool = True,
    ssh_public_key: str | None = None,
    ssh_password: str | None = None,
    persist: bool = True,
    volume_name: str | None = None,
    hf_cache_volume: str = "claude-code-hf-cache",
    run_cmd: str | None = None,
    exit_on_finish: bool = False,
    post_clone: str | None = None,
    extra_volumes: dict[str, str] | None = None,
    detach: bool = False,
    extra_repos: list[str] | None = None,
    with_launcher: bool = False,
    launcher_ref: str = "main",
    modal_secret: str | None = "modal-secret",
) -> int:
    """Launch a sandbox. Returns the batch command's exit status when this is a
    run-then-die launch that we waited on, else 0."""
    app = modal.App.lookup("claude-code-server", create_if_missing=True)

    # A run-then-die sandbox outlives nothing, so the interactive services are
    # pure overhead — and their tunnels don't exist to print. Use --run without
    # --exit-on-finish when you want the run *and* a shell afterwards.
    batch_exit = bool(run_cmd) and exit_on_finish
    if batch_exit:
        code_server = marimo = ssh = False

    # A registry secret only makes sense for a real registry pull.
    secret_name = registry_secret if image_base != "slim" else None
    work_image = build_image(image_base, secret_name, interactive_layers=not batch_exit)

    # Repo clones land on the persistent workspace volume (BASE_DIR/code) so they
    # survive restarts; without a repo the editor/terminal open at BASE_DIR.
    clone_dir = f"{BASE_DIR}/code" if repo else None
    work_dir = clone_dir or BASE_DIR

    # Side-by-side checkouts. --with-launcher is the common case: it puts this
    # launcher's own source in the sandbox so an agent inside can commission
    # further sandboxes — which needs the code *and* Modal credentials, hence
    # the secret below.
    specs = list(extra_repos or [])
    extra_env: dict[str, str] = {}
    if with_launcher:
        if not any(split_repo_ref(s)[0] == LAUNCHER_REPO for s in specs):
            specs.append(f"{LAUNCHER_REPO}@{launcher_ref}")
        if modal_secret:
            extra_secrets = list(extra_secrets or [])
            if modal_secret not in extra_secrets:
                extra_secrets.append(modal_secret)

    resolved_extra_repos: list[tuple[str, str, str]] = []
    for spec in specs:
        spec_repo, spec_ref = split_repo_ref(spec)
        name = spec_repo.rsplit("/", 1)[-1]
        # Beside the primary checkout, not inside it: a tool that expects to sit
        # next to the project (as RSPD's launcher does) then finds it, and git
        # never sees a nested repo.
        target = f"{BASE_DIR}/{name}"
        resolved_extra_repos.append((spec_repo, spec_ref, target))
        if spec_repo == LAUNCHER_REPO:
            extra_env["DEV_CONTAINER_DIR"] = target

    secrets = [modal.Secret.from_name("claude-code-secret")]  # CLAUDE_CREDENTIALS
    attached_names = ["claude-code-secret"]
    for name in (extra_secrets or []):
        secrets.append(modal.Secret.from_name(name))
        attached_names.append(name)
    if github_token or os.environ.get("GH_TOKEN"):
        secrets.append(modal.Secret.from_dict({"GH_TOKEN": github_token or os.environ["GH_TOKEN"]}))
        attached_names.append("GH_TOKEN (ephemeral)")

    resolved_key = resolve_ssh_public_key(ssh_public_key) if ssh else None
    if ssh:
        ssh_secret_dict = {}
        if resolved_key:
            ssh_secret_dict["SSH_PUBLIC_KEY"] = resolved_key
        if ssh_password:
            ssh_secret_dict["SSH_PASSWORD"] = ssh_password
        if ssh_secret_dict:
            secrets.append(modal.Secret.from_dict(ssh_secret_dict))
            attached_names.append("SSH auth (ephemeral)")
        else:
            print("⚠️  No SSH key found (~/.ssh/id_ed25519.pub or id_rsa.pub) and no "
                  "--ssh-password given — sshd will start but no login will succeed.")
    print(f"Attaching secrets: {', '.join(attached_names)}")

    resource_kwargs = {}
    if gpu:
        resource_kwargs["gpu"] = gpu
    if cpu is not None:
        resource_kwargs["cpu"] = cpu
    if memory is not None:
        resource_kwargs["memory"] = memory

    # Persistent storage. Each --volume-name (falling back to the repo name, then
    # "default") maps to its own workspace/claude/local volume trio; the HF weights
    # cache is shared across sandboxes since it's read-mostly after first download.
    volumes = {}
    slug = None
    if persist:
        slug = volume_slug(volume_name or repo)
        volumes = {
            BASE_DIR: modal.Volume.from_name(f"ccm-{slug}-workspace", create_if_missing=True),
            "/root/.claude": modal.Volume.from_name(f"ccm-{slug}-claude", create_if_missing=True),
            "/root/.local": modal.Volume.from_name(f"ccm-{slug}-local", create_if_missing=True),
            "/root/.cache/huggingface": modal.Volume.from_name(hf_cache_volume, create_if_missing=True),
        }
        print(f"Persistent storage: ccm-{slug}-{{workspace,claude,local}} + shared "
              f"'{hf_cache_volume}'. Reuse --volume-name {slug} to resume; don't run two "
              f"sandboxes with the same --volume-name at once (concurrent writers race).")
    else:
        print("Persistent storage: disabled (--no-persist) — sandbox filesystem is ephemeral.")

    # Caller-supplied mounts, e.g. a project's shared artifacts volume. Kept
    # deliberately opaque — this launcher doesn't need to know what they hold.
    # Applied after the standard set so an explicit --mount wins on a collision.
    for mount_path, vol_name in (extra_volumes or {}).items():
        volumes[mount_path] = modal.Volume.from_name(vol_name, create_if_missing=True)
        print(f"Extra volume: {vol_name} -> {mount_path}")

    startup = build_startup(BASE_DIR, work_dir, code_server, marimo, ssh,
                            settings_repo, settings_branch, repo, ref, clone_dir,
                            post_clone=post_clone, run_cmd=run_cmd,
                            exit_on_finish=exit_on_finish,
                            extra_repos=resolved_extra_repos, extra_env=extra_env)

    # ttyd only runs as part of the keep-alive, which a run-then-die job has
    # none of — asking Modal to tunnel a port nothing listens on is waste.
    ports = [] if batch_exit else [PORT_TERMINAL]
    if code_server:
        ports.append(PORT_CODE)
    if marimo:
        ports.append(PORT_MARIMO)

    resource_summary = (
        f"image={image_base} gpu={gpu or 'none'} cpu={cpu or 'default'} "
        f"mem={memory or 'default'}MiB storage={'ccm-' + slug if persist else 'ephemeral'} "
        f"ssh={'on' if ssh else 'off'}"
    )
    what = "batch job" if batch_exit else "Claude Code"
    print(f"Launching {what} on Modal (timeout: {hours}h, {resource_summary})...")
    sandbox_kwargs = dict(
        encrypted_ports=ports,
        secrets=secrets,
        timeout=hours * 3600,
        image=work_image,
        app=app,
        workdir=work_dir,
        volumes=volumes,
        **resource_kwargs,
    )
    if ssh:
        # Raw TCP, not TLS-wrapped like encrypted_ports — a plain `ssh` client
        # connects directly, unlike the encrypted tunnels which need a TLS-aware
        # client.
        sandbox_kwargs["unencrypted_ports"] = [PORT_SSH]

    with modal.enable_output():
        try:
            sandbox = modal.Sandbox.create("bash", "-c", startup, **sandbox_kwargs)
        except _not_found_error() as e:
            # Secrets resolve lazily, so a missing one surfaces here as a raw
            # SDK traceback naming a secret the user never chose — the defaults
            # and --with-launcher both attach secrets by name. Say which names
            # were attached and how to create the missing one.
            raise SystemExit(_missing_secret_help(e, attached_names)) from None

    if batch_exit:
        # No services, so no tunnels to print — the sandbox is a job, not a box.
        print(f"\n  Sandbox      : {sandbox.object_id}")
        print(f"  Logs         : modal container logs {sandbox.object_id}")
        if detach:
            print("  Detached     — not waiting; poll the command above for progress.")
            return 0
        print("  Waiting for the run to finish (Ctrl-C detaches; the job keeps going)...")
        sandbox.wait()
        rc = sandbox.returncode
        print(f"  Exit status  : {rc}")
        if persist:
            print(f"  Artifacts    : on the mounted volumes (ccm-{slug}, "
                  f"{', '.join(extra_volumes or {}) or 'no extra mounts'})")
        return rc if isinstance(rc, int) else 0

    tunnels = sandbox.tunnels()
    print(f"\n  Web terminal : {tunnels[PORT_TERMINAL].url}")
    print(f"               (bash shell, type `claude` — already authenticated)")
    if code_server:
        print(f"  VS Code      : {tunnels[PORT_CODE].url}")
    if marimo:
        print(f"  Marimo       : {tunnels[PORT_MARIMO].url}")
    if ssh:
        ssh_host, ssh_port = tunnels[PORT_SSH].tcp_socket
        print(f"  SSH          : ssh root@{ssh_host} -p {ssh_port}")
    shell_note = "  (fallback if SSH won't connect)" if ssh else ""
    print(f"  Shell access : modal shell {sandbox.object_id}{shell_note}")
    if persist:
        print(f"  Persistent   : ccm-{slug} volumes → workspace, ~/.claude, ~/.local, HF cache "
              f"(relaunch with --volume-name {slug} to resume)")
    else:
        print(f"  Persistent   : none (ephemeral — drop --no-persist to keep work across runs)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Claude Code on Modal (web terminal + VS Code + Marimo)")
    parser.add_argument("--repo", default=None, help="GitHub repo to clone (owner/repo)")
    parser.add_argument("--ref", default="main", help="Branch / tag / SHA (default: main)")
    parser.add_argument("--hours", type=int, default=4, help="Session timeout in hours (default: 4)")
    parser.add_argument("--github-token", default=None, help="GitHub PAT — tip: $(gh auth token)")
    parser.add_argument("--gpu", default=None,
                        help="GPU type: T4, L4, A10G, L40S, A100-40GB, A100-80GB, H100, H200. "
                             "Append :N for multi-GPU (e.g. H100:2). Omit for CPU only.")
    parser.add_argument("--cpu", type=float, default=None, help="CPU cores (fractional OK)")
    parser.add_argument("--memory", type=int, default=None, help="Memory in MiB")
    parser.add_argument("--image", default=DEFAULT_IMAGE,
                        help=f"Base image. 'slim' = debian_slim (fast, CPU-only). "
                             f"Default: {DEFAULT_IMAGE}")
    parser.add_argument("--registry-secret", default="ghcr-secret",
                        help="Modal secret name for private registry auth (ignored for --image slim)")
    parser.add_argument("--secret", action="append", default=None, metavar="NAME",
                        dest="secrets",
                        help=f"Modal secret to attach to the sandbox (repeatable). "
                             f"Defaults to: {' '.join(DEFAULT_SECRETS)}. "
                             f"Pass --secret '' once to attach none.")
    parser.add_argument("--no-code-server", dest="code_server", action="store_false",
                        help="Don't start browser VS Code")
    parser.add_argument("--no-marimo", dest="marimo", action="store_false",
                        help="Don't start Marimo")
    parser.add_argument("--claude-settings-repo", default="ThatE10/claude-code-settings",
                        help="Settings repo synced into ~/.claude (empty to disable)")
    parser.add_argument("--claude-settings-branch", default="main",
                        help="Settings repo branch (default: main)")
    parser.add_argument("--no-ssh", dest="ssh", action="store_false",
                        help="Don't start sshd (falls back to ttyd as the foreground process, "
                             "and `modal shell` as the only remote-shell option)")
    parser.add_argument("--ssh-public-key", default=None,
                        help="Public key (raw text or path to a .pub file) allowed to SSH in as "
                             "root. Default: auto-detect ~/.ssh/id_ed25519.pub or id_rsa.pub")
    parser.add_argument("--ssh-password", default=None,
                        help="Also (or instead) enable SSH password auth for root")
    parser.add_argument("--no-persist", dest="persist", action="store_false",
                        help="Ephemeral sandbox storage only — no Modal Volumes, nothing survives "
                             "a restart")
    parser.add_argument("--volume-name", default=None,
                        help="Slug identifying this sandbox's persistent volumes (workspace, "
                             "~/.claude, ~/.local). Default: derived from --repo, else 'default'. "
                             "Give concurrently-running sandboxes distinct names — two sandboxes "
                             "sharing a volume-name will race on writes.")
    parser.add_argument("--extra-repo", action="append", default=None,
                        metavar="owner/repo[@ref]", dest="extra_repos",
                        help="Clone another repo beside the main checkout, at "
                             "<workspace>/<repo-name> (repeatable). Ref defaults to main.")
    parser.add_argument("--with-launcher", action="store_true",
                        help=f"Clone {LAUNCHER_REPO} into the sandbox and attach Modal "
                             "credentials, so an agent inside can launch its own "
                             "sandboxes and batch jobs. Sets $DEV_CONTAINER_DIR.")
    parser.add_argument("--launcher-ref", default="main",
                        help="Ref for --with-launcher's checkout (default: main)")
    parser.add_argument("--modal-secret", default="modal-secret", metavar="NAME",
                        help="Modal secret holding MODAL_TOKEN_ID / MODAL_TOKEN_SECRET, "
                             "attached by --with-launcher so the inner agent can "
                             "authenticate. Pass '' to skip (default: modal-secret)")
    parser.add_argument("--run", default=None, metavar="CMD",
                        help="Run CMD in the checkout instead of just parking on a shell. "
                             "Output is teed to <workspace>/.last_run.log. Combine with "
                             "--exit-on-finish for a run-then-die batch job; without it the "
                             "command runs and the sandbox then stays up for inspection.")
    parser.add_argument("--exit-on-finish", action="store_true",
                        help="With --run: tear the sandbox down as soon as the command exits, "
                             "and exit with its status. Disables VS Code / Marimo / SSH, which "
                             "a job that outlives nothing has no use for.")
    parser.add_argument("--detach", action="store_true",
                        help="With --run --exit-on-finish: don't block waiting for the job")
    parser.add_argument("--post-clone", default=None, metavar="PATH",
                        help="Script run after the checkout, before anything else. Relative "
                             "paths resolve inside the checkout (e.g. scripts/setup.sh). "
                             "Failures warn but don't abort startup.")
    parser.add_argument("--mount", action="append", default=None, metavar="NAME:/path",
                        dest="mounts",
                        help="Mount an additional Modal Volume NAME at /path (repeatable). "
                             "Created if missing. Use a path outside the workspace volume — "
                             "volumes are not mounted inside one another.")
    parser.add_argument("--hf-cache-volume", default="claude-code-hf-cache",
                        help="Modal Volume name for the shared HuggingFace weights cache "
                             "(default: claude-code-hf-cache; shared across all sandboxes since "
                             "it's mostly read-only after first download)")
    args = parser.parse_args()
    # None → defaults; [''] → user explicitly cleared; otherwise use provided list.
    if args.secrets is None:
        extra = DEFAULT_SECRETS
    elif args.secrets == [""]:
        extra = []
    else:
        extra = [s for s in args.secrets if s]
    extra_volumes = {}
    for spec in (args.mounts or []):
        # NAME:/path — rsplit so a volume name containing ':' still works.
        name, _, path = spec.rpartition(":")
        if not name or not path.startswith("/"):
            parser.error(f"--mount expects NAME:/absolute/path, got {spec!r}")
        extra_volumes[path] = name

    if args.exit_on_finish and not args.run:
        parser.error("--exit-on-finish only makes sense with --run")

    sys.exit(main(args.repo, args.ref, args.hours, args.github_token,
                  gpu=args.gpu, cpu=args.cpu, memory=args.memory,
                  image_base=args.image, registry_secret=args.registry_secret,
                  extra_secrets=extra,
                  code_server=args.code_server, marimo=args.marimo,
                  settings_repo=args.claude_settings_repo or None,
                  settings_branch=args.claude_settings_branch,
                  ssh=args.ssh, ssh_public_key=args.ssh_public_key,
                  ssh_password=args.ssh_password,
                  persist=args.persist, volume_name=args.volume_name,
                  hf_cache_volume=args.hf_cache_volume,
                  run_cmd=args.run, exit_on_finish=args.exit_on_finish,
                  post_clone=args.post_clone, extra_volumes=extra_volumes,
                  detach=args.detach,
                  extra_repos=args.extra_repos, with_launcher=args.with_launcher,
                  launcher_ref=args.launcher_ref,
                  modal_secret=args.modal_secret or None))
