#!/usr/bin/env python3
"""
Claude Code server on Modal — browser VS Code + Marimo + a web terminal,
pre-authenticated via your Claude.ai subscription. No API keys. Uses the OAuth
credentials stored in your local macOS Keychain.

This mirrors the Docker dev container: same three surfaces (code-server, Marimo,
terminal) and the same optional ~/.claude settings-repo sync.

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
  python claude_code_modal.py --no-code-server        # terminal + Marimo only
  python claude_code_modal.py --no-marimo             # terminal + VS Code only
  python claude_code_modal.py --claude-settings-repo ThatE10/claude-code-settings
"""
import argparse
import os
import modal

# ── Ports (each gets its own Modal HTTPS tunnel) ──────────────────────────────
PORT_TERMINAL = 7681   # ttyd web terminal
PORT_CODE     = 8443   # code-server (browser VS Code)
PORT_MARIMO   = 2719   # Marimo

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
        # code-server (browser VS Code) — install under /usr/local so the binary
        # is on PATH (matches the Docker image; not the default ~/.local prefix).
        "curl -fsSL https://code-server.dev/install.sh"
        " | sh -s -- --method standalone --prefix /usr/local",
    )
    # Marimo — reactive notebooks, same as the Docker image.
    .pip_install("marimo")
    .env({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
)


def build_startup(workdir: str, code_server: bool, marimo: bool,
                  settings_repo: str | None, settings_branch: str) -> str:
    """Bash run at sandbox start: restore creds, optionally sync ~/.claude,
    launch VS Code + Marimo in the background, keep the web terminal in front."""
    lines = [
        "mkdir -p ~/.claude",
        # Credentials: raw JSON blob from the macOS Keychain. On Linux without a
        # keychain, Claude Code falls back to ~/.claude/.credentials.json.
        "printf '%s' \"$CLAUDE_CREDENTIALS\" > ~/.claude/.credentials.json",
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


def main(repo, ref, hours, github_token, code_server, marimo,
         settings_repo, settings_branch):
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

    startup = build_startup(workdir, code_server, marimo, settings_repo, settings_branch)

    ports = [PORT_TERMINAL]
    if code_server:
        ports.append(PORT_CODE)
    if marimo:
        ports.append(PORT_MARIMO)

    print(f"Launching Claude Code on Modal (timeout: {hours}h)...")
    with modal.enable_output():
        sandbox = modal.Sandbox.create(
            "bash", "-c", startup,
            encrypted_ports=ports,
            secrets=secrets,
            timeout=hours * 3600,
            image=work_image,
            app=app,
            workdir=workdir,
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
    parser.add_argument("--no-code-server", dest="code_server", action="store_false",
                        help="Don't start browser VS Code")
    parser.add_argument("--no-marimo", dest="marimo", action="store_false",
                        help="Don't start Marimo")
    parser.add_argument("--claude-settings-repo", default="ThatE10/claude-code-settings",
                        help="Settings repo synced into ~/.claude (empty to disable)")
    parser.add_argument("--claude-settings-branch", default="main",
                        help="Settings repo branch (default: main)")
    args = parser.parse_args()
    main(
        args.repo, args.ref, args.hours, args.github_token,
        args.code_server, args.marimo,
        args.claude_settings_repo or None, args.claude_settings_branch,
    )
