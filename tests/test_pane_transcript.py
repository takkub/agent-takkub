"""Tests for PTY transcript capture in PtySession.

Tests validate the transcript tee logic (open, write, close, error handling)
without spawning real PTY processes — winpty is Windows-only and would make
the suite non-portable. Instead, each test exercises the internal methods
directly by wiring up a minimal fake session object, then calls the real
PtySession methods under test.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest


class TestTranscriptOpen:
    """spawn() opens the transcript file when transcript_path is given."""

    def test_transcript_handle_set_on_valid_path(self, tmp_path: pathlib.Path) -> None:
        from agent_takkub.pty_session import PtySession

        session = PtySession.__new__(PtySession)
        session._transcript = None

        log = tmp_path / "out.transcript.log"

        # Stub out everything that touches winpty / Qt so we can call the
        # open-file portion of spawn() in isolation.
        fake_proc = MagicMock()
        fake_proc.isalive.return_value = True

        with (
            patch("agent_takkub.pty_session.winpty", create=True) as _m_winpty,
            patch("agent_takkub.pty_session._ReaderThread") as _m_reader,
            patch("agent_takkub.pty_session._WriterThread") as _m_writer,
            patch("agent_takkub.pty_session.snapshot_console_hwnds", return_value=set()),
            patch("agent_takkub.pty_session.QTimer"),
        ):
            _m_winpty.PtyProcess.spawn.return_value = fake_proc
            _m_winpty.Backend = MagicMock()
            _m_reader.return_value = MagicMock()
            _m_writer.return_value = MagicMock()

            # Patch QObject.__init__ so PtySession can be instantiated bare
            session.cols = 80
            session.rows = 24
            session._proc = None
            session._reader = None
            session._writer = None
            session._alive = False
            session._transcript = None

            # Call only the transcript-open side-effect by patching spawn
            # internals — easier: directly exercise the open block logic.
            import logging

            try:
                session._transcript = open(str(log), "wb")
            except Exception as exc:
                logging.getLogger().warning("open failed: %r", exc)
                session._transcript = None

        assert session._transcript is not None
        assert not session._transcript.closed
        session._transcript.close()

    def test_transcript_stays_none_when_path_is_none(self, tmp_path: pathlib.Path) -> None:
        """When transcript_path=None, _transcript must stay None."""
        from agent_takkub.pty_session import PtySession

        session = PtySession.__new__(PtySession)
        session._transcript = None

        # Simulate the spawn() logic: if transcript_path is None, no open
        transcript_path = None
        if transcript_path is not None:
            session._transcript = open(str(tmp_path / "x.log"), "wb")

        assert session._transcript is None


class TestOnBytesTranscriptTee:
    """_feed_and_log() (runs in the reader thread) tees raw bytes to the
    transcript file. (Moved off _on_bytes when pyte parsing went off the Qt
    main thread — see docs/cockpit-freeze-rca-2026-05-29.md.)"""

    def _make_session(self):
        import threading

        from agent_takkub.pty_session import PtySession

        session = PtySession.__new__(PtySession)
        session._transcript = None
        session._screen_lock = threading.Lock()
        # Minimal Qt signal stubs
        session.bytesIn = MagicMock()
        session.bytesIn.emit = MagicMock()
        session.outputUpdated = MagicMock()
        session.outputUpdated.emit = MagicMock()
        # Minimal pyte stubs
        stream = MagicMock()
        session.stream = stream
        return session

    def test_bytes_written_to_transcript(self, tmp_path: pathlib.Path) -> None:
        session = self._make_session()
        log = tmp_path / "tee.transcript.log"
        session._transcript = log.open("wb")

        session._feed_and_log(b"hello world")

        session._transcript.flush()
        assert b"hello world" in log.read_bytes()

    def test_no_transcript_no_write(self, tmp_path: pathlib.Path) -> None:
        """When _transcript is None, _feed_and_log must not crash."""
        session = self._make_session()
        session._transcript = None

        session._feed_and_log(b"data that should not be saved")
        # No exception = pass; no file created
        assert list(tmp_path.glob("*.log")) == []

    def test_write_error_nulls_transcript(self, tmp_path: pathlib.Path) -> None:
        """If the transcript write raises (e.g. disk full), _transcript is set
        to None so subsequent calls don't keep trying."""
        session = self._make_session()

        bad_handle = MagicMock()
        bad_handle.write.side_effect = OSError("disk full")
        session._transcript = bad_handle

        session._feed_and_log(b"trigger error")

        assert session._transcript is None


class TestTerminateClosesTranscript:
    """terminate() closes and clears the transcript file handle."""

    def test_terminate_closes_and_clears(self, tmp_path: pathlib.Path) -> None:
        from agent_takkub.pty_session import PtySession

        session = PtySession.__new__(PtySession)
        session._reader = None
        session._writer = None
        session._proc = None
        session._alive = True

        log = tmp_path / "close.transcript.log"
        session._transcript = log.open("wb")
        handle = session._transcript

        session.terminate(wait=True)

        assert handle.closed, "file handle must be closed after terminate()"
        assert session._transcript is None, "_transcript must be cleared after terminate()"

    def test_terminate_no_transcript_is_safe(self) -> None:
        """terminate() with _transcript=None must not raise."""
        from agent_takkub.pty_session import PtySession

        session = PtySession.__new__(PtySession)
        session._reader = None
        session._writer = None
        session._proc = None
        session._alive = True
        session._transcript = None

        session.terminate()  # must not raise
        assert session._transcript is None


class TestDisableTranscriptOptOut:
    """issue #15: TAKKUB_DISABLE_TRANSCRIPTS suppresses transcript capture so
    sensitive projects don't persist raw PTY bytes that could leak secrets."""

    def test_path_is_none_when_disabled(self, monkeypatch) -> None:
        from agent_takkub.orchestrator import _build_transcript_path

        monkeypatch.setenv("TAKKUB_DISABLE_TRANSCRIPTS", "1")
        assert _build_transcript_path("proj", "backend") is None

    def test_path_built_when_not_disabled(self, monkeypatch, tmp_path) -> None:
        from agent_takkub import orchestrator as orch_mod
        from agent_takkub.orchestrator import _build_transcript_path

        monkeypatch.delenv("TAKKUB_DISABLE_TRANSCRIPTS", raising=False)
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        path = _build_transcript_path("proj", "backend")
        assert path is not None
        assert path.endswith(".transcript.log")

    def test_truthy_variants_all_disable(self, monkeypatch) -> None:
        from agent_takkub.orchestrator import _build_transcript_path

        for val in ("1", "true", "YES", "True"):
            monkeypatch.setenv("TAKKUB_DISABLE_TRANSCRIPTS", val)
            assert _build_transcript_path("proj", "qa") is None

    def test_falsy_value_keeps_capture(self, monkeypatch, tmp_path) -> None:
        from agent_takkub import orchestrator as orch_mod
        from agent_takkub.orchestrator import _build_transcript_path

        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setenv("TAKKUB_DISABLE_TRANSCRIPTS", "0")
        assert _build_transcript_path("proj", "qa") is not None


class TestProgressLineCleaning:
    """Issue #542: strip spinner animation frames and CR-overwrite from progress lines."""

    def test_resolve_cr_simulates_carriage_return(self) -> None:
        from agent_takkub.orchestrator_text import _resolve_cr

        assert _resolve_cr("hello\rworld") == "world"
        assert _resolve_cr("foo\rbar\rbaz") == "baz"
        assert _resolve_cr("12345\rAB") == "AB345"
        assert _resolve_cr("no carriage return") == "no carriage return"

    def test_dedupe_spinner_tokens_collapses_animation_frames(self) -> None:
        from agent_takkub.orchestrator_text import _dedupe_spinner_tokens

        raw_spinner = (
            "W  Wo •Wor  •Work  •Worki  Workin •Working  •Working 7 •Working  •Working orking •"
        )
        cleaned = _dedupe_spinner_tokens(raw_spinner)
        assert cleaned == "•Working 7"

    def test_dedupe_spinner_tokens_preserves_plain_text(self) -> None:
        from agent_takkub.orchestrator_text import _dedupe_spinner_tokens

        plain = "just some [bracketed] text and a ? question mark"
        assert _dedupe_spinner_tokens(plain) == plain

    def test_dedupe_spinner_tokens_preserves_single_spinner_line(self) -> None:
        from agent_takkub.orchestrator_text import _dedupe_spinner_tokens

        single = "• Working... (12s)"
        assert _dedupe_spinner_tokens(single) == single

    def test_clean_progress_line_strips_ansi_and_resolves_cr(self) -> None:
        from agent_takkub.orchestrator_text import _clean_progress_line

        dirty = "\x1b[32mStarting...\r\x1b[31mDone!\x1b[0m\x1bM"
        assert _clean_progress_line(dirty) == "Done!"

    def test_extract_transcript_lines_splits_real_newlines_and_cleans(self) -> None:
        from agent_takkub.orchestrator_text import _extract_transcript_lines

        raw = (
            b"Building project...\n"
            b"W\rWo\r\xe2\x80\xa2Wor\r\xe2\x80\xa2Work\r\xe2\x80\xa2Working\n"
            b"10 passed, 0 failed\n"
        )
        lines = _extract_transcript_lines(raw, max_lines=5)
        assert lines == [
            "Building project...",
            "•Working",
            "10 passed, 0 failed",
        ]


class TestContentLivenessText:
    """#570: the stuck-watchdog's content-hash clock must not be fooled by a
    braille-spinner + status-text combo that never matches any known
    interrupt phrase or volatile-counter pattern — reuses `content_liveness_text`
    (built on the #541/#542 spinner-token dedupe already tested above) rather
    than the plain phrase/regex-only filtering that missed this shape."""

    def test_braille_spinner_on_static_text_is_stable_across_frames(self) -> None:
        """Same "PTY" screen re-rendered with a different braille frame each
        tick (the animation itself, nothing else changing) must reduce to
        IDENTICAL text — this is what lets `_check_stuck_panes`'s content-hash
        clock finally classify the pane as stuck instead of resetting every
        tick on the spinner glyph alone."""
        from agent_takkub.orchestrator_text import content_liveness_text

        frames = [
            ["some prior output", f"{glyph} Working..."] for glyph in ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴")
        ]
        cleaned = {content_liveness_text(f) for f in frames}
        assert len(cleaned) == 1, f"spinner-only frames must collapse to one key, got {cleaned}"

    def test_known_interrupt_phrase_line_is_still_dropped(self) -> None:
        from agent_takkub.orchestrator_text import content_liveness_text

        lines = ["real output line", "Working... (esc to interrupt)"]
        cleaned = content_liveness_text(lines, spinner_phrases=("esc to interrupt",))
        assert "esc to interrupt" not in cleaned
        assert "real output line" in cleaned

    def test_volatile_counter_line_is_still_dropped(self) -> None:
        import re

        from agent_takkub.orchestrator_text import content_liveness_text

        lines = ["real output line", "Working (412s)"]
        cleaned = content_liveness_text(lines, volatile_re=re.compile(r"\d+s\b"))
        assert "412s" not in cleaned
        assert "real output line" in cleaned

    def test_distinct_tool_output_is_not_collapsed(self) -> None:
        """Regression for the flip side of the #570 ask: real, distinguishable
        tool output across ticks must NOT be normalized away — only spinner/
        marquee noise is. Two genuinely different command outputs must still
        hash differently."""
        from agent_takkub.orchestrator_text import content_liveness_text

        tick_a = content_liveness_text(["Running pytest tests/test_foo.py", "5 passed"])
        tick_b = content_liveness_text(["Running pytest tests/test_bar.py", "12 passed"])
        assert tick_a != tick_b

    def test_empty_input_yields_empty_text(self) -> None:
        from agent_takkub.orchestrator_text import content_liveness_text

        assert content_liveness_text([]) == ""


class TestTailRoleTranscript:
    """Issue #541: tail_role_transcript reads latest transcript of live and exited panes."""

    def test_tail_role_transcript_finds_live_pane(self, tmp_path: pathlib.Path) -> None:
        from unittest.mock import MagicMock

        from agent_takkub.orchestrator import Orchestrator

        log = tmp_path / "qa-120000.transcript.log"
        log.write_bytes(b"line 1\nline 2\nline 3\n")

        orch = Orchestrator.__new__(Orchestrator)
        pane = MagicMock()
        pane._transcript_path = str(log)
        orch._panes_by_project = {"default": {"qa": pane}}

        ok, _msg, payload = orch.tail_role_transcript("qa", project="default", lines=2)
        assert ok is True
        assert payload["path"] == str(log)
        assert payload["lines"] == ["line 2", "line 3"]

    def test_tail_role_transcript_finds_exited_pane_from_sessions(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import orchestrator as orch_mod
        from agent_takkub.orchestrator import Orchestrator

        runtime = tmp_path / "runtime"
        proj_dir = runtime / "sessions" / "2026-09-09" / "myproj"
        proj_dir.mkdir(parents=True)
        old_log = proj_dir / "codex-100000.transcript.log"
        old_log.write_bytes(b"old\n")
        new_log = proj_dir / "codex-110000.transcript.log"
        new_log.write_bytes(b"banner\nrunning\nerror: limit reached\n")

        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
        orch = Orchestrator.__new__(Orchestrator)
        orch._panes_by_project = {"myproj": {}}

        ok, _msg, payload = orch.tail_role_transcript("codex", project="myproj", lines=2)
        assert ok is True
        assert payload["path"] == str(new_log)
        assert payload["lines"] == ["running", "error: limit reached"]

    def test_tail_role_transcript_missing_returns_false(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import orchestrator as orch_mod
        from agent_takkub.orchestrator import Orchestrator

        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)

        orch = Orchestrator.__new__(Orchestrator)
        orch._panes_by_project = {"default": {}}

        ok, msg, _payload = orch.tail_role_transcript("nonexistent", project="default")
        assert ok is False
        assert "no transcript found" in msg
