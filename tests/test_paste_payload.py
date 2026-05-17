"""Tests for `_paste_payload`, the bracketed-paste wrapper used by every
cockpit-driven write into a pane's PTY.

Short messages must pass through unchanged so single-keystroke prompts
feel like typing. Long messages must be wrapped with the standard
xterm bracketed-paste markers (`ESC [200~ ... ESC [201~`) so claude
code treats the whole payload as one atomic paste — without that
wrapping the head of long task specs gets lost when the pane is
mid-render at write time, which is the bug behind teammates
complaining about "ข้อความถูกตัดส่วนต้น".
"""

from __future__ import annotations

from agent_takkub.orchestrator import (
    BRACKETED_PASTE_THRESHOLD,
    _PASTE_END,
    _PASTE_ENTER_DELAY_MS,
    _PASTE_START,
    _TYPING_ENTER_DELAY_MS,
    _enter_delay_ms,
    _paste_payload,
)


class TestPastePayload:
    def test_short_message_is_unchanged(self) -> None:
        text = "hi"
        assert _paste_payload(text) == text

    def test_threshold_minus_one_is_unchanged(self) -> None:
        text = "x" * (BRACKETED_PASTE_THRESHOLD - 1)
        assert _paste_payload(text) == text

    def test_threshold_exact_is_wrapped(self) -> None:
        text = "x" * BRACKETED_PASTE_THRESHOLD
        wrapped = _paste_payload(text)
        assert wrapped.startswith(_PASTE_START)
        assert wrapped.endswith(_PASTE_END)
        assert text in wrapped

    def test_long_message_is_wrapped(self) -> None:
        text = "a" * 5_000
        wrapped = _paste_payload(text)
        assert wrapped.startswith(_PASTE_START)
        assert wrapped.endswith(_PASTE_END)
        # The original payload survives intact between the markers.
        assert wrapped[len(_PASTE_START):-len(_PASTE_END)] == text

    def test_paste_markers_are_canonical_xterm(self) -> None:
        # Sanity check the constants themselves so an accidental edit to
        # the escapes doesn't ship a non-functional paste sequence.
        assert _PASTE_START == "\x1b[200~"
        assert _PASTE_END == "\x1b[201~"

    def test_paste_is_idempotent_for_short_text(self) -> None:
        # Defence against accidental double-wrapping when a caller has
        # already invoked the helper. Short strings don't change shape.
        text = "ack"
        once = _paste_payload(text)
        twice = _paste_payload(once)
        assert once == twice == text

    def test_thai_unicode_survives_wrapping(self) -> None:
        # Long Thai payload — the common case in Lead → teammate spec
        # sends. We want every character to land in the wrapped block
        # without re-encoding mishaps.
        text = "ก" * 500
        wrapped = _paste_payload(text)
        assert wrapped.startswith(_PASTE_START)
        assert wrapped.endswith(_PASTE_END)
        assert text in wrapped


class TestEnterDelay:
    """Post-write delay before the submitting `\\r` is scaled by whether
    the payload was bracket-pasted. Claude Code v2.1.x renders the
    `[Pasted text #N +M lines]` placeholder for bracketed-paste blocks,
    and an Enter arriving mid-render is consumed as a soft newline —
    which is the bug behind teammate panes sitting at the placeholder
    forever instead of running the spec."""

    def test_short_payload_uses_typing_delay(self) -> None:
        assert _enter_delay_ms("hi") == _TYPING_ENTER_DELAY_MS

    def test_empty_payload_uses_typing_delay(self) -> None:
        # Edge case: empty string still goes through the typing path
        # (no `_PASTE_START` prefix to detect).
        assert _enter_delay_ms("") == _TYPING_ENTER_DELAY_MS

    def test_long_unwrapped_payload_uses_typing_delay(self) -> None:
        # A long payload that *wasn't* wrapped (e.g. caller bypassed
        # `_paste_payload`) should still get the typing delay — the
        # decision is based on the actual escape prefix, not length.
        assert _enter_delay_ms("x" * 5_000) == _TYPING_ENTER_DELAY_MS

    def test_bracketed_payload_uses_paste_delay(self) -> None:
        payload = _PASTE_START + "x" * 500 + _PASTE_END
        assert _enter_delay_ms(payload) == _PASTE_ENTER_DELAY_MS

    def test_paste_payload_output_round_trip(self) -> None:
        # The two helpers compose: whatever `_paste_payload` returns
        # for a long input must read back as the paste delay, and for
        # a short input as the typing delay. This is the contract the
        # wire-ups in orchestrator.py depend on.
        long_text = "a" * (BRACKETED_PASTE_THRESHOLD + 100)
        assert _enter_delay_ms(_paste_payload(long_text)) == _PASTE_ENTER_DELAY_MS

        short_text = "ack"
        assert _enter_delay_ms(_paste_payload(short_text)) == _TYPING_ENTER_DELAY_MS

    def test_paste_delay_is_longer_than_typing_delay(self) -> None:
        # Guard against an accidental edit that swaps the two constants.
        # The whole point of the helper is that paste > typing.
        assert _PASTE_ENTER_DELAY_MS > _TYPING_ENTER_DELAY_MS
