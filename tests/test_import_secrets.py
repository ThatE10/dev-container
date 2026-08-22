"""Secret resolution: env first, then PID 1's environ.

Modal injects a secret's values onto the container's *main* process. A startup
script, an ssh session, or a later-started tmux server may not have them in its
own environment, and on the ``slim`` image there is no
``/opt/scripts/import-secrets.sh`` to write ``~/.modal.toml`` at all. Both
failures present as "Token missing", which reads like an unattached secret
rather than a plumbing gap — so both paths are pinned here.

Nothing in this file touches real credentials: the PID-1 environ is a fixture,
and ``HOME`` is redirected to a tmp dir.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import conftest  # noqa: F401  — installs the modal stub
import claude_code_modal as ccm  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "secret-source.sh"


def environ_blob(tmp_path: Path, **pairs: str) -> Path:
    """A NUL-separated /proc/<pid>/environ lookalike."""
    blob = "".join(f"{k}={v}\0" for k, v in pairs.items())
    p = tmp_path / "environ"
    p.write_bytes(blob.encode())
    return p


def run_secret(var: str, *, env: dict | None = None, pid1: Path | None = None) -> str:
    script = f'. "{HELPER}"; secret {var}'
    full = {"PATH": os.environ["PATH"]}
    if pid1 is not None:
        full["PID1_ENVIRON"] = str(pid1)
    full.update(env or {})
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         env=full, check=True)
    return out.stdout


# ── the helper ───────────────────────────────────────────────────────────────

def test_prefers_the_process_environment(tmp_path):
    pid1 = environ_blob(tmp_path, MODAL_TOKEN_ID="from-pid1")
    assert run_secret("MODAL_TOKEN_ID",
                      env={"MODAL_TOKEN_ID": "from-env"}, pid1=pid1) == "from-env"


def test_falls_back_to_pid1_when_absent_from_the_environment(tmp_path):
    pid1 = environ_blob(tmp_path, MODAL_TOKEN_ID="ak-recovered")
    assert run_secret("MODAL_TOKEN_ID", pid1=pid1) == "ak-recovered"


def test_empty_environment_value_is_not_preferred_over_pid1(tmp_path):
    """An exported-but-empty var must not shadow a real value on PID 1."""
    pid1 = environ_blob(tmp_path, MODAL_TOKEN_ID="ak-real")
    assert run_secret("MODAL_TOKEN_ID", env={"MODAL_TOKEN_ID": ""}, pid1=pid1) == "ak-real"


def test_missing_environ_file_is_silent_and_non_fatal(tmp_path):
    """`set -euo pipefail` must survive a host with no readable /proc."""
    assert run_secret("MODAL_TOKEN_ID", pid1=tmp_path / "nope") == ""


def test_value_containing_equals_survives(tmp_path):
    pid1 = environ_blob(tmp_path, MODAL_TOKEN_SECRET="as-a=b=c")
    assert run_secret("MODAL_TOKEN_SECRET", pid1=pid1) == "as-a=b=c"


def test_only_the_requested_var_is_returned(tmp_path):
    pid1 = environ_blob(tmp_path, OTHER="x", MODAL_TOKEN_ID="ak-1", MORE="y")
    assert run_secret("MODAL_TOKEN_ID", pid1=pid1) == "ak-1"


# ── the launcher's image-independent backstop ────────────────────────────────

def backstop_block() -> str:
    """The ~/.modal.toml backstop, sliced out of the real generated startup."""
    script = ccm.build_startup(
        base_dir="/root/workspace", work_dir="/root/workspace/code",
        code_server=False, marimo=False, ssh=False,
        settings_repo=None, settings_branch="main",
        repo=None, ref="main", clone_dir=None,
    )
    m = re.search(r'^if \[ ! -s "\$HOME/\.modal\.toml" \]; then$.*?^fi$',
                  script, re.M | re.S)
    assert m, "backstop block not found in the generated startup script"
    return m.group(0)


def run_backstop(tmp_path: Path, *, pid1: Path | None = None, **env: str):
    home = tmp_path / "home"
    home.mkdir()
    # Always pin PID1_ENVIRON: without it these tests would read the *real*
    # /proc/1/environ of whatever host they run on, so a machine that happens to
    # carry Modal credentials would silently pass the "nothing to write" case.
    full = {"PATH": os.environ["PATH"], "HOME": str(home),
            "PID1_ENVIRON": str(pid1 if pid1 is not None else tmp_path / "absent")}
    full.update(env)
    proc = subprocess.run(["bash", "-c", backstop_block()],
                          capture_output=True, text=True, env=full)
    return home / ".modal.toml", proc


def test_backstop_writes_modal_toml_from_the_environment(tmp_path):
    toml, proc = run_backstop(tmp_path, MODAL_TOKEN_ID="ak-1",
                              MODAL_TOKEN_SECRET="as-2")
    assert proc.returncode == 0, proc.stderr
    assert toml.read_text() == '[default]\ntoken_id = "ak-1"\ntoken_secret = "as-2"\n'


def test_backstop_output_is_0600(tmp_path):
    """Credentials on disk must not be world-readable."""
    toml, _ = run_backstop(tmp_path, MODAL_TOKEN_ID="ak-1", MODAL_TOKEN_SECRET="as-2")
    assert oct(toml.stat().st_mode)[-3:] == "600"


def test_backstop_warns_loudly_when_there_is_nothing_to_write(tmp_path):
    """The old `|| true` swallowed this; silence is what made it hard to debug."""
    toml, proc = run_backstop(tmp_path)
    assert not toml.exists()
    assert "WARNING" in proc.stderr
    assert "NOT be able to launch Modal jobs" in proc.stderr


def test_backstop_does_not_clobber_an_existing_credential(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".modal.toml").write_text("[default]\ntoken_id = \"already-here\"\n")
    subprocess.run(["bash", "-c", backstop_block()], capture_output=True, text=True,
                   env={"PATH": os.environ["PATH"], "HOME": str(home),
                        "PID1_ENVIRON": str(tmp_path / "absent"),
                        "MODAL_TOKEN_ID": "ak-new", "MODAL_TOKEN_SECRET": "as-new"})
    assert "already-here" in (home / ".modal.toml").read_text()


def test_backstop_never_echoes_the_secret(tmp_path):
    _, proc = run_backstop(tmp_path, MODAL_TOKEN_ID="ak-SENSITIVE",
                           MODAL_TOKEN_SECRET="as-SENSITIVE")
    assert "SENSITIVE" not in proc.stdout + proc.stderr


def test_backstop_recovers_from_pid1_when_the_environment_is_empty(tmp_path):
    """The slim-image case: startup script has no MODAL_* of its own."""
    pid1 = environ_blob(tmp_path, MODAL_TOKEN_ID="ak-p1", MODAL_TOKEN_SECRET="as-p1")
    toml, proc = run_backstop(tmp_path, pid1=pid1)
    assert proc.returncode == 0, proc.stderr
    assert toml.read_text() == '[default]\ntoken_id = "ak-p1"\ntoken_secret = "as-p1"\n'
