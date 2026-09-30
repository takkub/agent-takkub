"""Regressions from the September 30 cockpit issue reports."""

import json
import os
import sys
from types import SimpleNamespace

from agent_takkub import pane_guard, qa_gate
from agent_takkub.cli import _git_lead_only_task_warning
from agent_takkub.orchestrator_text import done_report_warnings
from agent_takkub.provider_spec import picker_question_on_screen, quota_markers_for
from agent_takkub.routing_planner import classify_failure


def test_historical_checkout_is_not_a_pane_instruction():
    assert not _git_lead_only_task_warning("fixture ถูก git checkout เป็น CRLF", "shared")
    assert not _git_lead_only_task_warning("fixture was `git checkout` to CRLF", "shared")
    assert "warning นี้ไม่บล็อก assign" in _git_lead_only_task_warning(
        "Run git checkout -- fixture.txt", "shared"
    )


def test_typecheck_can_run_beside_other_panes_but_not_under_overload(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"working_panes": ["backend@other"], "cpu_percent": 20}))
    assert pane_guard.classify("npx tsc --noEmit", "frontend", snapshot_path=snapshot).allowed
    assert not pane_guard.classify("npx tsc -b", "frontend", snapshot_path=snapshot).allowed
    snapshot.write_text(json.dumps({"working_panes": [], "overloaded": True}))
    assert not pane_guard.classify("npx tsc --noEmit", "frontend", snapshot_path=snapshot).allowed


def test_claude_auto_continue_quota_screen_is_not_a_question():
    screen = SimpleNamespace(
        display_lines=lambda: [
            "Usage limit reached · continuing automatically at 11:10am",
            "Esc to cancel",
        ]
    )
    assert "usage limit reached" in quota_markers_for("claude")
    assert not picker_question_on_screen(screen, "claude")


def test_backend_renderer_failure_uses_file_ownership_and_role():
    assert classify_failure("FAIL api/src/xlsx.renderer.spec.ts")[0] == "backend"
    assert (
        classify_failure("FAIL billing-summary.aggregate.spec.ts render mismatch", "backend")[0]
        == "backend"
    )
    assert classify_failure("FAIL frontend/src/component.test.tsx")[0] == "frontend"


def test_done_quality_flags_missing_fields_and_unverified_typecheck():
    assert "note incomplete" in done_report_warnings("81 tests passed")[0]
    assert not done_report_warnings("CHANGED: x.py\nEVIDENCE: pytest 1 passed\nREMOVED: None")
    assert "typecheck unverified" in done_report_warnings("ยกเว้น tsc ไม่ยืนยัน typecheck")


def test_real_subprocess_failure_lists_all_suites_and_saves_full_log(tmp_path):
    script = (
        "import sys; print('FAIL api/a.spec.ts'); print('FAIL api/b.spec.ts'); "
        "print('\\n'.join('extra output' for _ in range(210))); sys.exit(1)"
    )
    step = qa_gate._run_step(
        "test", [sys.executable, "-c", script], dict(os.environ), tmp_path, tmp_path / "logs"
    )
    assert not step.ok
    assert step.failed_suites == ["api/a.spec.ts", "api/b.spec.ts"]
    assert "FAIL api/a.spec.ts" in step.log_path.read_text(encoding="utf-8")
    report = qa_gate.GateReport(steps=[step])
    for rendered in (qa_gate.render_table(report), qa_gate.render_report_md(report, "head")):
        assert "api/a.spec.ts" in rendered and "api/b.spec.ts" in rendered
