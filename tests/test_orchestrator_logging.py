"""Tests for the audit-log size cap and transcript auto-prune that bound disk
growth (root cause of the cockpit freeze / disk bloat — see
docs/cockpit-freeze-rca-2026-05-29.md).

RUNTIME_DIR / EVENTS_LOG are redirected to a tmp dir by the autouse fixture in
conftest.py, so these tests never touch the real runtime/events.log.
"""

from __future__ import annotations

import os
import pathlib
import time

from agent_takkub import orchestrator as orch


class TestEventsLogCap:
    def test_log_rotates_when_over_cap(self, monkeypatch) -> None:
        # Shrink the cap so a handful of events trips rotation.
        monkeypatch.setattr(orch, "_EVENTS_LOG_MAX_BYTES", 500)

        for i in range(50):
            orch._log_event("test_event", i=i, note="x" * 40)

        events = orch.EVENTS_LOG
        old = events.parent / (events.name + ".old")
        assert events.exists(), "live events.log should still exist after rotation"
        assert old.exists(), "rotation should have produced events.log.old"
        # The cap is a soft threshold (rotation happens on the *next* write once
        # the file is over cap), so the live file may briefly exceed cap by one
        # event — but it must stay bounded, far below the un-rotated total.
        assert events.stat().st_size < 2 * 500
        # Logging kept working — the live file has fresh lines.
        assert events.read_text(encoding="utf-8").strip() != ""

    def test_no_rotation_under_cap(self, monkeypatch) -> None:
        monkeypatch.setattr(orch, "_EVENTS_LOG_MAX_BYTES", 2 * 1024 * 1024)
        orch._log_event("small", note="hi")
        events = orch.EVENTS_LOG
        old = events.parent / (events.name + ".old")
        assert events.exists()
        assert not old.exists()

    def test_log_event_never_raises_on_bad_runtime(self, monkeypatch) -> None:
        # Point EVENTS_LOG at an unwritable path; _log_event must swallow it.
        monkeypatch.setattr(orch, "EVENTS_LOG", pathlib.Path("/this/does/not/exist/x.log"))
        orch._log_event("boom")  # must not raise


class TestPruneOldTranscripts:
    def _make(self, root: pathlib.Path, name: str, age_days: float) -> pathlib.Path:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("data", encoding="utf-8")
        when = time.time() - age_days * 86_400
        os.utime(p, (when, when))
        return p

    def test_prunes_old_transcripts_keeps_recent_and_md(self) -> None:
        sessions = orch.RUNTIME_DIR / "sessions"
        old_t = self._make(sessions, "2026-05-10/proj/lead-1.transcript.log", 9)
        new_t = self._make(sessions, "2026-05-29/proj/lead-2.transcript.log", 1)
        old_md = self._make(sessions, "2026-05-10/proj/note.md", 9)

        removed = orch.prune_old_transcripts(max_age_days=7)

        assert removed == 1
        assert not old_t.exists(), "9-day-old transcript should be pruned"
        assert new_t.exists(), "1-day-old transcript should be kept"
        assert old_md.exists(), ".md notes are never pruned"

    def test_no_sessions_dir_is_noop(self, monkeypatch, tmp_path) -> None:
        # Point RUNTIME_DIR somewhere with no sessions/ — must return 0, no raise.
        monkeypatch.setattr(orch, "RUNTIME_DIR", tmp_path / "empty")
        assert orch.prune_old_transcripts() == 0
