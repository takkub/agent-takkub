"""Tests for `takkub ma` — the maintenance sweep.

The command's whole value is that its report can be trusted without re-checking
by hand, so these pin the parts where a wrong answer would be actively
misleading: an empty result must read as "clean" and not as "nothing checked",
a missing tool must be reported rather than swallowed, and the plan must never
tell the operator to go look at findings that are not there.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from agent_takkub import maintenance


def _write_events(path: Path, records: list[dict]) -> Path:
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    path.write_text(body + "\n" if body else "", encoding="utf-8")
    return path


class TestScanEvents:
    def test_missing_log_is_skip_not_ok(self, tmp_path: Path) -> None:
        """'no log file' and 'log file says everything is fine' must never
        render the same — the first means the check did not run."""
        check = maintenance.scan_events(tmp_path / "nope.log")
        assert check.status == "skip"

    def test_counts_severe_and_warn_separately(self, tmp_path: Path) -> None:
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = _write_events(
            tmp_path / "events.log",
            [
                {"ts": (now - timedelta(minutes=5)).isoformat(), "event": "stuck_pane_recover"},
                {"ts": (now - timedelta(minutes=4)).isoformat(), "event": "verify_failed"},
                {
                    "ts": (now - timedelta(minutes=3)).isoformat(),
                    "event": "main_thread_stall",
                    "duration_ms": 900,
                },
                {"ts": (now - timedelta(minutes=2)).isoformat(), "event": "assign"},
            ],
        )
        check = maintenance.scan_events(log, since_hours=1, now=now)
        assert check.status == "attention"
        assert check.data["severe"] == {"stuck_pane_recover": 1, "verify_failed": 1}
        assert check.data["warn"] == {"main_thread_stall": 1}

    def test_events_outside_the_window_are_ignored(self, tmp_path: Path) -> None:
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = _write_events(
            tmp_path / "events.log",
            [{"ts": (now - timedelta(hours=48)).isoformat(), "event": "stuck_pane_recover"}],
        )
        check = maintenance.scan_events(log, since_hours=24, now=now)
        assert check.status == "ok"
        assert "ไม่มี event" in check.summary

    def test_healthy_window_is_ok(self, tmp_path: Path) -> None:
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = _write_events(
            tmp_path / "events.log",
            [
                {"ts": (now - timedelta(minutes=5)).isoformat(), "event": "assign"},
                {"ts": (now - timedelta(minutes=4)).isoformat(), "event": "done"},
            ],
        )
        check = maintenance.scan_events(log, since_hours=1, now=now)
        assert check.status == "ok"

    def test_long_stalls_are_called_out_individually(self, tmp_path: Path) -> None:
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = _write_events(
            tmp_path / "events.log",
            [
                {
                    "ts": (now - timedelta(minutes=5)).isoformat(),
                    "event": "main_thread_stall",
                    "duration_ms": 5200,
                    "active_panes": 2,
                },
                {
                    "ts": (now - timedelta(minutes=4)).isoformat(),
                    "event": "main_thread_stall",
                    "duration_ms": 300,
                },
            ],
        )
        check = maintenance.scan_events(log, since_hours=1, now=now)
        assert check.data["worst_stall_ms"] == 5200
        assert any("5.2s" in d for d in check.details)
        # The 0.3s one is noise on a busy box — counted, never named.
        assert not any("0.3s" in d for d in check.details)

    def test_corrupt_lines_do_not_abort_the_scan(self, tmp_path: Path) -> None:
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = tmp_path / "events.log"
        good = json.dumps(
            {"ts": (now - timedelta(minutes=1)).isoformat(), "event": "verify_failed"}
        )
        log.write_text("not json at all\n" + good + "\n", encoding="utf-8")
        check = maintenance.scan_events(log, since_hours=1, now=now)
        assert check.data["severe"] == {"verify_failed": 1}


class TestBuildActions:
    def test_plan_is_numbered_without_gaps(self) -> None:
        """A plan that jumps 1 -> 4 reads like steps were lost."""
        checks = [
            maintenance.Check("issues", "i", "ok", ""),
            maintenance.Check("prs", "p", "ok", ""),
            maintenance.Check("logs", "l", "ok", ""),
            maintenance.Check("repo", "r", "ok", ""),
        ]
        actions = maintenance.build_actions(checks)
        assert [a.split(".", 1)[0] for a in actions] == [str(i) for i in range(1, len(actions) + 1)]

    def test_does_not_point_at_red_findings_when_there_are_none(self) -> None:
        checks = [
            maintenance.Check(
                "logs",
                "l",
                "attention",
                "",
                data={"severe": {}, "warn": {"main_thread_stall": 3}},
            )
        ]
        actions = maintenance.build_actions(checks)
        assert any("\U0001f7e1" in a for a in actions)
        assert not any("\U0001f534" in a for a in actions)

    def test_points_at_red_findings_when_they_exist(self) -> None:
        checks = [
            maintenance.Check(
                "logs", "l", "attention", "", data={"severe": {"verify_failed": 1}, "warn": {}}
            )
        ]
        assert any("\U0001f534" in a for a in maintenance.build_actions(checks))

    def test_publish_is_always_last_and_gated_on_ci(self) -> None:
        actions = maintenance.build_actions([maintenance.Check("repo", "r", "ok", "")])
        assert "publish" in actions[-1]
        assert "CI" in actions[-1]


class TestRunMaintenance:
    def test_no_net_skips_the_network_checks_but_still_reads_the_log(self, tmp_path: Path) -> None:
        log = _write_events(tmp_path / "events.log", [])
        report = maintenance.run_maintenance(
            tmp_path, include_network=False, log_path=log, since_hours=1
        )
        by_key = {c.key: c for c in report.checks}
        assert by_key["issues"].status == "skip"
        assert by_key["prs"].status == "skip"
        assert by_key["repo"].status == "skip"
        assert by_key["logs"].status != "skip"

    def test_report_serialises_for_json_output(self, tmp_path: Path) -> None:
        log = _write_events(tmp_path / "events.log", [])
        report = maintenance.run_maintenance(
            tmp_path, include_network=False, log_path=log, since_hours=1
        )
        payload = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))
        assert payload["since_hours"] == 1
        assert {c["key"] for c in payload["checks"]} == {"issues", "prs", "logs", "repo"}

    def test_render_lists_every_check(self, tmp_path: Path) -> None:
        log = _write_events(tmp_path / "events.log", [])
        report = maintenance.run_maintenance(
            tmp_path, include_network=False, log_path=log, since_hours=1
        )
        text = maintenance.render_report(report)
        for check in report.checks:
            assert check.title in text


class TestRunHelper:
    def test_missing_executable_is_reported_not_raised(self) -> None:
        ok, msg = maintenance._run(["takkub-does-not-exist-xyz"])
        assert ok is False
        assert "ไม่พบคำสั่ง" in msg
