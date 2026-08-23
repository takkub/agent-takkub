"""#327 — the token-meter worker must survive its pane being closed mid-read.

Auto-captured on 1.0.79 (Windows): `RuntimeError: wrapped C/C++ object of type
AgentPane has been deleted` raised from `agent_pane.py:795`, inside the plain
`threading.Thread` named "token-meter". Emitting any signal on a QObject whose
C++ side is gone raises, and in a non-Qt thread that lands as an unhandled
exception rather than being ignored — which is how it reached the crash
reporter.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

import agent_takkub.agent_pane as agent_pane_mod
from agent_takkub.agent_pane import AgentPane
from agent_takkub.roles import by_name


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeTerminalWidget(QWidget):
    inputBytes = pyqtSignal(bytes)
    resized = pyqtSignal(int, int)
    fontSizeChanged = pyqtSignal(int)
    openInEditorRequested = pyqtSignal(str)

    def set_idle(self, idle: bool) -> None: ...
    def set_keepalive(self, active: bool) -> None: ...
    def set_input_locked(self, locked: bool) -> None: ...
    def set_cwd(self, cwd) -> None: ...
    def set_font_point_size(self, size: int) -> None: ...
    def write_bytes(self, data) -> None: ...
    def reset(self) -> None: ...
    def setFocus(self) -> None: ...
    def clear_view(self) -> None: ...
    def set_discard_enabled(self, enabled: bool) -> None: ...
    def set_discard_guard(self, guard) -> None: ...


@pytest.fixture(autouse=True)
def _fake_terminal(monkeypatch):
    monkeypatch.setattr(agent_pane_mod, "TerminalWidget", _FakeTerminalWidget)


def _make_pane(qapp) -> AgentPane:
    pane = AgentPane(by_name("backend"))
    pane._idle_clear_timer.stop()
    return pane


class _StubSignal:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls: list[tuple] = []

    def emit(self, *args) -> None:
        self.calls.append(args)
        if self.exc is not None:
            raise self.exc


class _StubPane:
    """`_emit_token_meter` touches nothing but `_tokenMeterReady`, so it can
    be exercised unbound — a real bound `pyqtBoundSignal.emit` is read-only
    and cannot be monkeypatched to raise."""

    def __init__(self, signal: _StubSignal) -> None:
        self._tokenMeterReady = signal


def _emit(signal: _StubSignal, *args) -> None:
    AgentPane._emit_token_meter(_StubPane(signal), *args)


def test_emit_on_a_deleted_pane_is_swallowed() -> None:
    signal = _StubSignal(RuntimeError("wrapped C/C++ object of type AgentPane has been deleted"))

    _emit(signal, None, None)  # must not raise

    assert signal.calls, "the guard must wrap the emit itself, not skip it"


def test_a_live_pane_still_gets_its_result() -> None:
    """The guard must not turn into a blanket 'never emit' — a pane that is
    still alive has to receive the reading it asked for."""
    signal = _StubSignal()

    _emit(signal, "sess.jsonl", {"input_tokens": 1})

    assert signal.calls == [("sess.jsonl", {"input_tokens": 1})]


def test_only_the_teardown_error_is_swallowed() -> None:
    """A real bug in the slot chain must still surface — swallowing every
    exception here would hide it in a thread nobody watches."""
    with pytest.raises(ValueError):
        _emit(_StubSignal(ValueError("genuine bug")), None, None)


def test_worker_routes_through_the_guard(qapp, monkeypatch) -> None:
    """The fix is only real if `_refresh_token_meter`'s worker calls the
    guarded path — a direct `_tokenMeterReady.emit` in the thread would
    reproduce #327 exactly."""
    pane = _make_pane(qapp)
    pane.session = MagicMock()
    pane._session_cwd = "C:/repo"
    pane.model.session_uuid = "abc-123"
    pane._token_refreshing = False
    monkeypatch.setattr(agent_pane_mod, "find_session_by_uuid", lambda *a, **k: None)

    calls: list[tuple] = []
    monkeypatch.setattr(pane, "_emit_token_meter", lambda *a: calls.append(a))

    class _InlineThread:
        def __init__(self, target=None, daemon=None, name=None):
            self._target = target

        def start(self) -> None:
            self._target()

    monkeypatch.setattr(agent_pane_mod.threading, "Thread", _InlineThread)

    pane._refresh_token_meter()

    assert calls == [(None, None)]
