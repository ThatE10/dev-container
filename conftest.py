"""Pytest bootstrap: make the repo root importable and stub out ``modal``.

``claude_code_modal.py`` does ``import modal`` at module load, but the unit
tests never touch real Modal infrastructure — they exercise the pure helpers
(slug/startup/image plumbing) and capture what ``main()`` *would* pass to
``modal.Sandbox.create``. So we install a lightweight fake ``modal`` module into
``sys.modules`` before any test imports the script.

The fake records every ``Volume.from_name`` / ``Secret.from_name`` call and the
kwargs handed to ``Sandbox.create`` so tests can assert on them via
``modal.LAST_SANDBOX_KWARGS`` etc.
"""
import sys
import types
from pathlib import Path

import pytest

# Repo root on sys.path so `import launch` / `import claude_code_modal` work
# regardless of where pytest is invoked from.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_fake_modal() -> types.ModuleType:
    import contextlib

    modal = types.ModuleType("modal")

    # Call logs the tests can inspect.
    modal.CREATED_VOLUMES = []      # list of (name, create_if_missing)
    modal.NAMED_SECRETS = []        # list of secret names looked up
    modal.DICT_SECRETS = []         # list of dicts turned into secrets
    modal.LAST_SANDBOX_ARGS = None  # positional args to Sandbox.create
    modal.LAST_SANDBOX_KWARGS = None
    modal.LAST_SANDBOX = None       # the _Sandbox object Sandbox.create returned
    modal.NEXT_RETURNCODE = 0       # what a batch sandbox's wait() should report

    def _reset():
        modal.CREATED_VOLUMES.clear()
        modal.NAMED_SECRETS.clear()
        modal.DICT_SECRETS.clear()
        modal.LAST_SANDBOX_ARGS = None
        modal.LAST_SANDBOX_KWARGS = None
        modal.LAST_SANDBOX = None
        modal.NEXT_RETURNCODE = 0

    modal.reset = _reset

    class _Image:
        """Chainable no-op image builder that records applied commands."""
        def __init__(self):
            self.commands = []

        def _record(self, tag, *a):
            self.commands.append((tag, a))
            return self

        def apt_install(self, *a, **k):        return self._record("apt_install", *a)
        def pip_install(self, *a, **k):        return self._record("pip_install", *a)
        def run_commands(self, *a, **k):       return self._record("run_commands", *a)
        def dockerfile_commands(self, *a, **k): return self._record("dockerfile", *a)
        def env(self, *a, **k):                return self._record("env", *a)

    class Image:
        @staticmethod
        def debian_slim(*a, **k):
            return _Image()

        @staticmethod
        def from_registry(*a, **k):
            return _Image()

    class _Secret:
        def __init__(self, kind, value):
            self.kind = kind
            self.value = value

    class Secret:
        @staticmethod
        def from_name(name, *a, **k):
            modal.NAMED_SECRETS.append(name)
            return _Secret("name", name)

        @staticmethod
        def from_dict(d, *a, **k):
            modal.DICT_SECRETS.append(dict(d))
            return _Secret("dict", dict(d))

    class _Volume:
        def __init__(self, name):
            self.name = name

        def __repr__(self):
            return f"Volume({self.name!r})"

    class Volume:
        @staticmethod
        def from_name(name, create_if_missing=False, *a, **k):
            modal.CREATED_VOLUMES.append((name, create_if_missing))
            return _Volume(name)

    class App:
        @staticmethod
        def lookup(name, create_if_missing=False, *a, **k):
            return types.SimpleNamespace(name=name)

    class _Tunnel:
        def __init__(self, port):
            self.url = f"https://example.invalid/{port}"
            # For SSH (TCP) tunnels, main() reads .tcp_socket.
            self.tcp_socket = ("ssh.example.invalid", 40000 + port)

    class _Sandbox:
        object_id = "sb-test-0001"

        def __init__(self, args, kwargs):
            self._ports = list(kwargs.get("encrypted_ports", [])) + \
                list(kwargs.get("unencrypted_ports", []))
            # Batch launches call wait()/returncode. Tests can steer the result
            # via modal.NEXT_RETURNCODE to exercise the failure path.
            self.returncode = None
            self.waited = False

        def tunnels(self):
            return {p: _Tunnel(p) for p in self._ports}

        def wait(self, *a, **k):
            self.waited = True
            self.returncode = modal.NEXT_RETURNCODE

    class Sandbox:
        @staticmethod
        def create(*args, **kwargs):
            modal.LAST_SANDBOX_ARGS = args
            modal.LAST_SANDBOX_KWARGS = kwargs
            modal.LAST_SANDBOX = _Sandbox(args, kwargs)
            return modal.LAST_SANDBOX

    def enable_output():
        return contextlib.nullcontext()

    modal.Image = Image
    modal.Secret = Secret
    modal.Volume = Volume
    modal.App = App
    modal.Sandbox = Sandbox
    modal.enable_output = enable_output
    return modal


# Only install the stub if the real package isn't already importable in a way
# the tests rely on. We always prefer the stub for hermetic, offline tests.
sys.modules["modal"] = _install_fake_modal()
