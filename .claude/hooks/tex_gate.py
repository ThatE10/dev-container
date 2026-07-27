#!/usr/bin/env python3
"""PreToolUse hook: redirect edits of the canonical draft to the staged copy.

Registered for the ``Edit`` and ``Write`` tools. Claude Code passes the hook
event as JSON on stdin. When the tool targets ``paper/paper.tex`` (the canonical
draft, which is write-protected on disk) we:

  (a) ensure ``paper/paper.staged.tex`` exists as a byte-for-byte copy of the
      canonical file, so an ``Edit`` whose ``old_string`` was written against the
      canonical text still matches cleanly against the staged file;
  (b) emit ``permissionDecision: "allow"`` together with an ``updatedInput`` that
      is the COMPLETE original ``tool_input`` with only ``file_path`` rewritten to
      the staged path. The runtime REPLACES (does not merge) the tool input with
      ``updatedInput``, so every original field must be echoed back verbatim;
  (c) attach ``additionalContext`` telling the model the edit was redirected and
      that the canonical file is promotion-only, keeping its belief state honest.

Any other path is none of our business: exit 0 with no output so the normal
permission flow applies.
"""

import json
import os
import shutil
import sys

CANONICAL_REL = os.path.join("paper", "paper.tex")
STAGED_BASENAME = "paper.staged.tex"


def _project_root(hook: dict) -> str:
    """Best-effort project root: env var, then hook cwd, then process cwd."""
    return (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or hook.get("cwd")
        or os.getcwd()
    )


def _abs(path: str, base: str) -> str:
    """Resolve ``path`` to a normalized absolute path, anchored at ``base``."""
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.realpath(path)


def main() -> int:
    try:
        hook = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Malformed payload: stay out of the way.
        return 0

    tool_input = hook.get("tool_input") or {}
    file_path = tool_input.get("file_path")
    if not file_path:
        return 0

    root = _project_root(hook)
    base = hook.get("cwd") or root
    canonical_abs = _abs(CANONICAL_REL, root)
    target_abs = _abs(file_path, base)

    # Only intercept the canonical draft; everything else passes through.
    if target_abs != canonical_abs:
        return 0

    canonical_dir = os.path.dirname(canonical_abs)
    staged_abs = os.path.join(canonical_dir, STAGED_BASENAME)

    # (a) Seed the staged file from the canonical text if it does not exist yet.
    #     copyfile (not copy2) so we do not inherit the canonical read-only bit.
    if not os.path.exists(staged_abs):
        try:
            shutil.copyfile(canonical_abs, staged_abs)
            os.chmod(staged_abs, 0o644)
        except OSError as exc:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                "tex_gate could not create the staged copy "
                                f"paper/{STAGED_BASENAME}: {exc}. Refusing to let "
                                "the edit fall through to the write-protected "
                                "canonical paper/paper.tex."
                            ),
                        }
                    }
                )
            )
            return 0

    # (b) Rewrite ONLY file_path, preserving the form (absolute/relative) of the
    #     original string, and echo every other field so the replace is lossless.
    updated_input = dict(tool_input)
    updated_input["file_path"] = os.path.join(
        os.path.dirname(file_path), STAGED_BASENAME
    )

    # (c) Keep the model's belief state accurate.
    additional_context = (
        "Your edit targeted paper/paper.tex, the CANONICAL draft, which is "
        "write-protected on disk. It has been transparently redirected to "
        f"paper/{STAGED_BASENAME} and applied there. paper/paper.tex is "
        "promotion-only: it is updated solely by the promote-draft GitHub "
        "Actions workflow (which the user triggers) via a reviewed pull "
        f"request. Continue editing paper/{STAGED_BASENAME} directly; editing "
        "paper/paper.tex will keep redirecting here."
    )

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "additionalContext": additional_context,
                    "updatedInput": updated_input,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
