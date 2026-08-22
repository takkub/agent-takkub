"""#345 follow-up: proves conftest.py's #344 QTimer-leak tracker (now
promoted from a non-fatal terminal-summary section to a real `pytest.fail`)
actually detects a leaked timer and builds a diagnosable failure message —
without needing to trigger a real session failure to check it.

`_qt_session_app`'s teardown (the thing that actually calls `pytest.fail`)
can't be exercised directly here — it only runs once, at the very end of
the whole pytest session — so this instead proves the two pieces it's built
from: `_stop_leaked_qtimers` correctly records+stops a timer left active
past its owning code's teardown, and `_qtimer_leak_failure_message` turns
that record into a message naming the offending timer.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QCoreApplication, QTimer

from tests.conftest import (
    _leaked_timer_reports,
    _qtimer_leak_failure_message,
    _stop_leaked_qtimers,
)


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def test_stop_leaked_qtimers_records_and_stops_active_timer(qapp) -> None:
    before = len(_leaked_timer_reports)
    timer = QTimer()
    timer.setObjectName("proof-leak-timer")
    timer.start(60_000)
    assert timer.isActive()

    try:
        _stop_leaked_qtimers()

        assert not timer.isActive(), "leaked timer must be stopped, not just reported"
        assert len(_leaked_timer_reports) == before + 1
        recorded = _leaked_timer_reports[-1]
        assert "proof-leak-timer" in recorded
        assert "interval=60000ms" in recorded
    finally:
        # Don't let this deliberate proof-leak fail the real session check
        # at `_qt_session_app` teardown (mirrors pop_qt_slot_exceptions()'s
        # drain in test_provider_toggle_orchestrator.py for the sibling
        # exception-escape guard).
        del _leaked_timer_reports[before:]


def test_qtimer_leak_failure_message_none_when_no_leaks() -> None:
    assert _qtimer_leak_failure_message([]) is None


def test_qtimer_leak_failure_message_names_the_leaking_test_and_interval() -> None:
    msg = _qtimer_leak_failure_message(
        [
            "tests/test_foo.py::test_bar: leaked active QTimer(interval=5000ms, "
            "objectName='<unnamed>')"
        ]
    )
    assert msg is not None
    assert "1 QTimer(s)" in msg
    assert "tests/test_foo.py::test_bar" in msg
    assert "interval=5000ms" in msg


def test_qtimer_leak_failure_message_truncates_past_fifty() -> None:
    reports = [f"tests/test_x.py::test_{i}: leaked active QTimer(interval=1ms)" for i in range(60)]
    msg = _qtimer_leak_failure_message(reports)
    assert msg is not None
    assert "test_49" in msg
    assert "test_50" not in msg
    assert "... and 10 more" in msg
