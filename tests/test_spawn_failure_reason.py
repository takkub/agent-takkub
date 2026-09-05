"""#492: a spawn-failed Lead notice must carry enough to diagnose without
also having events.log open — the full exception text (never truncated), a
PTY output tail when the session managed to render anything before dying,
and a breadcrumb back to the matching `spawn_native_failed` event.log
record. See `spawn_engine._spawn_failure_reason`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_takkub.spawn_engine import _spawn_failure_reason


def test_full_exception_text_is_never_truncated() -> None:
    long_marker = "x" * 200
    long_path = f"C:\\Users\\monch\\.agent-takkub\\work{long_marker}\\worktrees\\backend-3"
    exc = NotADirectoryError(f"working directory does not exist: {long_path!r}")

    reason = _spawn_failure_reason(exc, role_name="backend", project_ns="proj", session=None)

    assert long_marker in reason
    assert reason.startswith(f"failed to spawn claude: {exc}")


def test_no_session_means_no_output_tail() -> None:
    reason = _spawn_failure_reason(
        RuntimeError("boom"), role_name="backend", project_ns="proj", session=None
    )

    assert "pty output" not in reason


def test_session_display_lines_that_cannot_be_iterated_is_swallowed() -> None:
    # PtySession is mocked out in the orch.spawn() test harness, so
    # display_lines() returns a bare MagicMock (not iterable) rather than a
    # list — must degrade to "no tail", not raise.
    session = MagicMock()
    session.display_lines.return_value = MagicMock()

    reason = _spawn_failure_reason(
        RuntimeError("boom"), role_name="backend", project_ns="proj", session=session
    )

    assert "pty output" not in reason


def test_output_tail_included_when_session_rendered_something() -> None:
    session = MagicMock()
    session.display_lines.return_value = ["", "line one", "  ", "line two", "line three"]

    reason = _spawn_failure_reason(
        RuntimeError("boom"), role_name="backend", project_ns="proj", session=session
    )

    assert "pty output" in reason
    assert "line one" in reason
    assert "line two" in reason
    assert "line three" in reason


def test_breadcrumb_names_event_role_and_project() -> None:
    reason = _spawn_failure_reason(
        RuntimeError("boom"), role_name="backend", project_ns="myproj", session=None
    )

    assert "events.log" in reason
    assert "spawn_native_failed" in reason
    assert "role=backend" in reason
    assert "project=myproj" in reason
