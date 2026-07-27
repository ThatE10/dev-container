#!/usr/bin/env python3
"""PreToolUse hook for the Bash tool: block writes to the canonical draft.

Registered for the ``Bash`` tool. Denies any shell command that references
``paper/paper.tex`` in a *mutating* context -- redirections (``>``, ``>>``),
in-place stream editing (``sed -i``), ``tee``, or ``mv`` / ``cp`` / ``rm`` /
``chmod +w`` naming the file.

NOTE: this is a HEURISTIC. Shell is not a regular language and this hook can be
fooled by aliases, variable indirection, ``eval``, unusual quoting, etc. It is a
convenience tripwire that gives the model a clear reason, not a security
boundary. The HARD BACKSTOP is the filesystem: paper/paper.tex is ``chmod a-w``
(re-asserted by the SessionStart hook because git operations reset permissions),
so a mutation that slips past this heuristic still fails at the OS level.
"""

import json
import re
import sys

# Match the canonical basename `paper.tex` but never `paper.staged.tex`
# ("paper.tex" is not a substring of "paper.staged.tex", and the trailing
# boundary keeps us off things like `paper.textutil`).
CANONICAL = re.compile(r"paper\.tex(?![\w.])")

# Each entry: (compiled pattern, human-readable description of the mutation).
# Every pattern additionally requires the canonical file to be referenced;
# that conjunction is checked in main().
MUTATORS = [
    # `>` or `>>` redirecting into (an optional path ending in) paper.tex.
    (re.compile(r">>?\s*(?:[\w./~-]*/)?paper\.tex(?![\w.])"),
     "output redirection (> or >>) into paper/paper.tex"),
    # sed -i / --in-place anywhere in a command that also names paper.tex.
    (re.compile(r"\bsed\b(?=.*\bpaper\.tex(?![\w.]))(?=.*(?:-i\b|--in-place))",
                re.DOTALL),
     "in-place edit via `sed -i` on paper/paper.tex"),
    # tee writing to paper.tex.
    (re.compile(r"\btee\b(?:\s+\S+)*\s+(?:[\w./~-]*/)?paper\.tex(?![\w.])"),
     "`tee` writing to paper/paper.tex"),
    # mv / cp / rm naming paper.tex (destination or target).
    (re.compile(r"\b(?:mv|cp|rm)\b(?=.*\bpaper\.tex(?![\w.]))", re.DOTALL),
     "`mv`/`cp`/`rm` targeting paper/paper.tex"),
    # chmod ... +w ... paper.tex (attempt to remove the write-protection).
    (re.compile(r"\bchmod\b(?=.*\+w)(?=.*\bpaper\.tex(?![\w.]))", re.DOTALL),
     "`chmod +w` on paper/paper.tex (would defeat the write-protection)"),
]


def _deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def main() -> int:
    try:
        hook = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    command = (hook.get("tool_input") or {}).get("command") or ""
    if not command or not CANONICAL.search(command):
        return 0

    for pattern, description in MUTATORS:
        if pattern.search(command):
            _deny(
                "Blocked: this command would mutate the canonical draft "
                f"paper/paper.tex via {description}. That file is promotion-only "
                "and write-protected -- edit paper/paper.staged.tex instead and "
                "promote through the promote-draft workflow. (Heuristic guard; "
                "the filesystem chmod a-w bit is the hard backstop.)"
            )
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
