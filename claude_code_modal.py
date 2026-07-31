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
from pathlib import Path

import modal

# ── Ports (each gets its own Modal HTTPS tunnel, except SSH — see below) ──────
PORT_TERMINAL = 7681   # ttyd web terminal
PORT_CODE     = 8443   # code-server (browser VS Code)
PORT_MARIMO   = 2719   # Marimo
PORT_SSH      = 22     # sshd — forwarded as a raw TCP (unencrypted) tunnel, not HTTPS

DEFAULT_IMAGE = "ghcr.io/thate10/dev-container:latest"

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


def build_image(base: str, registry_secret_name: str | None) -> modal.Image:
    """Build the sandbox image.

    base = 'slim' → debian_slim (fast, CPU-only, no CUDA userspace).
    base = 'ghcr.io/...' or any tag → pull that image; needs a Modal registry secret
    for private registries. dev-container's ENTRYPOINT is stripped so we control
    startup directly instead of going through its SSH/banner setup — sshd is
    started explicitly at sandbox startup (see build_startup) instead.
    """
    if base == "slim":
        img = modal.Image.debian_slim().apt_install("curl", "git", "bash", "python3")
    else:
        secret = modal.Secret.from_name(registry_secret_name) if registry_secret_name else None
        img = (
            modal.Image.from_registry(base, add_python=None, secret=secret)
            .dockerfile_commands("ENTRYPOINT []")  # bypass dev-container's SSH+banner entrypoint
        )
    return (
        img.run_commands(*_LAYER_CMDS)
        .pip_install("marimo")  # no-op if the base already has it (dev-container does)
        .env({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
    )


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
                  repo: str | None, ref: str, clone_dir: str | None) -> str:
    """Bash run at sandbox start: restore creds, import secrets, clone the repo
    (idempotent — skipped if already present on a persistent volume), optionally
    sync ~/.claude, bring up sshd, launch VS Code + Marimo in the background."""
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
        "([ -x /opt/scripts/import-secrets.sh ] && /opt/scripts/import-secrets.sh || true)",
    ]

    if repo and clone_dir:
        # Runs at startup (not image build) so the clone lands on the mounted
        # persistent volume and survives restarts. Only clones once — on a
        # persistent volume, later launches just resume the existing checkout.
        lines += [
            "git config --global advice.detachedHead false",
            f'if [ ! -d "{clone_dir}/.git" ]; then',
            '  if [ -n "$GH_TOKEN" ]; then',
            f'    GIT_ASKPASS=echo git clone --quiet --depth 1 --branch {ref} '
            f'"https://oauth2:${{GH_TOKEN}}@github.com/{repo}.git" "{clone_dir}"',
            '  else',
            f'    GIT_TERMINAL_PROMPT=0 git clone --quiet --depth 1 --branch {ref} '
            f'"https://github.com/{repo}.git" "{clone_dir}"',
            '  fi',
            # $? here is the exit status of whichever clone command the
            # if/else above just ran — don't claim success unconditionally.
            '  if [ $? -eq 0 ]; then',
            f'    echo "[repo] cloned {repo}@{ref} -> {clone_dir}"',
            '  else',
            f'    echo "[repo] ERROR: git clone of {repo}@{ref} failed — check the ref '
            f'exists and GH_TOKEN has access" >&2',
            '  fi',
            'else',
            f'  echo "[repo] {clone_dir} already present (persistent volume) — resuming as-is"',
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

    # Exactly one process stays in the foreground to keep the sandbox alive.
    # sshd is the default (more stable than the ttyd terminal or `modal shell`
    # for long-running work) — ttyd still runs in the background for the
    # browser terminal. With --no-ssh, ttyd takes over the foreground instead.
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
# Mirrors the Docker dev container's named volumes. Each path below is backed by
# a named Modal Volume so its contents survive across sandbox runs — stop the
# sandbox, relaunch with the same volume name, and your workspace, editor
# extensions, shell history, and Claude Code state are all still there. Modal
# background-commits these volumes while the sandbox runs and on shutdown, so a
# session that hits its timeout still persists its work.
#
# suffix → mount path. The Volume is named "<base>-<suffix>", so one base name
# maps to a consistent set of volumes; reuse the base name to reuse the storage.
PERSIST_MOUNTS = {
    "workspace": "/root/workspace",           # project code + per-project .venvs
    "local":     "/root/.local",              # code-server ext/settings, pip --user, ~/.local/bin, shell history
    "claude":    "/root/.claude",             # Claude Code config & chat history
    "hf":        "/root/.cache/huggingface",  # downloaded model weights
}


def build_volumes(volume: str | None) -> dict:
    """Named Modal Volumes for persistent storage, keyed off a base name.

    Returns {mount_path: modal.Volume}. Empty when ``volume`` is None → a fully
    ephemeral sandbox (the previous behaviour). Volumes are created on first use
    (``create_if_missing``) and reused on every later launch with the same base
    name, so relaunching the same config restores its state.
    """
    if not volume:
        return {}
    return {
        path: modal.Volume.from_name(f"{volume}-{suffix}", create_if_missing=True)
        for suffix, path in PERSIST_MOUNTS.items()
    }
# Base directory for the persistent workspace volume. Repo clones land in
# BASE_DIR/code; everything else in BASE_DIR is yours to use as scratch space
# that survives sandbox restarts (unlike the rest of the filesystem).
BASE_DIR = "/root/workspace"


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
    volume: str | None = None,
    ssh: bool = True,
    ssh_public_key: str | None = None,
    ssh_password: str | None = None,
    persist: bool = True,
    volume_name: str | None = None,
    hf_cache_volume: str = "claude-code-hf-cache",
):
    app = modal.App.lookup("claude-code-server", create_if_missing=True)

    # A registry secret only makes sense for a real registry pull.
    secret_name = registry_secret if image_base != "slim" else None
    work_image = build_image(image_base, secret_name)

    clone_dir = f"{BASE_DIR}/code" if repo else None
    work_dir = clone_dir or BASE_DIR

    # Persistent Modal Volumes (empty dict → ephemeral). When persisting and no
    # repo was cloned, land the editor/terminal in the persistent workspace so
    # files created there actually survive.
    volumes = build_volumes(volume)
    if volume and not repo:
        workdir = "/root/workspace"

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

    volumes = {}
    if persist:
        slug = (volume_name or repo or "default").strip().lower()
        slug = "".join(c if c.isalnum() or c == "-" else "-" for c in slug).strip("-")
        volumes = {
            BASE_DIR: modal.Volume.from_name(f"ccm-{slug}-workspace", create_if_missing=True),
            "/root/.claude": modal.Volume.from_name(f"ccm-{slug}-claude", create_if_missing=True),
            "/root/.local": modal.Volume.from_name(f"ccm-{slug}-local", create_if_missing=True),
            "/root/.cache/huggingface": modal.Volume.from_name(hf_cache_volume, create_if_missing=True),
        }
        print(f"Persistent storage: ccm-{slug}-{{workspace,claude,local}} + shared "
              f"'{hf_cache_volume}'. Reuse the same --volume-name to resume; don't run two "
              f"sandboxes with the same --volume-name at once (concurrent writers race).")
    else:
        print("Persistent storage: disabled (--no-persist) — sandbox filesystem is ephemeral.")

    startup = build_startup(BASE_DIR, work_dir, code_server, marimo, ssh,
                            settings_repo, settings_branch, repo, ref, clone_dir)

    ports = [PORT_TERMINAL]
    if code_server:
        ports.append(PORT_CODE)
    if marimo:
        ports.append(PORT_MARIMO)

    resource_summary = (
        f"image={image_base} gpu={gpu or 'none'} cpu={cpu or 'default'} "
        f"mem={memory or 'default'}MiB storage={'vol:' + volume if volume else 'ephemeral'}"
    )
    print(f"Launching Claude Code on Modal (timeout: {hours}h, {resource_summary})...")
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
        # can connect directly, unlike the encrypted tunnels which need an
        # HTTP(S)/TLS-aware client.
        sandbox_kwargs["unencrypted_ports"] = [PORT_SSH]

    with modal.enable_output():
        sandbox = modal.Sandbox.create(
            "bash", "-c", startup,
            encrypted_ports=ports,
            secrets=secrets,
            timeout=hours * 3600,
            image=work_image,
            app=app,
            workdir=workdir,
            volumes=volumes,
            **resource_kwargs,
        )

    tunnels = sandbox.tunnels()
    print(f"\n  Web terminal : {tunnels[PORT_TERMINAL].url}")
    print(f"               (bash shell, type `claude` — already authenticated)")
    if code_server:
        print(f"  VS Code      : {tunnels[PORT_CODE].url}")
    if marimo:
        print(f"  Marimo       : {tunnels[PORT_MARIMO].url}")
    print(f"  Shell access : modal shell {sandbox.object_id}")
    if volume:
        print(f"  Persistent   : volume '{volume}' → workspace, ~/.local, ~/.claude, HF cache "
              f"(relaunch with the same name to restore)")
    else:
        print(f"  Persistent   : none (ephemeral — pass --volume NAME to persist across runs)")
    if ssh:
        ssh_host, ssh_port = tunnels[PORT_SSH].tcp_socket
        print(f"  SSH          : ssh root@{ssh_host} -p {ssh_port}")
    shell_note = "  (fallback if SSH won't connect)" if ssh else ""
    print(f"  Shell access : modal shell {sandbox.object_id}{shell_note}")


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
    parser.add_argument("--volume", default=None, metavar="NAME",
                        help="Base name for persistent Modal Volumes mounted at workspace, "
                             "~/.local, ~/.claude, and the HF cache. Reusing a name restores "
                             "that config's state across runs. Omit for an ephemeral sandbox.")
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
    main(args.repo, args.ref, args.hours, args.github_token,
         gpu=args.gpu, cpu=args.cpu, memory=args.memory,
         image_base=args.image, registry_secret=args.registry_secret,
         extra_secrets=extra,
         code_server=args.code_server, marimo=args.marimo,
         settings_repo=args.claude_settings_repo or None,
         settings_branch=args.claude_settings_branch,
         volume=args.volume)
         ssh=args.ssh, ssh_public_key=args.ssh_public_key, ssh_password=args.ssh_password,
         persist=args.persist, volume_name=args.volume_name,
         hf_cache_volume=args.hf_cache_volume)
