"""Regression tests for is_at_ready_prompt() busy/idle detection.

The idle watchdog (orchestrator._check_idle_teammates) fires `takkub done`
reminders into any pane that is_at_ready_prompt() reports as idle. gemini and
codex keep their "type your message or @path" input box visible *even while
they are Thinking…* — so the busy state must be detected via the
"esc to cancel" indicator, not the absence of the input box. Without that,
a thinking gemini reads as idle and the watchdog floods it with reminders
(the 2026-05-30 reminder-pileup + search-loop incident).
"""

from __future__ import annotations

from agent_takkub.pty_session import PtySession


def _feed_screen(*lines: str) -> PtySession:
    s = PtySession(cols=80, rows=24)
    s._feed_and_log(("\r\n".join(lines)).encode())
    return s


def test_gemini_thinking_with_input_box_is_not_ready() -> None:
    # gemini renders the input prompt AND the cancel indicator at once.
    s = _feed_screen(
        "Thinking... (esc to cancel, 1h 1m 48s)",
        "Type your message or @path/to/file",
    )
    assert s.is_at_ready_prompt() is False


def test_gemini_idle_input_box_only_is_ready() -> None:
    s = _feed_screen("Type your message or @path/to/file")
    assert s.is_at_ready_prompt() is True


def test_claude_working_esc_to_interrupt_is_not_ready() -> None:
    # Regression guard for the pre-existing claude busy indicator.
    s = _feed_screen("(esc to interrupt) building...", "bypass permissions")
    assert s.is_at_ready_prompt() is False
