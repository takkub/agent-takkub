"""Regression for issue #511 (macOS): `_PosixBackend.isalive()` used to let
a `waitpid()` ECHILD race crash straight up to the pane's read loop as an
unhandled `ptyprocess.util.PtyProcessError` ("did someone else call
waitpid() on our process?").

ptyprocess's own `isalive()` calls `os.waitpid(self.pid, ...)` internally;
if the child was already reaped by something else first, that raises
`ChildProcessError` (ECHILD), which ptyprocess itself catches and re-raises
as its own `PtyProcessError` instead of returning cleanly. Either shape
means the same thing: no waitpid-able child left == the process is
definitively gone, so `isalive()` must return False, not propagate.

The `_PosixBackend` class is plain Python (only its `spawn()` classmethod
does a platform-gated import), so it can be instantiated and exercised
directly on any OS — no real ptyprocess/POSIX pty required.
"""

from __future__ import annotations

import sys
import types

import pytest

from agent_takkub._pty_backend import _PosixBackend


class _FakeProc:
    def __init__(self, isalive_effect):
        self._isalive_effect = isalive_effect

    def isalive(self):
        if isinstance(self._isalive_effect, BaseException):
            raise self._isalive_effect
        return self._isalive_effect


def test_isalive_passes_through_normal_result():
    backend = _PosixBackend(_FakeProc(True))
    assert backend.isalive() is True

    backend = _PosixBackend(_FakeProc(False))
    assert backend.isalive() is False


def test_isalive_treats_bare_child_process_error_as_dead():
    backend = _PosixBackend(_FakeProc(ChildProcessError("[Errno 10] No child processes")))
    assert backend.isalive() is False


def test_isalive_treats_process_lookup_error_as_dead():
    backend = _PosixBackend(_FakeProc(ProcessLookupError("[Errno 3] No such process")))
    assert backend.isalive() is False


def test_isalive_treats_wrapped_ptyprocess_error_as_dead(monkeypatch):
    """The actual shape from issue #511: ptyprocess's own isalive() already
    caught the ECHILD OSError and re-raised it as *its* PtyProcessError —
    our wrapper only ever sees that outer type."""

    class _FakePtyProcessError(Exception):
        pass

    fake_module = types.ModuleType("ptyprocess")
    fake_module.PtyProcessError = _FakePtyProcessError
    monkeypatch.setitem(sys.modules, "ptyprocess", fake_module)

    backend = _PosixBackend(
        _FakeProc(
            _FakePtyProcessError(
                'isalive() encountered condition where "terminated" is 0, but '
                "there was no child process. Did someone else call waitpid() "
                "on our process?"
            )
        )
    )
    assert backend.isalive() is False


def test_isalive_reraises_unrelated_errors(monkeypatch):
    """Only the ECHILD race is swallowed — any other failure must still
    surface, not be silently treated as "process is dead"."""

    class _FakePtyProcessError(Exception):
        pass

    fake_module = types.ModuleType("ptyprocess")
    fake_module.PtyProcessError = _FakePtyProcessError
    monkeypatch.setitem(sys.modules, "ptyprocess", fake_module)

    backend = _PosixBackend(_FakeProc(ValueError("something else entirely")))
    with pytest.raises(ValueError):
        backend.isalive()
