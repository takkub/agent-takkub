"""#365 phase 10 — `takkub doctor --workspace`'s pure formatting/check layer."""

from agent_takkub.doctor import Status, check_workspace_monaco_bundle, format_workspace_report


class TestCheckWorkspaceMonacoBundle:
    def test_reports_ok_when_bundle_present(self) -> None:
        # The repo's own vendored bundle — this test asserts against the
        # real checked-in tree, same convention as check_installed_integrity.
        findings = check_workspace_monaco_bundle()
        assert len(findings) == 1
        assert findings[0].status == Status.OK
        assert findings[0].category == "workspace"
        assert "MB" in findings[0].detail

    def test_reports_fail_when_bundle_files_missing(self, tmp_path) -> None:
        findings = check_workspace_monaco_bundle(vendor_dir=tmp_path / "vendor")
        assert len(findings) == 1
        assert findings[0].status == Status.FAIL
        assert "loader.js" in findings[0].detail
        assert findings[0].fix_hint


def test_workspace_report_reports_cockpit_not_running_when_resp_is_none() -> None:
    text = format_workspace_report(None)
    assert "cockpit is not running" in text


def test_workspace_report_reports_live_failure() -> None:
    text = format_workspace_report({"ok": False, "msg": "connection refused"})
    assert "live workspace diagnostics failed: connection refused" in text


def test_workspace_report_editor_host_not_registered() -> None:
    resp = {
        "ok": True,
        "editor_host": {"registered": False},
        "preview": {},
        "design_artifacts": {},
        "per_project": {},
    }
    text = format_workspace_report(resp)
    assert "editor host: not registered" in text


def test_workspace_report_editor_host_state() -> None:
    resp = {
        "ok": True,
        "editor_host": {"registered": True, "has_view": True, "open_count": 2},
        "preview": {},
        "design_artifacts": {},
        "per_project": {},
    }
    text = format_workspace_report(resp)
    assert "instance=yes" in text
    assert "open_tabs=2" in text


def test_workspace_report_preview_state_and_nav_blocks() -> None:
    resp = {
        "ok": True,
        "editor_host": {"registered": False},
        "preview": {
            "proj1": {
                "project": "proj1",
                "mode": "url",
                "target": "http://127.0.0.1:5173",
                "device": "desktop",
                "approved": False,
                "nav_blocks": 3,
            }
        },
        "design_artifacts": {},
        "per_project": {},
    }
    text = format_workspace_report(resp)
    assert "preview[proj1]" in text
    assert "nav_blocks=3" in text


def test_workspace_report_design_artifacts_by_status() -> None:
    resp = {
        "ok": True,
        "editor_host": {"registered": False},
        "preview": {},
        "design_artifacts": {"proj1": {"count": 3, "by_status": {"draft": 2, "approved": 1}}},
        "per_project": {},
    }
    text = format_workspace_report(resp)
    assert "design artifacts[proj1]: 3 total" in text
    assert "draft=2" in text
    assert "approved=1" in text


def test_workspace_report_design_artifacts_read_error() -> None:
    resp = {
        "ok": True,
        "editor_host": {"registered": False},
        "preview": {},
        "design_artifacts": {"proj1": {"error": "OSError: boom"}},
        "per_project": {},
    }
    text = format_workspace_report(resp)
    assert "design artifacts[proj1]: read failed — OSError: boom" in text


def test_workspace_report_no_registered_sources() -> None:
    resp = {
        "ok": True,
        "editor_host": {"registered": False},
        "preview": {},
        "design_artifacts": {},
        "per_project": {},
    }
    text = format_workspace_report(resp)
    assert "no project has a registered diagnostic source" in text


def test_workspace_report_per_project_sources() -> None:
    resp = {
        "ok": True,
        "editor_host": {"registered": False},
        "preview": {},
        "design_artifacts": {},
        "per_project": {
            "proj1": {
                "file_watch": {"watched_count": 2, "pending_count": 1, "debounce_ms": 400},
                "git_changes": {
                    "last_status_ms": 42.5,
                    "last_status_error": None,
                    "status_run_count": 5,
                },
                "tree_index": {
                    "last_scan_ms": 3.2,
                    "last_scan_entry_count": 10,
                    "scan_count": 4,
                },
            }
        },
    }
    text = format_workspace_report(resp)
    assert "file_watch[proj1]: watched=2 pending=1 debounce_ms=400" in text
    assert "git_changes[proj1]: last_status_ms=42.5" in text
    assert "tree_scan[proj1]: last_scan_ms=3.2 entries=10 scans=4" in text


def test_workspace_report_git_changes_error_shown() -> None:
    resp = {
        "ok": True,
        "editor_host": {"registered": False},
        "preview": {},
        "design_artifacts": {},
        "per_project": {
            "proj1": {
                "git_changes": {
                    "last_status_ms": 10.0,
                    "last_status_error": "git exited 128: not a repository",
                    "status_run_count": 1,
                },
            }
        },
    }
    text = format_workspace_report(resp)
    assert "error=git exited 128: not a repository" in text
