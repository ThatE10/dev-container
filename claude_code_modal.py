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
"""
import argparse
import os
import modal

# ── Ports (each gets its own Modal HTTPS tunnel) ──────────────────────────────
PORT_TERMINAL = 7681   # ttyd web terminal
PORT_CODE     = 8443   # code-server (browser VS Code)
PORT_MARIMO   = 2719   # Marimo

DEFAULT_IMAGE = "ghcr.io/thate10/dev-container:latest"

# Layers added on top of any base — Node, Claude Code CLI, ttyd, code-server.
# code-server installs under /usr/local (on PATH); harmless if the base (the
# dev-container image) already ships it. Marimo is added via pip_install below.
_LAYER_CMDS = (
    "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -",
    "apt-get install -y nodejs",
    "npm install -g @anthropic-ai/claude-code",
    "curl -fsSL https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64"
    " -o /usr/local/bin/ttyd && chmod +x /usr/local/bin/ttyd",
    "curl -fsSL https://code-server.dev/install.sh"
    " | sh -s -- --method standalone --prefix /usr/local",
)


def build_image(base: str, registry_secret_name: str | None) -> modal.Image:
    """Build the sandbox image.

    base = 'slim' → debian_slim (fast, CPU-only, no CUDA userspace).
    base = 'ghcr.io/...' or any tag → pull that image; needs a Modal registry secret
    for private registries. dev-container's ENTRYPOINT is stripped so we control
    startup directly instead of going through its SSH/banner setup.
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


def build_startup(workdir: str, code_server: bool, marimo: bool,
                  settings_repo: str | None, settings_branch: str) -> str:
    """Bash run at sandbox start: restore creds, import secrets, optionally sync
    ~/.claude, launch VS Code + Marimo in the background, keep ttyd in front."""
    lines = [
        "mkdir -p ~/.claude",
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
            'git -C ~/.claude pull -q --ff-only origin "$CS_BRANCH" 2>/dev/null '
            '&& echo "[claude-settings] updated ~/.claude" || true',
        ]

    if code_server:
        lines.append(
            f"code-server --auth none --bind-addr 0.0.0.0:{PORT_CODE} "
            f"--disable-telemetry {workdir} > /tmp/code-server.log 2>&1 &"
        )
    if marimo:
        lines.append(
            f"( cd {workdir} && marimo edit --headless --host 0.0.0.0 "
            f"--port {PORT_MARIMO} --no-token > /tmp/marimo.log 2>&1 ) &"
        )

    # ttyd stays in the foreground so the sandbox stays alive.
    lines.append(f"exec ttyd -W -p {PORT_TERMINAL} bash")
    return "\n".join(lines)


# Modal secrets attached by default (in addition to claude-code-secret which is
# always attached). Missing secrets will cause sandbox creation to fail with a
# clear error — remove from the list or create via `modal secret create`.
DEFAULT_SECRETS = ["huggingface-secret", "github-secret"]


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
):
    app = modal.App.lookup("claude-code-server", create_if_missing=True)

    # A registry secret only makes sense for a real registry pull.
    secret_name = registry_secret if image_base != "slim" else None
    work_image = build_image(image_base, secret_name)
    workdir = "/root"

    if repo:
        token = github_token or os.environ.get("GH_TOKEN")
        if token:
            clone_cmd = (
                f"GIT_ASKPASS=echo git clone --quiet --depth 1 --branch {ref} "
                f"https://oauth2:{token}@github.com/{repo}.git /root/code"
            )
        else:
            clone_cmd = (
                f"GIT_TERMINAL_PROMPT=0 git clone --quiet --depth 1 --branch {ref} "
                f"https://github.com/{repo}.git /root/code"
            )

        work_image = work_image.run_commands(
            "git config --global advice.detachedHead false",
            clone_cmd,
            force_build=True,
        )
        workdir = "/root/code"

    secrets = [modal.Secret.from_name("claude-code-secret")]  # CLAUDE_CREDENTIALS
    attached_names = ["claude-code-secret"]
    for name in (extra_secrets or []):
        secrets.append(modal.Secret.from_name(name))
        attached_names.append(name)
    if github_token or os.environ.get("GH_TOKEN"):
        secrets.append(modal.Secret.from_dict({"GH_TOKEN": github_token or os.environ["GH_TOKEN"]}))
        attached_names.append("GH_TOKEN (ephemeral)")
    print(f"Attaching secrets: {', '.join(attached_names)}")

    resource_kwargs = {}
    if gpu:
        resource_kwargs["gpu"] = gpu
    if cpu is not None:
        resource_kwargs["cpu"] = cpu
    if memory is not None:
        resource_kwargs["memory"] = memory

    startup = build_startup(workdir, code_server, marimo, settings_repo, settings_branch)

    ports = [PORT_TERMINAL]
    if code_server:
        ports.append(PORT_CODE)
    if marimo:
        ports.append(PORT_MARIMO)

    resource_summary = (
        f"image={image_base} gpu={gpu or 'none'} cpu={cpu or 'default'} "
        f"mem={memory or 'default'}MiB"
    )
    print(f"Launching Claude Code on Modal (timeout: {hours}h, {resource_summary})...")
    with modal.enable_output():
        sandbox = modal.Sandbox.create(
            "bash", "-c", startup,
            encrypted_ports=ports,
            secrets=secrets,
            timeout=hours * 3600,
            image=work_image,
            app=app,
            workdir=workdir,
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
         settings_branch=args.claude_settings_branch)
