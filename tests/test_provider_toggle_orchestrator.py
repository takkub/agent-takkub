"""Integration smoke test: toggle_provider() writes state + emits signal.

Verifies the orchestrator method without spawning a full Lead pane —
the broadcast-into-pane path is exercised only when a live pane exists,
which is covered by manual smoke testing in spec section 8.2.

Note: pytest-qt (qtbot fixture) is not installed in this repo. Tests use
plain PyQt6 signal connection via a Python list to capture emissions.
"""

from __future__ import annotations

import pytest

from agent_takkub import provider_state


@pytest.fixture
def tmp_state_path(tmp_path, monkeypatch):
    path = tmp_path / "disabled-providers.json"
    monkeypatch.setattr(provider_state, "_PATH", path)
    return path


def _stop_all_orchestrator_timers(orch) -> None:
    """#344: stop every QTimer Orchestrator.__init__ arms unconditionally,
    not just _idle_watchdog — see the callers below for why."""
    orch._idle_watchdog.stop()
    orch._resource_timer.stop()
    orch._hot_md_timer.stop()


def test_toggle_provider_persists_and_emits(tmp_state_path):
    """toggle_provider() writes to disk and emits providerStateChanged."""
    from agent_takkub.orchestrator import Orchestrator

    orch = Orchestrator()
    # #344: Orchestrator.__init__ arms THREE QTimers unconditionally
    # (_idle_watchdog, _resource_timer, _hot_md_timer), all of which keep
    # firing on the shared session QApplication after this test's fixtures
    # (tmp_state_path, monkeypatch) tear down otherwise — the leak that
    # produced a CI-observed abort. Stopping all three here (not just
    # _idle_watchdog, this file's original holdout) closes it fully.
    _stop_all_orchestrator_timers(orch)
    received: list[tuple[str, bool]] = []
    orch.providerStateChanged.connect(lambda p, d: received.append((p, d)))

    ok, msg = orch.toggle_provider("codex", True)
    assert ok
    assert "disabled" in msg.lower()
    assert provider_state.is_disabled("codex") is True
    assert received == [("codex", True)]

    ok, msg = orch.toggle_provider("codex", False)
    assert ok
    assert provider_state.is_disabled("codex") is False
    assert received == [("codex", True), ("codex", False)]


def test_toggle_provider_rejects_unknown(tmp_state_path):
    from agent_takkub.orchestrator import Orchestrator

    orch = Orchestrator()
    _stop_all_orchestrator_timers(orch)  # #344, see rationale above
    ok, msg = orch.toggle_provider("bogus", True)
    assert not ok
    assert "unknown provider" in msg


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtCore import QCoreApplication

    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def test_leaked_qtimer_slot_exception_is_diagnosable(qapp):
    """#344 proof: the session-wide Qt exception guard (conftest.py's
    `_qt_session_app` + `pop_qt_slot_exceptions`) turns an exception raised
    inside a Qt slot into a readable, queued failure instead of PyQt6's
    silent qFatal() process abort — the exact mechanism that killed CI when a
    leaked `_idle_watchdog` (the bug the two tests above now fix) fired its
    slot after the owning test's state was gone.

    Uses `timeout.emit()` directly rather than starting a real timer and
    pumping the event loop: `.emit()` already invokes the connected slot
    through the same C++ dispatch path a firing QTimer uses, which is the
    exact path PyQt6 hard-aborts on — no live timer tick needed to prove it.
    """
    from PyQt6.QtCore import QTimer

    from tests.conftest import pop_qt_slot_exceptions

    pop_qt_slot_exceptions()  # start from a clean queue

    timer = QTimer()

    def _boom() -> None:
        raise RuntimeError("simulated leaked-timer slot exception (#344)")

    timer.timeout.connect(_boom)
    try:
        timer.timeout.emit()  # would hard-abort the whole pytest process without the guard
    finally:
        timer.timeout.disconnect(_boom)

    leaked = pop_qt_slot_exceptions()
    assert len(leaked) == 1
    assert "RuntimeError: simulated leaked-timer slot exception (#344)" in leaked[0]
