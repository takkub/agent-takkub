"""Regression test for #569: no_content_pane_recover firing on live,
spinner-only panes.

Root cause: a pane whose only rendered output so far is a spinner/elapsed-
counter/trailing-dots animation (e.g. "● 20s") normalizes to an EMPTY
`_content_fingerprint` — that normalization is deliberate (see
test_output_content_fingerprint.py's `test_fingerprint_ignores_*` cases, so
animation redraw alone never counts as a real content *change*) — but the
pre-#569 `_feed_and_log` only ever stamped `_first_content_ts` when the
fingerprint was non-empty, so a pane stuck showing nothing but a spinner
never got `_first_content_ts` set at all, and the watchdog's
`no_first_content` timeout eventually fired on it even though the pane was
genuinely alive and working, not hung.

Fix: `_first_content_ts` is now also set as soon as any RAW rendered line has
visible (non-blank) content, even if `_content_fingerprint` later strips that
content away entirely — this must stay distinct from
`test_init_escape_sequence_alone_is_not_output` (a pane fed only its
terminal-init handshake renders NO visible glyph at all, and must keep
`first_content_ts() is None`).
"""

from __future__ import annotations

from agent_takkub.pty_session import PtySession, _content_fingerprint


def test_content_fingerprint_filters_spinner_only_messages() -> None:
    """Spinner + elapsed-counter-only lines normalize to an empty fingerprint
    (this is the existing, deliberate behavior #569 must not change)."""
    assert _content_fingerprint(["● 20s"]) == ""
    assert _content_fingerprint(["..."]) == ""
    assert _content_fingerprint([".... 5s"]) == ""


def test_spinner_only_output_still_sets_first_content_ts() -> None:
    """#569: a pane whose only output so far is a spinner+elapsed-counter
    line must still get `first_content_ts` stamped — the pane is alive and
    rendering, even though that specific frame normalizes to "" once
    `_content_fingerprint` strips the animation."""
    s = PtySession(cols=80, rows=24)
    assert s.first_content_ts() is None
    s._feed_and_log("● 20s".encode())
    assert s.first_content_ts() is not None


def test_init_escape_sequence_alone_still_leaves_first_content_ts_none() -> None:
    """Must not regress #248/#247: a pane fed only its terminal-init
    handshake (no visible glyph painted anywhere) is not the same as a
    spinner-only pane and must still read as having no first content yet."""
    s = PtySession(cols=80, rows=24)
    s._feed_and_log(b"\x1b[1t\x1b[c\x1b[?1004h\x1b[?9001h\x1b[?2004h")
    assert s.first_content_ts() is None
