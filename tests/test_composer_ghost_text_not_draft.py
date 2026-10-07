"""Claude Code paints its prompt suggestion faint (SGR 2) in the composer. pyte
drops SGR 2, so the draft probe read the hint as an unsubmitted draft and the
idle-compact watchdog skipped the Lead forever (prod wash-locker, 2026-10-07)."""

from agent_takkub.pty_session import PtySession, _sgr_dim_after

DIV = "─" * 100


def _session(composer: str) -> PtySession:
    s = PtySession(100, 20)
    frame = f"{DIV}\r\n❯\xa0{composer}\r\n{DIV}\r\n  ⏵⏵ bypass permissions on\r\n"
    s.stream.feed(frame.encode())
    s._output_generation += 1
    return s


def test_faint_prompt_suggestion_is_not_a_draft():
    s = _session("\x1b[2mเอาแบบนี้ เริ่มเลย\x1b[22m")
    assert s.is_at_ready_prompt()
    assert not s.shows_pending_input()


def test_typed_draft_still_counts():
    assert _session("เอาแบบนี้ เริ่มเลย").shows_pending_input()


def test_truecolor_component_2_is_not_faint():
    # 38;2;r;g;b — the "2" selects 24-bit colour, not faint.
    assert _session("\x1b[38;2;2;2;2mtyped text\x1b[39m").shows_pending_input()


def _opencode(composer: str, cursor_col: int) -> PtySession:
    """opencode 1.18.35 shape: `┃` bar at col 4, text from col 7, status row
    below; the terminal cursor is left where opencode parks it (1-based)."""
    s = PtySession(100, 20)
    frame = (
        "    ┃\r\n"
        f"    ┃  {composer}\r\n"
        "    ┃\r\n"
        "    ┃  Build auto · Big Pickle OpenCode Zen\r\n"
        "    ╹" + "▀" * 60 + f"\r\n\x1b[2;{cursor_col}H"
    )
    s.stream.feed(frame.encode())
    s._output_generation += 1
    return s


def test_opencode_grey_placeholder_with_cursor_at_start_is_not_a_draft():
    s = _opencode('\x1b[38;2;128;128;128mAsk anything… "Fix a TODO in the codebase"\x1b[39m', 8)
    assert not s.shows_pending_input()


def test_opencode_typed_draft_counts():
    text = "this is a real typed draft"
    assert _opencode(text, 8 + len(text)).shows_pending_input()


def test_sgr_dim_state_machine():
    assert _sgr_dim_after(False, (2,))
    assert not _sgr_dim_after(True, (22,))
    assert not _sgr_dim_after(True, ())
    assert not _sgr_dim_after(True, (0,))
    assert _sgr_dim_after(True, (38, 5, 2))
    assert not _sgr_dim_after(False, (48, 2, 2, 2, 2))
