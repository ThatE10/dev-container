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

  # Optional — for private GitHub repos:
  modal secret create github-secret GH_TOKEN=$(gh auth token)

Usage:
  python claude_code_modal.py
  python claude_code_modal.py --repo owner/repo
  python claude_code_modal.py --repo owner/repo --ref dev --hours 8
  python claude_code_modal.py --repo owner/repo --github-token $(gh auth token)
"""
import argparse
import os
import modal

PORT = 7681

image = (
    modal.Image.debian_slim()
    .apt_install("curl", "git", "bash", "python3")
    .run_commands(
        # Node.js 20 LTS
        "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -",
        "apt-get install -y nodejs",
        # Claude Code CLI
        "npm install -g @anthropic-ai/claude-code",
        # ttyd — lightweight web terminal
        "curl -fsSL https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64"
        " -o /usr/local/bin/ttyd && chmod +x /usr/local/bin/ttyd",
    )
    .env({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
)

# Startup: restore credentials from env, then open a web terminal where `claude` is ready to use.
# Credentials are the raw JSON blob from the macOS Keychain ("Claude Code-credentials").
# On Linux without a keychain, Claude Code falls back to reading ~/.claude/.credentials.json.
STARTUP = (
    "mkdir -p ~/.claude && "
    "printf '%s' \"$CLAUDE_CREDENTIALS\" > ~/.claude/.credentials.json && "
    f"ttyd -W -p {PORT} bash"
)


def main(repo: str | None, ref: str, hours: int, github_token: str | None):
    app = modal.App.lookup("claude-code-server", create_if_missing=True)

    work_image = image
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
    if github_token or os.environ.get("GH_TOKEN"):
        secrets.append(modal.Secret.from_dict({"GH_TOKEN": github_token or os.environ["GH_TOKEN"]}))

    print(f"Launching Claude Code on Modal (timeout: {hours}h)...")
    with modal.enable_output():
        sandbox = modal.Sandbox.create(
            "bash", "-c", STARTUP,
            encrypted_ports=[PORT],
            secrets=secrets,
            timeout=hours * 3600,
            image=work_image,
            app=app,
            workdir=workdir,
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
    args = parser.parse_args()
    main(args.repo, args.ref, args.hours, args.github_token)
