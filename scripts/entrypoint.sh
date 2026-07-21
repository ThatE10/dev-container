#!/usr/bin/env bash
set -euo pipefail

echo "🚀 Dev Container starting..."

# ── Secrets ───────────────────────────────────────────────────────────────────
/opt/scripts/import-secrets.sh

# ── Claude Code credentials ───────────────────────────────────────────────────
# CLAUDE_CREDENTIALS = raw JSON from macOS Keychain ("Claude Code-credentials").
# On Linux without a keychain, Claude Code reads ~/.claude/.credentials.json.
if [ -n "${CLAUDE_CREDENTIALS:-}" ]; then
    mkdir -p /root/.claude
    printf '%s' "$CLAUDE_CREDENTIALS" > /root/.claude/.credentials.json
    echo "  [claude] Credentials written to ~/.claude/.credentials.json"
fi

# ── SSH auth ──────────────────────────────────────────────────────────────────
# Require at least SSH_PUBLIC_KEY or SSH_PASSWORD — no hardcoded defaults.
if [ -z "${SSH_PUBLIC_KEY:-}" ] && [ -z "${SSH_PASSWORD:-}" ]; then
    echo ""
    echo "⚠️  WARNING: No SSH auth configured. Set SSH_PUBLIC_KEY or SSH_PASSWORD." >&2
    echo "   SSH will start but no login will succeed." >&2
    echo ""
fi

if [ -n "${SSH_PUBLIC_KEY:-}" ]; then
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    printf '%s\n' "$SSH_PUBLIC_KEY" >> /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    echo "  [ssh] Public key installed"
fi

if [ -n "${SSH_PASSWORD:-}" ]; then
    printf 'root:%s' "$SSH_PASSWORD" | chpasswd
    sed -i 's/^PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
    echo "  [ssh] Password auth enabled"
fi

service ssh start
echo "  [ssh] Server started on port 22"

# ── Persisted shell context ───────────────────────────────────────────────────
# /root/.local is a named volume (see docker-compose.yml), so anything under it
# survives a rebuild: code-server extensions, `pip install --user` packages,
# ~/.local/bin tools, and the shell history below.
mkdir -p /root/.local/share /root/.local/bin
add_bashrc() {
    grep -qxF "$1" /root/.bashrc 2>/dev/null || echo "$1" >> /root/.bashrc
}
add_bashrc 'export PATH="$HOME/.local/bin:$PATH"'
add_bashrc 'export HISTFILE="$HOME/.local/share/.bash_history"'
add_bashrc 'export HISTSIZE=10000 HISTFILESIZE=20000'
# Append rather than overwrite history, and flush after each command so a
# hard `docker compose down` doesn't lose the last session's history.
add_bashrc 'shopt -s histappend'
add_bashrc 'export PROMPT_COMMAND="history -a; ${PROMPT_COMMAND:-}"'

# ── Source .env on every SSH login ───────────────────────────────────────────
add_bashrc '[ -f /root/.env ] && source /root/.env'

# ── code-server (browser VS Code) ─────────────────────────────────────────────
# Auto-starts unless START_CODE_SERVER=0. Port defaults to 8443.
# Auth: if CODE_SERVER_PASSWORD is set, require it; otherwise auth is disabled
# and access relies on the SSH tunnel / trusted network (same model as Jupyter).
if [ "${START_CODE_SERVER:-1}" != "0" ]; then
    CODE_PORT="${CODE_SERVER_PORT:-8443}"
    mkdir -p /root/.config/code-server
    if [ -n "${CODE_SERVER_PASSWORD:-}" ]; then
        cat > /root/.config/code-server/config.yaml << EOF
bind-addr: 0.0.0.0:${CODE_PORT}
auth: password
password: ${CODE_SERVER_PASSWORD}
cert: false
EOF
        echo "  [code-server] Password auth enabled"
    else
        cat > /root/.config/code-server/config.yaml << EOF
bind-addr: 0.0.0.0:${CODE_PORT}
auth: none
cert: false
EOF
        echo "  [code-server] ⚠️  No CODE_SERVER_PASSWORD set — auth disabled (use an SSH tunnel)"
    fi
    # Open the workspace by default so you land in your project.
    nohup code-server --disable-telemetry /root/workspace \
        > /var/log/code-server.log 2>&1 &
    echo "  [code-server] Started on port ${CODE_PORT} (log: /var/log/code-server.log)"
fi

# ── Info banner ───────────────────────────────────────────────────────────────
cat << 'BANNER'

┌──────────────────────────────────────────────────────┐
│  Dev Container ready                                 │
│                                                      │
│  VS Code:    open http://localhost:8443  (auto-run)  │
│  SSH:        ssh root@<host> -p <port>               │
│  JupyterLab: jupyter lab          → port 8888        │
│  Marimo:     marimo edit          → port 2719        │
│  vLLM:       python -m vllm.entrypoints.openai \     │
│                .api_server --model <model>           │
│                                                      │
│  GPU info:   nvidia-smi                              │
└──────────────────────────────────────────────────────┘

BANNER

exec "$@"
