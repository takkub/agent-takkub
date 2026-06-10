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


def test_gemini_idle_with_update_footer_is_ready() -> None:
    # issue #51: once a newer gemini release exists upstream, gemini shows a
    # PASSIVE "Gemini CLI update available! <cur> → <new>" footer that does
    # NOT block input. A ready gemini wearing this banner must still read as
    # idle so the watchdog can nudge it to run `takkub done`. Previously the
    # blanket "update available!" blocker made it read as perpetually-busy.
    s = _feed_screen(
        "Gemini CLI update available! 0.46.0 → 0.47.0",
        "Type your message or @path/to/file",
    )
    assert s.is_at_ready_prompt() is True


def test_gemini_thinking_with_update_footer_is_not_ready() -> None:
    # The update footer must not flip a *thinking* gemini to ready — the
    # "esc to cancel" busy indicator still takes precedence.
    s = _feed_screen(
        "Gemini CLI update available! 0.46.0 → 0.47.0",
        "Thinking... (esc to cancel, 12s)",
        "Type your message or @path/to/file",
    )
    assert s.is_at_ready_prompt() is False


def test_codex_splash_update_modal_is_not_ready() -> None:
    # codex's "update available!" is part of a startup splash modal that must
    # be dismissed before the prompt is usable — it must still block (the
    # gemini ready marker is absent on a codex screen, so the blocker applies).
    s = _feed_screen(
        "OpenAI Codex (v1.2.3)",
        "update available! run npm i -g @openai/codex",
    )
    assert s.is_at_ready_prompt() is False
