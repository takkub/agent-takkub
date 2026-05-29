"""Tests for logs_panel.read_log_tail — the cheap tail reader that replaced
reading the whole events.log every second (the multi-MB read on the Qt main
thread that wedged the cockpit; see docs/cockpit-freeze-rca-2026-05-29.md)."""

from __future__ import annotations

import pathlib

from agent_takkub.logs_panel import _TAIL_BYTES, read_log_tail


def _write_big_log(path: pathlib.Path, n_lines: int) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("OLD_HEAD_LINE_MUST_NOT_RENDER\n")
        for i in range(n_lines):
            f.write(f'{{"ts":"t","event":"e","note":"line {i}"}}\n')


class TestReadLogTail:
    def test_reads_only_the_tail_of_a_large_file(self, tmp_path: pathlib.Path) -> None:
        log = tmp_path / "events.log"
        _write_big_log(log, 200_000)  # ~6 MB, far bigger than the tail window
        size = log.stat().st_size
        assert size > _TAIL_BYTES  # precondition: file exceeds the window

        text = read_log_tail(log)

        # The head sentinel sits >6 MB before EOF — it must never be returned.
        assert "OLD_HEAD_LINE_MUST_NOT_RENDER" not in text
        # We read at most the tail window (plus the dropped partial line).
        assert len(text.encode("utf-8")) <= _TAIL_BYTES
        # The most recent line is present and intact.
        assert '"note":"line 199999"' in text
        # And the partial first line was dropped cleanly — every retained line
        # parses as a whole record (starts with '{').
        first = text.splitlines()[0]
        assert first.startswith("{")

    def test_small_file_returned_whole(self, tmp_path: pathlib.Path) -> None:
        log = tmp_path / "events.log"
        log.write_bytes(b"line one\nline two\n")
        text = read_log_tail(log)
        assert text == "line one\nline two\n"

    def test_missing_file_returns_empty(self, tmp_path: pathlib.Path) -> None:
        assert read_log_tail(tmp_path / "nope.log") == ""

    def test_custom_window(self, tmp_path: pathlib.Path) -> None:
        log = tmp_path / "events.log"
        _write_big_log(log, 5_000)
        text = read_log_tail(log, tail_bytes=1024)
        assert len(text.encode("utf-8")) <= 1024
        assert "OLD_HEAD_LINE_MUST_NOT_RENDER" not in text
