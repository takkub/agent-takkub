"""PtySession.spawn() integration with the bounded native spawn (#139).

On a native-spawn timeout, PtySession.spawn() must raise (not hang) and leave
the session in a clean not-alive state with no reader/writer thread wired —
so the existing spawn-failure cleanup path in spawn_engine.py (session.terminate,
setParent(None), deleteLater, pane-token revoke, _spawn_in_progress reset +
queue drain) works unchanged, same as any other native-spawn failure.
"""

from __future__ import annotations

import pytest

import agent_takkub.pty_session as pty_session_mod
from agent_takkub._pty_backend import PtySpawnTimeout
from agent_takkub.pty_session import PtySession


class _FakeProc:
    """Minimal stand-in for the native proc wrapper. Reports EOF/not-alive
    immediately so the real reader thread PtySession.spawn() starts exits its
    very first loop iteration on its own — these tests only care that
    PtySession wires spawn_pty_bounded correctly, not the full PTY read/write
    lifecycle. No `pid` attribute → PtySession._pid resolves to None, so
    terminate()'s tree-kill never shells out to a real (bogus) PID."""

    def isalive(self) -> bool:
        return False

    def read(self, size: int) -> bytes:
        raise EOFError()

    def terminate(self, force: bool = False) -> None:
        pass


def test_ptysession_spawn_raises_on_native_timeout(monkeypatch) -> None:
    def _boom(*a, **kw):
        raise PtySpawnTimeout("native pty spawn exceeded 0.1s (argv[0]='claude')")

    monkeypatch.setattr(pty_session_mod, "spawn_pty_bounded", _boom)

    s = PtySession(cols=80, rows=24)
    with pytest.raises(PtySpawnTimeout):
        s.spawn(["claude"], cwd=None, env={})

    assert s.is_alive is False
    assert s._proc is None
    assert s._reader is None
    assert s._writer is None


def test_ptysession_spawn_passes_configured_timeout(monkeypatch) -> None:
    captured: dict = {}

    def _capture(argv, cwd, env, rows, cols, timeout_sec):
        captured["timeout_sec"] = timeout_sec
        captured["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(pty_session_mod, "spawn_pty_bounded", _capture)
    monkeypatch.setattr(pty_session_mod, "PTY_SPAWN_TIMEOUT_SEC", 12.5)

    s = PtySession(cols=80, rows=24)
    try:
        s.spawn(["claude", "--flag"], cwd=None, env={})
        assert captured["timeout_sec"] == 12.5
        assert captured["argv"] == ["claude", "--flag"]
    finally:
        s.terminate(wait=True)


def test_ptysession_spawn_still_succeeds_on_fast_native_call(monkeypatch) -> None:
    """Sanity: the bounded wrapper must not break the ordinary fast-success
    path — reader/writer threads still get wired up normally."""
    monkeypatch.setattr(pty_session_mod, "spawn_pty_bounded", lambda *a, **kw: _FakeProc())

    s = PtySession(cols=80, rows=24)
    s.spawn(["claude"], cwd=None, env={})
    try:
        assert s.is_alive is True
        assert s._proc is not None
        assert s._pid is None  # _FakeProc has no `pid` attribute
    finally:
        s.terminate(wait=True)
