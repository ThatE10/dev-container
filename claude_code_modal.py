#!/usr/bin/env python3
"""
Claude Code server on Modal — web terminal, pre-authenticated via your Claude.ai subscription.
No API keys. Uses the OAuth credentials stored in your local macOS Keychain.

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
"""
import argparse
import os
import modal

PORT = 7681
DEFAULT_IMAGE = "ghcr.io/thate10/dev-container:latest"

# Layers added on top of any base — Node, Claude Code CLI, ttyd terminal.
_LAYER_CMDS = (
    "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -",
    "apt-get install -y nodejs",
    "npm install -g @anthropic-ai/claude-code",
    "curl -fsSL https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64"
    " -o /usr/local/bin/ttyd && chmod +x /usr/local/bin/ttyd",
)


def build_image(base: str, registry_secret_name: str | None) -> modal.Image:
    """Build the sandbox image.

    base = 'slim' → debian_slim (fast, CPU-only, no CUDA userspace).
    base = 'ghcr.io/...' or any tag → pull that image; needs a Modal registry secret
    for private registries. dev-container's ENTRYPOINT is stripped so ttyd starts
    directly instead of going through the SSH/banner setup.
    """
    if base == "slim":
        img = modal.Image.debian_slim().apt_install("curl", "git", "bash", "python3")
    else:
        secret = modal.Secret.from_name(registry_secret_name) if registry_secret_name else None
        img = (
            modal.Image.from_registry(base, add_python=None, secret=secret)
            .dockerfile_commands("ENTRYPOINT []")  # bypass dev-container's SSH+banner entrypoint
        )
    return img.run_commands(*_LAYER_CMDS).env(
        {"TERM": "xterm-256color", "COLORTERM": "truecolor"}
    )

# Startup: restore credentials from env, run the dev-container's secret importer
# (writes HF/WandB/GitHub/Modal tokens to their canonical config files), then
# open a web terminal where `claude` is ready to use.
# On the slim base the importer doesn't exist — the `|| true` keeps ttyd starting.
STARTUP = (
    "mkdir -p ~/.claude && "
    "printf '%s' \"$CLAUDE_CREDENTIALS\" > ~/.claude/.credentials.json && "
    # Normalize GH_TOKEN ↔ GITHUB_TOKEN — gh CLI accepts either but import-secrets.sh
    # only checks GITHUB_TOKEN, so a `github-secret` that exports GH_TOKEN would
    # silently skip `gh auth login`.
    "export GITHUB_TOKEN=\"${GITHUB_TOKEN:-${GH_TOKEN:-}}\" && "
    "export GH_TOKEN=\"${GH_TOKEN:-${GITHUB_TOKEN:-}}\" && "
    "([ -x /opt/scripts/import-secrets.sh ] && /opt/scripts/import-secrets.sh || true) && "
    f"ttyd -W -p {PORT} bash"
)

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

    resource_summary = (
        f"image={image_base} gpu={gpu or 'none'} cpu={cpu or 'default'} "
        f"mem={memory or 'default'}MiB"
    )
    print(f"Launching Claude Code on Modal (timeout: {hours}h, {resource_summary})...")
    with modal.enable_output():
        sandbox = modal.Sandbox.create(
            "bash", "-c", STARTUP,
            encrypted_ports=[PORT],
            secrets=secrets,
            timeout=hours * 3600,
            image=work_image,
            app=app,
            workdir=workdir,
            **resource_kwargs,
        )

    tunnel = sandbox.tunnels()[PORT]
    print(f"\n  Web terminal : {tunnel.url}")
    print(f"               (bash shell, type `claude` — already authenticated)")
    print(f"  Shell access : modal shell {sandbox.object_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Claude Code on Modal via web terminal")
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
         extra_secrets=extra)
