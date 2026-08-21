"""#297: auto-file cockpit issues from runtime signals, not just from crashes.

The bar these tests defend is "would a human have opened an issue about this".
Too low and the tracker fills with normal operation; too high and the feature
is decorative. The thresholds are stated against measured field data, so the
tests below use the same real numbers: the pre-fix #291 window (1,448 stalls /
6h, worst 21s) must fire, and the post-fix window (13 stalls / 1h, worst 3.3s)
must stay silent.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from agent_takkub import auto_issue_signals as sig


def _log(path: Path, records: list[dict]) -> Path:
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    path.write_text(body + "\n" if body else "", encoding="utf-8")
    return path


def _stalls(now: datetime, count: int, duration_ms: int) -> list[dict]:
    return [
        {
            "ts": (now - timedelta(minutes=i % 300)).isoformat(),
            "event": "main_thread_stall",
            "duration_ms": duration_ms,
            "active_panes": 1,
        }
        for i in range(count)
    ]


class TestThresholds:
    def test_pre_fix_291_window_fires(self, tmp_path: Path) -> None:
        """The window that produced #291 by hand must produce it automatically."""
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = _log(tmp_path / "events.log", _stalls(now, 60, 21000))
        hits = sig.scan_for_signals(log, now=now)
        assert [h.rule.key for h in hits] == ["main_thread_stall_severe"]
        assert hits[0].worst == 21000

    def test_post_fix_window_stays_silent(self, tmp_path: Path) -> None:
        """13 short stalls in an hour is a busy machine, not a defect."""
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = _log(tmp_path / "events.log", _stalls(now, 13, 3300))
        assert sig.scan_for_signals(log, now=now) == []

    def test_many_short_stalls_alone_do_not_fire(self, tmp_path: Path) -> None:
        """Count alone must not be enough — the duration bar exists so a noisy
        but harmless machine never files a bug."""
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = _log(tmp_path / "events.log", _stalls(now, 500, 900))
        assert sig.scan_for_signals(log, now=now) == []

    def test_repeated_watchdog_respawn_fires(self, tmp_path: Path) -> None:
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = _log(
            tmp_path / "events.log",
            [
                {
                    "ts": (now - timedelta(minutes=m)).isoformat(),
                    "event": "stuck_pane_recover",
                    "role": "qa",
                }
                for m in (5, 30, 90)
            ],
        )
        hits = sig.scan_for_signals(log, now=now)
        assert [h.rule.key for h in hits] == ["stuck_pane_recover"]
        assert hits[0].count == 3

    def test_two_respawns_are_below_the_bar(self, tmp_path: Path) -> None:
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = _log(
            tmp_path / "events.log",
            [
                {"ts": (now - timedelta(minutes=m)).isoformat(), "event": "stuck_pane_recover"}
                for m in (5, 30)
            ],
        )
        assert sig.scan_for_signals(log, now=now) == []

    def test_events_older_than_the_window_do_not_count(self, tmp_path: Path) -> None:
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = _log(
            tmp_path / "events.log",
            [
                {"ts": (now - timedelta(hours=20)).isoformat(), "event": "stuck_pane_recover"}
                for _ in range(10)
            ],
        )
        assert sig.scan_for_signals(log, now=now) == []

    def test_normal_traffic_is_silent(self, tmp_path: Path) -> None:
        """A busy but healthy day must never file anything."""
        now = datetime(2026, 8, 18, 12, 0, 0)
        records = []
        for i in range(400):
            for name in ("assign", "done", "send", "task_delivery_accepted", "close"):
                records.append(
                    {"ts": (now - timedelta(minutes=i % 300)).isoformat(), "event": name}
                )
        log = _log(tmp_path / "events.log", records)
        assert sig.scan_for_signals(log, now=now) == []

    def test_missing_log_is_silent(self, tmp_path: Path) -> None:
        assert sig.scan_for_signals(tmp_path / "nope.log") == []

    def test_corrupt_lines_do_not_abort(self, tmp_path: Path) -> None:
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = tmp_path / "events.log"
        good = "\n".join(
            json.dumps(
                {"ts": (now - timedelta(minutes=m)).isoformat(), "event": "stuck_pane_recover"}
            )
            for m in (1, 2, 3)
        )
        log.write_text("garbage\n" + good + "\n", encoding="utf-8")
        assert [h.rule.key for h in sig.scan_for_signals(log, now=now)] == ["stuck_pane_recover"]


class TestIssueBody:
    def test_body_carries_counts_not_payloads(self, tmp_path: Path) -> None:
        """The report leaves the user's machine, so it must contain the shape
        of the problem and nothing from the events themselves."""
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = _log(
            tmp_path / "events.log",
            [
                {
                    "ts": (now - timedelta(minutes=m)).isoformat(),
                    "event": "stuck_pane_recover",
                    "role": "qa",
                    "cwd": "C:/Users/secret/project",
                    "note": "do not leak me",
                }
                for m in (1, 2, 3)
            ],
        )
        hit = sig.scan_for_signals(log, now=now)[0]
        title, body = sig.build_issue(hit)
        assert "stuck_pane_recover" in title
        assert "×3" in title
        assert "do not leak me" not in body
        assert "C:/Users/secret" not in body
        assert "เกณฑ์" in body


class TestEnableSwitch:
    def test_default_is_on(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", tmp_path)
        monkeypatch.delenv("TAKKUB_AUTO_ISSUE", raising=False)
        assert sig.auto_issue_enabled() is True

    def test_toggle_persists(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", tmp_path)
        monkeypatch.delenv("TAKKUB_AUTO_ISSUE", raising=False)
        sig.set_auto_issue_enabled(False)
        assert sig.auto_issue_enabled() is False
        sig.set_auto_issue_enabled(True)
        assert sig.auto_issue_enabled() is True

    def test_env_can_force_off(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", tmp_path)
        sig.set_auto_issue_enabled(True)
        monkeypatch.setenv("TAKKUB_AUTO_ISSUE", "0")
        assert sig.auto_issue_enabled() is False

    def test_corrupt_flag_file_defaults_to_on(self, tmp_path, monkeypatch) -> None:
        """A broken settings file must not silently disable reporting."""
        monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", tmp_path)
        monkeypatch.delenv("TAKKUB_AUTO_ISSUE", raising=False)
        (tmp_path / "auto-issue.json").write_text("{not json", encoding="utf-8")
        assert sig.auto_issue_enabled() is True

    def test_disabled_means_no_scan(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", tmp_path)
        monkeypatch.setenv("TAKKUB_AUTO_ISSUE", "off")
        now = datetime(2026, 8, 18, 12, 0, 0)
        _log(tmp_path / "events.log", _stalls(now, 60, 21000))
        assert sig.run_scan_once(tmp_path / "events.log") == []


class TestSharedRateCap:
    def test_signals_share_the_crash_reporter_cap(self, monkeypatch) -> None:
        """Two independently-capped channels would let a bad hour file twice
        the intended maximum, each believing it obeyed the cap."""
        from agent_takkub import auto_issue_capture

        calls: list[str] = []
        monkeypatch.setattr(
            auto_issue_capture, "reserve_signature", lambda s: calls.append(s) or False
        )
        monkeypatch.setattr(auto_issue_capture, "_auto_issue_suppressed", lambda: False)
        monkeypatch.setattr(sig, "auto_issue_enabled", lambda: True)

        hit = sig.SignalHit(sig.RULES[0], 3, 0.0, [])
        sig.file_signal_issue(hit)

        assert calls == [f"signal:{sig.RULES[0].key}"]

    def test_suppressed_process_files_nothing(self, monkeypatch) -> None:
        from agent_takkub import auto_issue_capture

        monkeypatch.setattr(auto_issue_capture, "_auto_issue_suppressed", lambda: True)
        called: list[str] = []
        monkeypatch.setattr(
            auto_issue_capture, "reserve_signature", lambda s: called.append(s) or True
        )
        sig.file_signal_issue(sig.SignalHit(sig.RULES[0], 3, 0.0, []))
        assert called == []


def _delivery_failures(now: datetime, count: int, reason: str | None) -> list[dict]:
    return [
        {
            "ts": (now - timedelta(minutes=i * 5)).isoformat(),
            "event": "task_delivery_failed",
            "role": "backend",
            **({"reason": reason} if reason else {}),
        }
        for i in range(count)
    ]


class TestDeliveryFailureReasons:
    """#331 was filed over four ordinary `takkub done --failed` reports.
    `task_delivery_failed` is emitted for outcomes that are nothing alike —
    the task never reaching the pane (a real defect), a pane being closed with
    a delivery on the books (routine), and a teammate reporting the task
    failed (routine, and the delivery worked). Only the first is a bug."""

    def test_agent_reported_failures_never_file_an_issue(self, tmp_path: Path) -> None:
        now = datetime(2026, 8, 21, 12, 0, 0)
        log = _log(tmp_path / "events.log", _delivery_failures(now, 8, "agent_reported_failed"))
        assert sig.scan_for_signals(log, now=now) == []

    def test_pane_close_failures_never_file_an_issue(self, tmp_path: Path) -> None:
        now = datetime(2026, 8, 21, 12, 0, 0)
        log = _log(tmp_path / "events.log", _delivery_failures(now, 8, "pane_closed"))
        assert sig.scan_for_signals(log, now=now) == []

    def test_real_delivery_failures_still_fire(self, tmp_path: Path) -> None:
        """The rule must not be neutered — a task that genuinely never reached
        the pane is exactly what this signal is for."""
        now = datetime(2026, 8, 21, 12, 0, 0)
        log = _log(tmp_path / "events.log", _delivery_failures(now, 4, "writer_queue_full"))
        hits = sig.scan_for_signals(log, now=now)
        assert [h.rule.key for h in hits] == ["task_delivery_failed"]
        assert hits[0].count == 4

    def test_routine_reasons_do_not_pad_the_count_of_real_ones(self, tmp_path: Path) -> None:
        """Mixed window: two real failures (below the bar of 3) alongside a
        pile of routine ones must stay silent, not be pushed over by them."""
        now = datetime(2026, 8, 21, 12, 0, 0)
        records = _delivery_failures(now, 2, "writer_queue_full") + _delivery_failures(
            now, 20, "agent_reported_failed"
        )
        log = _log(tmp_path / "events.log", records)
        assert sig.scan_for_signals(log, now=now) == []

    def test_an_event_with_no_reason_still_counts(self, tmp_path: Path) -> None:
        """Logs written by an older build carry no `reason`. Treating those as
        routine would silently blind the rule against the very history it was
        calibrated on."""
        now = datetime(2026, 8, 21, 12, 0, 0)
        log = _log(tmp_path / "events.log", _delivery_failures(now, 4, None))
        assert [h.rule.key for h in sig.scan_for_signals(log, now=now)] == ["task_delivery_failed"]

    def test_the_reason_reaches_the_issue_body(self, tmp_path: Path) -> None:
        """An auto-filed issue that only says "×4" gives its reader nothing to
        act on — which is precisely what #331 looked like."""
        now = datetime(2026, 8, 21, 12, 0, 0)
        log = _log(tmp_path / "events.log", _delivery_failures(now, 3, "writer_queue_full"))
        hits = sig.scan_for_signals(log, now=now)
        assert all("writer_queue_full" in s for s in hits[0].samples)
