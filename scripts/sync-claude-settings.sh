#!/usr/bin/env bash
# Load & update global Claude Code settings from a git repo into ~/.claude.
#
# The settings repo (default: ThatE10/claude-code-settings) is designed to BE
# ~/.claude: it tracks agents/, hooks/, skills/, memory/, .claude-tools/, and
# gitignores runtime files (settings.json, sessions/, projects/, history.jsonl,
# credentials). So we make ~/.claude a checkout of that repo and `git pull` it
# on every start. Untracked/ignored files — your credentials and session state —
# are never touched.
#
# Everything here is best-effort: a missing repo, no network, or a diverged
# branch logs a warning and returns 0 so the container still starts.
#
# Env:
#   CLAUDE_SETTINGS_REPO    owner/repo or full git URL (default below; empty = skip)
#   CLAUDE_SETTINGS_BRANCH  branch to track (default: main)
#   CLAUDE_SETTINGS_SYNC    set to 0 to disable entirely
#   GITHUB_TOKEN            used for private repos when REPO is given as owner/repo

set -uo pipefail

info() { echo "  [claude-settings] $*"; }

REPO="${CLAUDE_SETTINGS_REPO:-ThatE10/claude-code-settings}"
BRANCH="${CLAUDE_SETTINGS_BRANCH:-main}"
CLAUDE_DIR="/root/.claude"

if [ "${CLAUDE_SETTINGS_SYNC:-1}" = "0" ] || [ -z "$REPO" ]; then
    info "Sync disabled — skipping"
    exit 0
fi

# Accept either "owner/repo" or a full URL. For owner/repo, use GITHUB_TOKEN if
# present so private settings repos work too.
case "$REPO" in
    http://*|https://*|git@*) URL="$REPO" ;;
    *)
        if [ -n "${GITHUB_TOKEN:-}" ]; then
            URL="https://oauth2:${GITHUB_TOKEN}@github.com/${REPO}.git"
        else
            URL="https://github.com/${REPO}.git"
        fi
        ;;
esac

mkdir -p "$CLAUDE_DIR"
# Avoid "dubious ownership" errors on volume-mounted dirs.
git config --global --add safe.directory "$CLAUDE_DIR" 2>/dev/null || true

# First run: ~/.claude already exists (credentials, persistent volume), so a
# plain `git clone` (which needs an empty target) won't work. Clone the history
# into a temp dir, move .git into place, then check out tracked files. `-f`
# overwrites tracked files only; untracked credentials/sessions stay put.
if [ ! -d "$CLAUDE_DIR/.git" ]; then
    TMP="$(mktemp -d)"
    if git clone --quiet --branch "$BRANCH" --no-checkout "$URL" "$TMP/repo" 2>/dev/null; then
        mv "$TMP/repo/.git" "$CLAUDE_DIR/.git"
        if git -C "$CLAUDE_DIR" checkout -f "$BRANCH" 2>/dev/null; then
            info "Cloned ${REPO} → ~/.claude"
        else
            info "Checkout of ${BRANCH} failed (non-fatal)"
            rm -rf "$CLAUDE_DIR/.git"
        fi
    else
        info "Clone failed — check CLAUDE_SETTINGS_REPO / GITHUB_TOKEN (non-fatal)"
    fi
    rm -rf "$TMP"
fi

# Every start: fast-forward to the latest settings.
if [ -d "$CLAUDE_DIR/.git" ]; then
    if git -C "$CLAUDE_DIR" pull --quiet --ff-only origin "$BRANCH" 2>/dev/null; then
        info "Updated ~/.claude from ${REPO} (${BRANCH})"
    else
        info "Pull skipped (offline or branch diverged; non-fatal)"
    fi
fi

exit 0
