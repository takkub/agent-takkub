"""Tests for the pure Lead draft-typing state machine (issue #3).

Scope: byte-level transitions only — no Qt, no Orchestrator. Integration
(gate wired into _pump_lead_notify / _flush_pending_lead_cc / _on_pane_input)
is covered by test_lead_draft_guard.py.
"""

from __future__ import annotations

from agent_takkub.lead_draft_state import (
    DRAFT_HOLD_TIMEOUT_S,
    EMPTY,
    NONEMPTY,
    UNKNOWN_NONEMPTY,
    LeadDraftState,
    advance_draft_state,
    draft_hold_expired,
    draft_state_allows_injection,
)

NOW = 1_000_000.0


def _advance(state: LeadDraftState, data: bytes, now: float = NOW) -> LeadDraftState:
    return advance_draft_state(state, data, now)


class TestPrintableText:
    def test_typing_ascii_marks_nonempty(self):
        st = _advance(LeadDraftState(), b"hello")
        assert st.state == NONEMPTY
        assert st.draft_len == 5

    def test_pending_since_stamped_on_first_char(self):
        st = _advance(LeadDraftState(), b"h", now=NOW)
        assert st.pending_since == NOW

    def test_pending_since_not_restamped_on_further_typing(self):
        st = _advance(LeadDraftState(), b"h", now=NOW)
        st2 = _advance(st, b"i", now=NOW + 10)
        assert st2.pending_since == NOW
        assert st2.draft_len == 2


class TestBackspaceToEmpty:
    def test_n_chars_then_n_backspaces_clears(self):
        st = _advance(LeadDraftState(), b"abc")
        assert st.state == NONEMPTY and st.draft_len == 3
        st = _advance(st, b"\x7f\x7f\x7f")
        assert st.state == EMPTY
        assert st.draft_len == 0
        assert st.pending_since == 0.0

    def test_partial_backspace_stays_nonempty(self):
        st = _advance(LeadDraftState(), b"abc")
        st = _advance(st, b"\x7f")
        assert st.state == NONEMPTY
        assert st.draft_len == 2

    def test_backspace_on_empty_is_noop(self):
        st = _advance(LeadDraftState(), b"\x7f")
        assert st.state == EMPTY
        assert st.draft_len == 0

    def test_ctrl_h_backspace_variant_also_clears(self):
        st = _advance(LeadDraftState(), b"a")
        st = _advance(st, b"\x08")
        assert st.state == EMPTY


class TestExplicitClears:
    def test_enter_cr_clears(self):
        st = _advance(LeadDraftState(), b"task text")
        st = _advance(st, b"\r")
        assert st.state == EMPTY
        assert st.draft_len == 0

    def test_enter_lf_clears(self):
        st = _advance(LeadDraftState(), b"task text")
        st = _advance(st, b"\n")
        assert st.state == EMPTY

    def test_esc_clears_in_one_call_when_next_byte_rules_out_a_sequence(self):
        st = _advance(LeadDraftState(), b"draft")
        st = _advance(st, b"\x1bx")  # Esc + a byte that can't start CSI/SS3
        assert st.state == NONEMPTY
        assert st.draft_len == 1  # "draft" cleared, "x" typed anew

    def test_lone_esc_stays_ambiguous_until_the_next_byte_arrives(self):
        """A standalone Esc keypress can be delivered as a single byte with
        nothing else in that read() — indistinguishable, from the byte
        stream alone, from the opening byte of a CSI/SS3/mouse sequence a
        PTY under load split across two reads. It can only be proven bare
        once a following byte rules out '['/'O' (issue #111); resolving
        eagerly is exactly the bug this guards against."""
        st = _advance(LeadDraftState(), b"draft")
        st = _advance(st, b"\x1b")
        assert st.state == NONEMPTY  # not yet proven — still holding "draft"
        assert st.draft_len == 5
        st = _advance(st, b"x")  # next byte proves it: genuine bare Esc
        assert st.state == NONEMPTY
        assert st.draft_len == 1

    def test_ctrl_c_clears(self):
        st = _advance(LeadDraftState(), b"draft")
        st = _advance(st, b"\x03")
        assert st.state == EMPTY

    def test_ctrl_u_clears(self):
        st = _advance(LeadDraftState(), b"draft")
        st = _advance(st, b"\x15")
        assert st.state == EMPTY

    def test_clears_also_reset_unknown_nonempty(self):
        st = _advance(LeadDraftState(), b"\x1b[A")  # Up arrow -> unknown hold
        assert st.state == UNKNOWN_NONEMPTY
        st = _advance(st, b"\r")
        assert st.state == EMPTY


class TestArrowsAndHistoryRecall:
    def test_up_arrow_from_empty_becomes_unknown_nonempty(self):
        st = _advance(LeadDraftState(), b"\x1b[A")
        assert st.state == UNKNOWN_NONEMPTY

    def test_down_arrow_becomes_unknown_nonempty(self):
        st = _advance(LeadDraftState(), b"\x1b[B")
        assert st.state == UNKNOWN_NONEMPTY

    def test_ss3_up_arrow_variant_becomes_unknown_nonempty(self):
        st = _advance(LeadDraftState(), b"\x1bOA")
        assert st.state == UNKNOWN_NONEMPTY

    def test_left_arrow_is_noop(self):
        st = _advance(LeadDraftState(), b"abc")
        st2 = _advance(st, b"\x1b[D")
        assert st2 == st

    def test_right_arrow_is_noop(self):
        st = _advance(LeadDraftState(), b"abc")
        st2 = _advance(st, b"\x1b[C")
        assert st2 == st

    def test_left_arrow_on_empty_stays_empty(self):
        st = _advance(LeadDraftState(), b"\x1b[D")
        assert st.state == EMPTY

    def test_unknown_nonempty_ignores_further_typing_and_backspace(self):
        st = _advance(LeadDraftState(), b"\x1b[A")
        assert st.state == UNKNOWN_NONEMPTY
        st = _advance(st, b"xyz\x7f\x7f\x7f\x7f\x7f")
        assert st.state == UNKNOWN_NONEMPTY


class TestCtrlAE:
    def test_ctrl_a_is_noop(self):
        st = _advance(LeadDraftState(), b"abc")
        st2 = _advance(st, b"\x01")
        assert st2 == st

    def test_ctrl_e_is_noop(self):
        st = _advance(LeadDraftState(), b"abc")
        st2 = _advance(st, b"\x05")
        assert st2 == st


class TestBracketedPaste:
    def test_paste_start_marker_becomes_unknown_nonempty(self):
        st = _advance(LeadDraftState(), b"\x1b[200~pasted content\x1b[201~")
        assert st.state == UNKNOWN_NONEMPTY

    def test_paste_content_not_counted_toward_draft_len(self):
        st = _advance(LeadDraftState(), b"\x1b[200~" + b"x" * 500 + b"\x1b[201~")
        assert st.state == UNKNOWN_NONEMPTY
        # draft_len tracking is meaningless in unknown_nonempty; the state
        # itself (not the length) is what the injection gate reads.
        assert draft_state_allows_injection(st) is False


class TestUnrecognizedCsi:
    def test_home_end_delete_do_not_change_state(self):
        st = _advance(LeadDraftState(), b"abc")
        for seq in (b"\x1b[H", b"\x1b[F", b"\x1b[3~", b"\x1b[5~"):
            st2 = _advance(st, seq)
            assert st2 == st, f"{seq!r} must not change draft state"


class TestFocusInOut:
    """xterm focus-tracking sequences (\\x1b[I focus-in, \\x1b[O focus-out) are
    plain CSI with no final-byte special-case — they must be pure no-ops, not
    a false-positive nonempty (#108 investigation: ruled out as the cause of
    a draft reading pending for minutes, but pinned here as a regression)."""

    def test_focus_in_is_noop_on_empty(self):
        st = _advance(LeadDraftState(), b"\x1b[I")
        assert st.state == EMPTY

    def test_focus_out_is_noop_on_empty(self):
        st = _advance(LeadDraftState(), b"\x1b[O")
        assert st.state == EMPTY

    def test_focus_in_out_do_not_disturb_existing_draft(self):
        st = _advance(LeadDraftState(), b"abc")
        st2 = _advance(st, b"\x1b[O\x1b[I")
        assert st2 == st

    def test_focus_events_around_typing_do_not_inflate_length(self):
        st = _advance(LeadDraftState(), b"\x1b[Oab\x1b[Ic")
        assert st.state == NONEMPTY
        assert st.draft_len == 3


class TestMouseSequences:
    """SGR (`\\x1b[<Cb;Cx;CyM`/`m`) and legacy X10 (`\\x1b[M` + 3 raw bytes)
    mouse reports must be pure no-ops — their digit/';'/M/m bytes were
    previously mis-counted as typed characters since `_CSI` requires
    digits/';' right after `[` and never matches the `<` SGR prefix (#108
    root cause: a single mouse press/release read as 10 "typed" chars)."""

    def test_sgr_mouse_press_is_noop_on_empty(self):
        st = _advance(LeadDraftState(), b"\x1b[<0;42;13M")
        assert st.state == EMPTY
        assert st.draft_len == 0

    def test_sgr_mouse_release_is_noop(self):
        st = _advance(LeadDraftState(), b"\x1b[<0;42;13m")
        assert st.state == EMPTY

    def test_sgr_mouse_does_not_disturb_existing_draft(self):
        st = _advance(LeadDraftState(), b"abc")
        st2 = _advance(st, b"\x1b[<0;42;13M\x1b[<0;42;13m")
        assert st2 == st

    def test_sgr_mouse_wheel_burst_is_noop(self):
        st = _advance(LeadDraftState(), b"abc")
        wheel = b"\x1b[<64;10;20M" * 8  # rapid wheel ticks (button code 64+)
        st2 = _advance(st, wheel)
        assert st2 == st

    def test_sgr_mouse_drag_sequence_is_noop(self):
        st = _advance(LeadDraftState(), b"abc")
        drag = b"\x1b[<32;1;1M\x1b[<32;2;2M\x1b[<32;3;3M\x1b[<0;3;3m"  # button-32 = motion-while-pressed
        st2 = _advance(st, drag)
        assert st2 == st

    def test_legacy_x10_mouse_is_noop_on_empty(self):
        st = _advance(LeadDraftState(), b"\x1b[M" + bytes([0x20, 0x21, 0x22]))
        assert st.state == EMPTY
        assert st.draft_len == 0

    def test_legacy_x10_mouse_does_not_disturb_existing_draft(self):
        st = _advance(LeadDraftState(), b"abc")
        st2 = _advance(st, b"\x1b[M" + bytes([0x20, 0x21, 0x22]))
        assert st2 == st

    def test_typing_around_mouse_events_counts_only_typed_chars(self):
        st = _advance(LeadDraftState(), b"ab\x1b[<0;42;13Mcd\x1b[<0;42;13me")
        assert st.state == NONEMPTY
        assert st.draft_len == 5


class TestMultibyteThai:
    def test_thai_chars_counted_as_characters_not_bytes(self):
        thai = "สวัสดี"  # 6 Thai characters, >1 byte each in UTF-8
        st = _advance(LeadDraftState(), thai.encode("utf-8"))
        assert st.state == NONEMPTY
        assert st.draft_len == len(thai)

    def test_one_backspace_per_thai_character_reaches_empty(self):
        thai = "สวัสดี"
        st = _advance(LeadDraftState(), thai.encode("utf-8"))
        st = _advance(st, b"\x7f" * len(thai))
        assert st.state == EMPTY
        assert st.draft_len == 0


class TestSplitMouseSequences:
    """A PTY under load can flush a single logical write across two
    separate reads — if the split lands inside a mouse-report escape
    sequence, the first half matches nothing on its own, and (pre-fix) the
    second half fell through to the printable-run branch and got counted as
    typed characters that could never be un-typed by any real keystroke,
    leaving the draft stuck forever (issue #111, 100% repro). Every interior
    split point of the sequence must reassemble losslessly across the two
    `advance_draft_state()` calls."""

    SGR = b"\x1b[<0;42;13M"

    def test_sgr_split_at_every_interior_byte_reassembles_to_noop(self):
        for cut in range(1, len(self.SGR)):
            first, second = self.SGR[:cut], self.SGR[cut:]
            st = _advance(LeadDraftState(), first)
            st = _advance(st, second)
            assert st.state == EMPTY, f"cut={cut} first={first!r} second={second!r}"
            assert st.draft_len == 0, f"cut={cut}"
            assert st.pending_tail == b"", f"cut={cut}"

    def test_sgr_split_does_not_disturb_existing_draft(self):
        for cut in range(1, len(self.SGR)):
            first, second = self.SGR[:cut], self.SGR[cut:]
            st = _advance(LeadDraftState(), b"abc")
            st = _advance(st, first)
            st = _advance(st, second)
            assert st.state == NONEMPTY, f"cut={cut}"
            assert st.draft_len == 3, f"cut={cut}"

    X10 = b"\x1b[M" + bytes([0x20, 0x21, 0x22])

    def test_x10_legacy_split_at_every_interior_byte_reassembles_to_noop(self):
        for cut in range(1, len(self.X10)):
            first, second = self.X10[:cut], self.X10[cut:]
            st = _advance(LeadDraftState(), first)
            st = _advance(st, second)
            assert st.state == EMPTY, f"cut={cut} first={first!r} second={second!r}"
            assert st.draft_len == 0, f"cut={cut}"
            assert st.pending_tail == b"", f"cut={cut}"

    def test_sgr_split_byte_at_a_time_streaming(self):
        st = LeadDraftState()
        for byte in self.SGR:
            st = _advance(st, bytes([byte]))
        assert st.state == EMPTY
        assert st.draft_len == 0
        assert st.pending_tail == b""


class TestSplitMultibyteThai:
    def test_thai_char_split_across_chunk_boundary_still_counts_as_one(self):
        thai = "สวัสดี"
        encoded = thai.encode("utf-8")
        for cut in range(1, len(encoded)):
            first, second = encoded[:cut], encoded[cut:]
            st = _advance(LeadDraftState(), first)
            st = _advance(st, second)
            assert st.state == NONEMPTY, f"cut={cut}"
            assert st.draft_len == len(thai), f"cut={cut} got {st.draft_len}"
            assert st.pending_tail == b"", f"cut={cut}"

    def test_thai_char_split_byte_at_a_time_streaming(self):
        thai = "สวัสดี"
        encoded = thai.encode("utf-8")
        st = LeadDraftState()
        for byte in encoded:
            st = _advance(st, bytes([byte]))
        assert st.state == NONEMPTY
        assert st.draft_len == len(thai)
        assert st.pending_tail == b""


class TestInjectionGate:
    def test_none_state_allows_injection(self):
        assert draft_state_allows_injection(None) is True

    def test_empty_state_allows_injection(self):
        assert draft_state_allows_injection(LeadDraftState()) is True

    def test_nonempty_blocks_injection(self):
        assert draft_state_allows_injection(LeadDraftState(state=NONEMPTY, draft_len=1)) is False

    def test_unknown_nonempty_blocks_injection(self):
        assert draft_state_allows_injection(LeadDraftState(state=UNKNOWN_NONEMPTY)) is False


class TestWordDeleteCtrlW:
    """Ctrl+W ('delete previous word', 0x17) — issue #114 A3-secondary. Prior
    behavior treated it as an untracked no-op, which overcounted draft_len
    and could leave state stuck "nonempty" (or, worse, only reach "empty"
    after backspacing *past* the real end of text — see module docstring for
    why overcounting, not undercounting, is the safe direction)."""

    def test_ctrl_w_deletes_the_word_just_typed(self):
        st = _advance(LeadDraftState(), b"takkub status")
        st = _advance(st, b"\x17")
        assert st.state == NONEMPTY
        assert st.draft_len == 7  # "takkub " remains

    def test_typed_word_ctrl_w_then_backspaces_reaches_empty_without_underflow(self):
        st = _advance(LeadDraftState(), b"takkub status")
        st = _advance(st, b"\x17")  # deletes "status" -> "takkub "
        assert st.draft_len == 7
        st = _advance(st, b"\x7f" * 10)  # far more backspaces than remain
        assert st.state == EMPTY
        assert st.draft_len == 0

    def test_ctrl_w_on_empty_is_noop(self):
        st = _advance(LeadDraftState(), b"\x17")
        assert st.state == EMPTY
        assert st.draft_len == 0

    def test_ctrl_w_clears_when_it_consumes_the_whole_draft(self):
        st = _advance(LeadDraftState(), b"solo")
        st = _advance(st, b"\x17")
        assert st.state == EMPTY
        assert st.draft_len == 0

    def test_repeated_ctrl_w_falls_back_to_conservative_single_char_steps(self):
        """After the tracked word is consumed, word_len resets — a second
        Ctrl+W in a row has no boundary to work from, so it must step
        conservatively (1 char) rather than guess a possibly-too-large
        amount that could undercount past the real remaining text."""
        st = _advance(LeadDraftState(), b"ab cd")  # draft_len=5, word_len=2 ("cd")
        st = _advance(st, b"\x17")  # -> "ab " draft_len=3
        assert st.draft_len == 3
        st = _advance(st, b"\x17")  # word_len now 0 -> conservative -1
        assert st.state == NONEMPTY
        assert st.draft_len == 2

    def test_ctrl_w_in_unknown_nonempty_is_noop(self):
        st = _advance(LeadDraftState(), b"\x1b[A")  # Up arrow -> unknown hold
        assert st.state == UNKNOWN_NONEMPTY
        st2 = _advance(st, b"\x17")
        assert st2 == st

    def test_word_len_resets_on_whitespace_mid_run(self):
        st = _advance(LeadDraftState(), b"foo bar baz")
        st = _advance(st, b"\x17")  # deletes "baz" only, not "bar baz"
        assert st.draft_len == 8  # "foo bar "


class TestWordDeleteAltBackspace:
    """Alt+Backspace commonly arrives PTY-side as Esc immediately followed by
    a backspace byte (`\\x1b\\x7f` / `\\x1b\\x08`). Unlike a lone Esc this
    2-byte encoding is unambiguous — Claude CLI never uses it to dismiss a
    menu — so it is safe to special-case as a word-delete."""

    def test_esc_del_deletes_the_word_just_typed(self):
        st = _advance(LeadDraftState(), b"takkub status")
        st = _advance(st, b"\x1b\x7f")
        assert st.state == NONEMPTY
        assert st.draft_len == 7

    def test_esc_ctrl_h_variant_also_deletes_the_word(self):
        st = _advance(LeadDraftState(), b"takkub status")
        st = _advance(st, b"\x1b\x08")
        assert st.state == NONEMPTY
        assert st.draft_len == 7

    def test_esc_del_split_across_two_chunks_still_deletes_the_word(self):
        """The lone-Esc byte alone is ambiguous (issue #111) until the next
        byte arrives, even when that next byte turns out to prove Alt+Backspace
        rather than a bare Esc."""
        st = _advance(LeadDraftState(), b"takkub status")
        st = _advance(st, b"\x1b")
        assert st.state == NONEMPTY and st.draft_len == 13  # still held, unproven
        st = _advance(st, b"\x7f")
        assert st.state == NONEMPTY
        assert st.draft_len == 7

    def test_esc_del_then_backspaces_reaches_empty_without_underflow(self):
        st = _advance(LeadDraftState(), b"takkub status")
        st = _advance(st, b"\x1b\x7f")
        st = _advance(st, b"\x7f" * 10)
        assert st.state == EMPTY
        assert st.draft_len == 0


class TestBareEscFailSafeUnchanged:
    """Issue #114 (A3-secondary): a lone Esc that proves out as neither
    CSI/SS3/mouse/Alt+Backspace is Claude CLI's own overload for *both*
    clearing a draft in progress and dismissing its own slash-menu/dialog
    overlay. This module only observes bytes typed *into* the pane — it has
    no visibility into which the CLI is currently showing — so the two are
    provably indistinguishable from the input byte stream alone (confirmed:
    `advance_draft_state` is fed exclusively from `Orchestrator._on_pane_input`,
    never from render/output bytes). Recommendation to Lead: over-clearing
    (current behavior, pinned below) is the safer of the two unprovable
    guesses — it can only make an injection land a keystroke earlier than a
    human would've typed into the line, where guessing "don't clear" wrong
    would leave state stuck "nonempty" over text that's actually gone,
    silently blocking injection for up to `DRAFT_HOLD_TIMEOUT_S`. Distinguishing
    the two correctly would require render-side menu-open detection, which is
    out of scope for this pure byte-level input-only module. This test pins
    the deliberately-unchanged behavior as a regression guard, not an
    endorsement that it's fully correct."""

    def test_bare_esc_still_clears_a_real_in_progress_draft(self):
        st = _advance(LeadDraftState(), b"draft")
        st = _advance(st, b"\x1bx")
        assert st.state == NONEMPTY  # "draft" cleared, "x" typed anew
        assert st.draft_len == 1


class TestHoldExpiry:
    def test_none_state_never_expired(self):
        assert draft_hold_expired(None, NOW) is False

    def test_empty_state_never_expired(self):
        assert draft_hold_expired(LeadDraftState(), NOW) is False

    def test_fresh_hold_not_expired(self):
        st = LeadDraftState(state=NONEMPTY, draft_len=1, pending_since=NOW)
        assert draft_hold_expired(st, NOW + 5) is False

    def test_hold_expires_after_timeout(self):
        st = LeadDraftState(state=NONEMPTY, draft_len=1, pending_since=NOW)
        assert draft_hold_expired(st, NOW + DRAFT_HOLD_TIMEOUT_S) is True

    def test_hold_expires_for_unknown_nonempty_too(self):
        st = LeadDraftState(state=UNKNOWN_NONEMPTY, pending_since=NOW)
        assert draft_hold_expired(st, NOW + DRAFT_HOLD_TIMEOUT_S + 1) is True
