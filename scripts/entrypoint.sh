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

# ── Source .env on every SSH login ───────────────────────────────────────────
grep -qxF '[ -f /root/.env ] && source /root/.env' /root/.bashrc 2>/dev/null \
    || echo '[ -f /root/.env ] && source /root/.env' >> /root/.bashrc

# ── Info banner ───────────────────────────────────────────────────────────────
cat << 'BANNER'

┌──────────────────────────────────────────────────────┐
│  Dev Container ready                                 │
│                                                      │
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
