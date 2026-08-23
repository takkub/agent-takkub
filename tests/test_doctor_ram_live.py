"""#364 lever 6 — `takkub doctor --ram`'s pure formatting layer."""

from agent_takkub.doctor import Status, check_ram_node_children, format_ram_report


def test_ram_report_reports_cockpit_not_running_when_resp_is_none() -> None:
    text = format_ram_report(None)
    assert "cockpit is not running" in text


def test_ram_report_reports_live_failure() -> None:
    text = format_ram_report({"ok": False, "msg": "connection refused"})
    assert "live RAM report failed: connection refused" in text


def test_ram_report_renders_table_sorted_by_total_desc() -> None:
    resp = {
        "ok": True,
        "generated_at": 1_700_000_000.0,
        "takkub_version": "1.0.87",
        "main_process": {"pid": 100, "rss_bytes": 300_000_000},
        "shared_qtwebengine": {"rss_bytes": 99_000_000, "process_count": 1},
        "panes": [
            {
                "role": "codex",
                "project": "proj1",
                "provider": "codex",
                "pid": 300,
                "cli_rss_bytes": 400_000_000,
                "node_children_rss_bytes": 0,
                "node_children_count": 0,
                "qtwebengine_rss_bytes": 0,
                "total_bytes": 400_000_000,
                "note": "",
            },
            {
                "role": "backend",
                "project": "proj1",
                "provider": "claude",
                "pid": 200,
                "cli_rss_bytes": 550_000_000,
                "node_children_rss_bytes": 80_000_000,
                "node_children_count": 1,
                "qtwebengine_rss_bytes": 99_000_000,
                "total_bytes": 729_000_000,
                "note": "",
            },
            {
                "role": "gone",
                "project": "proj1",
                "provider": "claude",
                "pid": 999,
                "cli_rss_bytes": 0,
                "node_children_rss_bytes": 0,
                "node_children_count": 0,
                "qtwebengine_rss_bytes": 0,
                "total_bytes": 0,
                "note": "process not found (exited?)",
            },
        ],
        "total_panes_bytes": 1_129_000_000,
        "total_cockpit_bytes": 1_528_000_000,
        "machine_total_bytes": 16_000_000_000,
        "machine_available_bytes": 4_000_000_000,
        "machine_available_percent": 25.0,
        "governor_min_available_ram_percent": 12.0,
    }

    text = format_ram_report(resp)
    lines = text.splitlines()

    backend_line = next(line for line in lines if "backend" in line)
    codex_line = next(line for line in lines if "codex" in line)
    assert lines.index(backend_line) < lines.index(codex_line)
    assert "process not found (exited?)" in text
    assert "main process (pythonw)" in text
    assert "pid=100" in text
    assert "shared QtWebEngine" in text
    assert "1 process(es)" in text
    assert "governor pause line: 12% available" in text
    assert "takkub_version=1.0.87" in text


def test_ram_report_flags_discarded_pane() -> None:
    """#364 lever 1: a discarded pane's row is marked so the near-zero RSS
    reads as "reclaimed", not "broken"."""
    resp = {
        "ok": True,
        "generated_at": 1_700_000_000.0,
        "takkub_version": "1.0.87",
        "main_process": {"pid": 100, "rss_bytes": 300_000_000},
        "shared_qtwebengine": {"rss_bytes": 0, "process_count": 0},
        "panes": [
            {
                "role": "backend",
                "project": "proj1",
                "provider": "claude",
                "pid": 200,
                "discarded": True,
                "cli_rss_bytes": 10_000_000,
                "node_children_rss_bytes": 0,
                "node_children_count": 0,
                "qtwebengine_rss_bytes": 0,
                "total_bytes": 10_000_000,
                "note": "",
            },
            {
                "role": "lead",
                "project": "proj1",
                "provider": "claude",
                "pid": 201,
                "discarded": False,
                "cli_rss_bytes": 300_000_000,
                "node_children_rss_bytes": 0,
                "node_children_count": 0,
                "qtwebengine_rss_bytes": 60_000_000,
                "total_bytes": 360_000_000,
                "note": "",
            },
        ],
        "total_panes_bytes": 370_000_000,
        "total_cockpit_bytes": 670_000_000,
        "machine_total_bytes": 16_000_000_000,
        "machine_available_bytes": 4_000_000_000,
        "machine_available_percent": 25.0,
        "governor_min_available_ram_percent": 12.0,
    }

    text = format_ram_report(resp)
    lines = text.splitlines()

    backend_line = next(line for line in lines if "backend" in line)
    lead_line = next(line for line in lines if "lead" in line)
    assert "[discarded]" in backend_line
    assert "[discarded]" not in lead_line


class TestCheckRamNodeChildren:
    """#364 lever 4 — the `[ram]` node/mcp-children threshold WARN."""

    def test_no_findings_when_resp_is_none(self) -> None:
        assert check_ram_node_children(None) == []

    def test_no_findings_when_live_check_failed(self) -> None:
        assert check_ram_node_children({"ok": False, "msg": "connection refused"}) == []

    def test_no_finding_under_threshold(self) -> None:
        resp = {
            "ok": True,
            "panes": [
                {
                    "role": "devops",
                    "project": "proj1",
                    "pid": 200,
                    "node_children_rss_bytes": 180_000_000,  # graft-only baseline
                    "node_children_count": 4,
                }
            ],
        }
        assert check_ram_node_children(resp) == []

    def test_warns_and_names_the_pane_over_threshold(self) -> None:
        resp = {
            "ok": True,
            "panes": [
                {
                    "role": "backend",
                    "project": "proj1",
                    "pid": 200,
                    "node_children_rss_bytes": 180_000_000,
                    "node_children_count": 4,
                },
                {
                    "role": "qa",
                    "project": "proj1",
                    "pid": 300,
                    "node_children_rss_bytes": 900_000_000,
                    "node_children_count": 12,
                },
            ],
        }

        findings = check_ram_node_children(resp)

        assert len(findings) == 1
        finding = findings[0]
        assert finding.status == Status.WARN
        assert finding.category == "ram"
        assert "qa" in finding.detail
        assert "proj1" in finding.detail
        assert "858" in finding.detail  # 900_000_000 bytes -> MiB, matches _fmt_mb's rounding
        assert "pid=300" in finding.detail
        assert "backend" not in finding.detail

    def test_custom_threshold_is_honored(self) -> None:
        resp = {
            "ok": True,
            "panes": [
                {
                    "role": "devops",
                    "project": "proj1",
                    "pid": 200,
                    "node_children_rss_bytes": 50_000_000,
                    "node_children_count": 1,
                }
            ],
        }
        assert check_ram_node_children(resp, threshold_bytes=10_000_000) != []
        assert check_ram_node_children(resp, threshold_bytes=100_000_000) == []
