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

    def test_most_common_stall_frame_is_called_out(self, tmp_path: Path) -> None:
        """#452: when ≥2s stall records carry a `stack`, the most-frequent
        top frame across them gets its own one-line callout so `takkub ma`
        stops requiring a manual boot.log dig for the common case."""
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = _write_events(
            tmp_path / "events.log",
            [
                {
                    "ts": (now - timedelta(minutes=5)).isoformat(),
                    "event": "main_thread_stall",
                    "duration_ms": 2400,
                    "stack": ["orchestrator.py:_check_x:100", "app.py:main:1018"],
                },
                {
                    "ts": (now - timedelta(minutes=4)).isoformat(),
                    "event": "main_thread_stall",
                    "duration_ms": 3000,
                    "stack": ["orchestrator.py:_check_x:100", "app.py:main:1018"],
                },
                {
                    # below the ≥2s callout threshold — must not count toward the tally
                    "ts": (now - timedelta(minutes=3)).isoformat(),
                    "event": "main_thread_stall",
                    "duration_ms": 300,
                    "stack": ["somewhere_else.py:other:1"],
                },
            ],
        )
        check = maintenance.scan_events(log, since_hours=1, now=now)
        assert any("orchestrator.py:_check_x:100" in d and "×2/2" in d for d in check.details)

    def test_no_stack_data_omits_the_frame_callout(self, tmp_path: Path) -> None:
        now = datetime(2026, 8, 18, 12, 0, 0)
        log = _write_events(
            tmp_path / "events.log",
            [
                {
                    "ts": (now - timedelta(minutes=5)).isoformat(),
                    "event": "main_thread_stall",
                    "duration_ms": 2400,
                }
            ],
        )
        check = maintenance.scan_events(log, since_hours=1, now=now)
        assert not any("ส่วนใหญ่ค้างที่" in d for d in check.details)

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
        assert {c["key"] for c in payload["checks"]} == {
            "issues",
            "prs",
            "code_scanning",
            "logs",
            "local_issues",
            "repo",
        }

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


class TestLocalIssueBacklog:
    """#297: a cockpit bug that fell back to the local store is invisible —
    the existing warning goes to stderr, which a GUI-hosted cockpit never
    shows. `takkub ma` is where the operator would actually notice."""

    def test_no_store_is_ok(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("agent_takkub.config.DATA_HOME", tmp_path)
        assert maintenance.check_local_issue_backlog().status == "ok"

    def test_open_local_issues_need_attention(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("agent_takkub.config.DATA_HOME", tmp_path)
        (tmp_path / ".takkub_issues.json").write_text(
            json.dumps(
                [
                    {"number": 1, "title": "stuck pane", "status": "open"},
                    {"number": 2, "title": "already handled", "status": "closed"},
                ]
            ),
            encoding="utf-8",
        )
        check = maintenance.check_local_issue_backlog()
        assert check.status == "attention"
        assert check.data["count"] == 1
        assert any("stuck pane" in d for d in check.details)

    def test_closed_only_store_is_ok(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("agent_takkub.config.DATA_HOME", tmp_path)
        (tmp_path / ".takkub_issues.json").write_text(
            json.dumps([{"number": 1, "title": "done", "status": "closed"}]), encoding="utf-8"
        )
        assert maintenance.check_local_issue_backlog().status == "ok"

    def test_corrupt_store_is_reported_not_swallowed(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("agent_takkub.config.DATA_HOME", tmp_path)
        (tmp_path / ".takkub_issues.json").write_text("{not json", encoding="utf-8")
        assert maintenance.check_local_issue_backlog().status == "error"

    def test_plan_tells_you_to_send_the_backlog_first(self) -> None:
        checks = [
            maintenance.Check("local_issues", "l", "attention", "2 ใบ", data={"count": 2}),
        ]
        actions = maintenance.build_actions(checks)
        assert any("ค้างในเครื่อง" in a for a in actions)


class TestCheckCodeScanning:
    """Alerts on the GitHub Security tab never appear in `gh issue list` and CI
    stays green over them, so the sweep must surface them itself (alert #43)."""

    def _fake_run(self, monkeypatch, api_ok: bool, api_out: str, slug_ok: bool = True):
        calls: list[list[str]] = []

        def fake(cmd, cwd=None, timeout=60.0):
            calls.append(cmd)
            if cmd[:3] == ["gh", "repo", "view"]:
                return (True, "takkub/agent-takkub") if slug_ok else (False, "no remote")
            return api_ok, api_out

        monkeypatch.setattr(maintenance, "_run", fake)
        return calls

    def test_empty_is_ok_and_reads_clean(self, tmp_path: Path, monkeypatch) -> None:
        self._fake_run(monkeypatch, True, "[]")
        check = maintenance.check_code_scanning(tmp_path)
        assert check.status == "ok"
        assert check.key == "code_scanning"

    def test_open_alert_is_attention_with_path_line_and_url(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        rows = [
            {
                "number": 43,
                "html_url": "https://github.com/takkub/agent-takkub/security/code-scanning/43",
                "rule": {
                    "id": "py/overly-permissive-file",
                    "description": "Overly permissive file permissions",
                    "security_severity_level": "high",
                },
                "tool": {"name": "CodeQL"},
                "most_recent_instance": {
                    "location": {"path": "src/agent_takkub/resource_lock.py", "start_line": 101}
                },
            }
        ]
        calls = self._fake_run(monkeypatch, True, json.dumps(rows))
        check = maintenance.check_code_scanning(tmp_path)
        assert check.status == "attention"
        assert check.summary.startswith("1 ใบ")
        assert "high 1" in check.summary
        line = check.details[0]
        assert "#43" in line and "[high]" in line
        assert "resource_lock.py:101" in line
        assert "security/code-scanning/43" in line
        assert check.data["numbers"] == [43]
        api = next(c for c in calls if c[:2] == ["gh", "api"])
        assert "repos/takkub/agent-takkub/code-scanning/alerts?state=open" in api[2]

    def test_sorted_by_severity_not_by_recency(self, tmp_path: Path, monkeypatch) -> None:
        rows = [
            {"number": 9, "rule": {"id": "n", "security_severity_level": "low"}},
            {"number": 3, "rule": {"id": "c", "security_severity_level": "critical"}},
            {"number": 7, "rule": {"id": "m", "security_severity_level": "medium"}},
        ]
        self._fake_run(monkeypatch, True, json.dumps(rows))
        check = maintenance.check_code_scanning(tmp_path)
        assert check.data["numbers"] == [3, 7, 9]

    def test_scanning_not_enabled_is_skip_not_error(self, tmp_path: Path, monkeypatch) -> None:
        self._fake_run(monkeypatch, False, "HTTP 404: no analysis found for this repository")
        assert maintenance.check_code_scanning(tmp_path).status == "skip"

    def test_gh_failure_is_error_not_ok(self, tmp_path: Path, monkeypatch) -> None:
        self._fake_run(monkeypatch, False, "gh auth required")
        assert maintenance.check_code_scanning(tmp_path).status == "error"

    def test_no_slug_is_error(self, tmp_path: Path, monkeypatch) -> None:
        self._fake_run(monkeypatch, True, "[]", slug_ok=False)
        assert maintenance.check_code_scanning(tmp_path).status == "error"

    def test_plan_names_the_alerts_only_when_present(self) -> None:
        with_alert = maintenance.build_actions(
            [maintenance.Check("code_scanning", "c", "attention", "1 ใบ")]
        )
        assert any("code scanning" in a for a in with_alert)
        clean = maintenance.build_actions([maintenance.Check("code_scanning", "c", "ok", "")])
        assert not any("code scanning" in a for a in clean)

    def test_no_net_skips_code_scanning(self, tmp_path: Path) -> None:
        log = _write_events(tmp_path / "events.log", [])
        report = maintenance.run_maintenance(tmp_path, include_network=False, log_path=log)
        cs = [c for c in report.checks if c.key == "code_scanning"]
        assert cs and cs[0].status == "skip"
