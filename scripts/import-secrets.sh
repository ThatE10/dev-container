#!/usr/bin/env bash
# Write secret environment variables to their canonical config locations.
# Called by entrypoint.sh at container start — safe to run with missing vars.

set -euo pipefail

info() { echo "  [secrets] $*"; }

# ── HuggingFace ───────────────────────────────────────────────────────────────
HF_TOKEN="${HF_TOKEN:-${HUGGINGFACE_HUB_TOKEN:-}}"
if [ -n "$HF_TOKEN" ]; then
    mkdir -p /root/.cache/huggingface
    printf '%s' "$HF_TOKEN" > /root/.cache/huggingface/token
    info "HuggingFace token written"
fi

# ── Modal ─────────────────────────────────────────────────────────────────────
if [ -n "${MODAL_TOKEN_ID:-}" ] && [ -n "${MODAL_TOKEN_SECRET:-}" ]; then
    cat > /root/.modal.toml << EOF
[default]
token_id = "$MODAL_TOKEN_ID"
token_secret = "$MODAL_TOKEN_SECRET"
EOF
    info "Modal credentials written to ~/.modal.toml"
fi

# ── Weights & Biases ──────────────────────────────────────────────────────────
if [ -n "${WANDB_API_KEY:-}" ]; then
    mkdir -p /root/.config/wandb
    printf '%s' "$WANDB_API_KEY" > /root/.config/wandb/auth
    info "Weights & Biases token written"
fi

# ── GitHub CLI ────────────────────────────────────────────────────────────────
if [ -n "${GITHUB_TOKEN:-}" ]; then
    echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null \
        && info "GitHub CLI authenticated" \
        || info "GitHub CLI auth failed (non-fatal)"
fi

# ── Private secrets repo (optional) ───────────────────────────────────────────
# Set SECRETS_REPO=owner/repo to pull additional secrets from a private GitHub repo.
# The repo must contain a .env file at its root.
if [ -n "${SECRETS_REPO:-}" ] && [ -n "${GITHUB_TOKEN:-}" ]; then
    TMP=$(mktemp -d)
    if git clone --quiet --depth 1 \
        "https://oauth2:${GITHUB_TOKEN}@github.com/${SECRETS_REPO}.git" \
        "$TMP/secrets" 2>/dev/null; then
        if [ -f "$TMP/secrets/.env" ]; then
            cat "$TMP/secrets/.env" >> /root/.env
            info "Loaded .env from $SECRETS_REPO"
        fi
    else
        info "Could not clone $SECRETS_REPO (check GITHUB_TOKEN permissions)"
    fi
    rm -rf "$TMP"
fi

# ── Write consolidated .env (sourced on every SSH login) ─────────────────────
{
    for VAR in HF_TOKEN HUGGINGFACE_HUB_TOKEN ANTHROPIC_API_KEY OPENAI_API_KEY \
                MODAL_TOKEN_ID MODAL_TOKEN_SECRET WANDB_API_KEY COMET_API_KEY \
                GITHUB_TOKEN; do
        VAL="${!VAR:-}"
        [ -n "$VAL" ] && printf "export %s='%s'\n" "$VAR" "$VAL"
    done
} >> /root/.env

info "Done"
