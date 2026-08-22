#!/usr/bin/env bash
# Resolve a secret's value, wherever the container actually put it.
#
# Modal injects a secret's values as environment variables on the container's
# **main process**. Any shell that is not a descendant of PID 1 — an ssh
# session, a pane in a tmux server started later, a Claude Code tool call —
# builds a fresh environment without them. Reading `$MODAL_TOKEN_ID` from such a
# shell silently yields nothing, and every consumer downstream then reports a
# *missing credential*, which reads like a bad or unattached secret rather than
# the plumbing gap it actually is.
#
# Sourced by import-secrets.sh. Kept as its own file so the fallback can be
# unit-tested against a fixture instead of the live container's real tokens.
#
# Usage:  . secret-source.sh;  val="$(secret MODAL_TOKEN_ID)"

# Overridable so tests can point at a fixture.
PID1_ENVIRON="${PID1_ENVIRON:-/proc/1/environ}"

# Read one NUL-separated VAR=value pair out of a process environ blob.
# Silent and non-fatal when the file is absent or not ours to read, so this is
# safe under `set -euo pipefail` on a host without /proc.
pid1_env() {
    [ -r "$PID1_ENVIRON" ] || return 0
    tr '\0' '\n' < "$PID1_ENVIRON" 2>/dev/null | sed -n "s/^$1=//p" | head -1
}

# Prefer this process's environment; fall back to PID 1's.
secret() {
    local val="${!1:-}"
    [ -n "$val" ] || val="$(pid1_env "$1")"
    printf '%s' "$val"
}
