"""#248/#247: `_last_output_ts` must stamp only on a real screen-content
change, not on every raw PTY byte chunk.

Before this fix `_feed_and_log` stamped `_last_output_ts` unconditionally,
so two very different situations both read as "output just happened":
  - a terminal-init escape sequence (cursor-position report, mouse-mode
    enables) that renders no visible glyph at all — a codex pane whose CLI
    hadn't printed anything yet looked "not-quiet" forever.
  - an animated spinner glyph redrawing next to otherwise-static text (an
    agy pane stuck on "Signing in...") — looked like continuous progress
    forever.

`seconds_since_output()` feeds directly into lead_inbox.py's delivery
busy-wait (#130/#144) and the stale-marker watchdog (#20), so either false
signal meant a genuinely wedged pane was treated as "still working" for the
full BUSY_WAIT_CEILING_SEC / STUCK_THRESHOLD_S window.
"""

from __future__ import annotations

from agent_takkub.pty_session import PtySession, _content_fingerprint

# ── _content_fingerprint (pure function) ────────────────────────────────────


def test_fingerprint_blank_screen_is_empty() -> None:
    assert _content_fingerprint(["", "   ", ""]) == ""


def test_fingerprint_ignores_which_braille_spinner_frame() -> None:
    a = _content_fingerprint(["⠋ Signing in..."])
    b = _content_fingerprint(["⠙ Signing in..."])
    c = _content_fingerprint(["⣾ Signing in..."])
    assert a == b == c
    assert a != ""


def test_fingerprint_ignores_trailing_ellipsis_growth() -> None:
    assert (
        _content_fingerprint(["Signing in."])
        == _content_fingerprint(["Signing in.."])
        == _content_fingerprint(["Signing in..."])
    )


def test_fingerprint_ignores_isolated_ascii_spinner_glyph() -> None:
    assert _content_fingerprint(["| Loading"]) == _content_fingerprint(["/ Loading"])
    assert _content_fingerprint(["/ Loading"]) == _content_fingerprint(["\\ Loading"])


def test_fingerprint_does_not_eat_real_hyphens_and_slashes() -> None:
    # A hyphen/slash that's part of a word/flag/path is real content, not a
    # spinner glyph, and a genuine change here must still register as one.
    assert _content_fingerprint(["--force"]) != _content_fingerprint(["--yolo"])
    assert _content_fingerprint(["path/to/a"]) != _content_fingerprint(["path/to/b"])
    assert _content_fingerprint(["a-b"]) != _content_fingerprint(["a-c"])


def test_fingerprint_real_text_change_differs() -> None:
    assert _content_fingerprint(["Signing in..."]) != _content_fingerprint(["Connected."])


# ── PtySession integration: _last_output_ts / _last_byte_ts / first_content_ts ──


def test_init_escape_sequence_alone_is_not_output() -> None:
    """A freshly-spawned pane's terminal-init handshake (cursor-position
    report, mouse-mode enables, bracketed-paste) renders nothing — must not
    count as content output."""
    s = PtySession(cols=80, rows=24)
    s._feed_and_log(b"\x1b[1t\x1b[c\x1b[?1004h\x1b[?9001h\x1b[?2004h")
    assert s.seconds_since_output() == float("inf")
    assert s.first_content_ts() is None
    # But the PTY is demonstrably alive — the raw-byte signal must still fire.
    assert s.seconds_since_byte() < 5.0


def test_real_content_sets_first_content_ts_and_output_ts() -> None:
    s = PtySession(cols=80, rows=24)
    assert s.first_content_ts() is None
    s._feed_and_log(b"Signing in...")
    assert s.seconds_since_output() < 5.0
    assert s.first_content_ts() is not None


def test_spinner_redraw_does_not_advance_output_ts() -> None:
    s = PtySession(cols=80, rows=24)
    s._feed_and_log("\r⠋ Signing in...".encode())
    first_ts = s.last_output_monotonic()
    first_content_ts = s.first_content_ts()
    assert first_ts > 0.0

    # Same status text, only the spinner glyph rotates (redrawn via \r, the
    # same way a real CLI repaints a status line in place).
    for glyph in "⠙⠹⠸⠼⠴⠦⠧⠇":
        s._feed_and_log(f"\r{glyph} Signing in...".encode())
        assert s.last_output_monotonic() == first_ts, (
            "a spinner-only redraw must not advance _last_output_ts"
        )
        assert s.first_content_ts() == first_content_ts

    # The raw byte clock, unlike the content clock, does advance every chunk.
    assert s.seconds_since_byte() <= s.seconds_since_output()


def test_real_content_change_after_spinner_advances_output_ts() -> None:
    s = PtySession(cols=80, rows=24)
    s._feed_and_log("\r⠋ Signing in...".encode())
    first_ts = s.last_output_monotonic()

    s._feed_and_log("\r⠙ Signing in...".encode())
    assert s.last_output_monotonic() == first_ts  # still just the spinner

    s._feed_and_log(b"\x1b[2K\rConnected.")  # clear line + real new content
    assert s.last_output_monotonic() >= first_ts
    assert "Connected." in "\n".join(s.display_lines())


def test_ready_prompt_classification_unaffected_by_fingerprint_change() -> None:
    """Regression guard: the fingerprint/_last_output_ts change must not
    alter is_at_ready_prompt()/is_at_ready_prompt_cached() at all — they're
    computed from the same `lines` but via the pre-existing, untouched
    _classify_ready(_ready_region(...)) path."""
    s = PtySession(cols=80, rows=24)
    s._feed_and_log(b"bypass permissions")
    assert s.is_at_ready_prompt_cached() is True
    assert s.is_at_ready_prompt() is True

    s._feed_and_log(b"\r\n(esc to interrupt) working...")
    assert s.is_at_ready_prompt_cached() is False
    assert s.is_at_ready_prompt() is False
