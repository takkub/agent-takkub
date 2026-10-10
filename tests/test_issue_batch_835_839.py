"""Regression scenarios from the October 10 cockpit incidents."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_takkub import impact_contract, task_ledger
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.report_builder import ReportBuilder


@pytest.mark.parametrize("report_type", ["customer", "dev", "boss"])
def test_custom_template_css_wins_over_shipped_kit(tmp_path: Path, report_type: str) -> None:
    builder = ReportBuilder(report_type, str(tmp_path))
    custom = builder._get_fallback_template().replace(
        "</style>", "#custom-card { background: rgb(12, 34, 56); padding: 37px; }</style>"
    )
    (tmp_path / "template.html").write_text(custom, encoding="utf-8")
    (tmp_path / "content.html").write_text('<div id="custom-card">Card</div>', encoding="utf-8")
    html = builder.build("Custom report")
    assert "background: rgb(12, 34, 56)" in html
    assert '<div id="custom-card">Card</div>' in html
    assert '<div class="lb" id="lb"' in html
    assert "<title>Custom report</title>" in html


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini-agy", "opencode", "cursor"])
def test_read_only_assignment_keeps_its_completion_contract(
    tmp_path, monkeypatch, provider
) -> None:
    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path)
    brief = (
        "Read-only Docker verification: inspect image IDs, health and restart counts.\n"
        "> Historical evidence: already rebuilt web and worker with audio_mode=seedance_only.\n"
        "Do not change audio_mode, shared state or identity. No restart, no DB changes."
    )
    assert impact_contract.plan_from_task(brief) == (None, "")
    warning, _ = task_ledger.create_assignment(
        "readonly", "devops", str(tmp_path), brief, "goal", "verify", provider, task_id="read-only"
    )
    assert not warning
    # The provider's delivery wrapper mentions the impact policy; that must
    # not reclassify the already-accepted original assignment at completion.
    assert not task_ledger.check_impact_completion(
        "readonly",
        "devops",
        "read-only",
        "Verified healthy images",
        task_text=brief + "\nPolicy: shared state and identity changes require impact plans.",
    )
    assert "active task" in task_ledger.check_impact_completion(
        "readonly", "devops", "old-task", "Verified"
    )


@pytest.mark.parametrize(
    "brief",
    [
        "Do not change identity. Then change audio_mode to seedance_only.",
        "Verify identity and update shared state.",
        "Read-only checks first. Switch account for the worker.",
        "Do not restart. [impact-required] Inspect service health.",
    ],
)
def test_real_mutation_still_requires_impact_plan(brief) -> None:
    assert "required" in impact_contract.plan_from_task(brief)[1]


def _inbox(monkeypatch):
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda project=None: "p"))
    orch = Orchestrator()
    orch.shutdown_timers()
    lead = MagicMock()
    lead.model.provider_name = "codex"
    lead.session.is_alive = True
    lead.session.is_at_ready_prompt.return_value = False
    orch._panes_by_project["p"] = {"lead": lead}
    return orch, lead


def test_busy_lead_holds_one_digest_and_preserves_human_priority(monkeypatch) -> None:
    monkeypatch.setenv("TAKKUB_INBOX_DIGEST_MS", "15000")
    orch, lead = _inbox(monkeypatch)
    now = time.time()
    with patch("agent_takkub.lead_inbox.QTimer.singleShot"):
        for i in range(8):
            orch._notify_lead("p", f"[frontend progress] Step {i}", queued_ts=now - 600 + i)
            orch._flush_lead_digest("p")
        assert not orch._lead_notify_queue.get("p")
        lead.session.write.assert_not_called()
        orch._notify_lead("p", "[frontend done] Final result", queued_ts=now - 500)
        orch._enqueue_live_lead_notice("p", "📬 [Lead Inbox Digest — old]")
        orch._enqueue_live_lead_notice("p", "[frontend FAILED] old blocker", front=True)
        orch._notify_lead("p", "Keep this exact user instruction", from_role="user")
        assert orch._lead_notify_queue["p"][0][0] == "Keep this exact user instruction"
        lead.session.is_at_ready_prompt.return_value = True
        orch._flush_lead_digest("p", arm_pump=False)
        digest = orch._lead_notify_queue["p"][-1]
        assert "Final result" in digest[0]
        assert "Step " not in digest[0]
        assert "8m ago" in digest[0]
        assert digest[2] == now - 500


def test_resolved_failure_is_suppressed_but_new_failure_is_retained(monkeypatch) -> None:
    orch, _ = _inbox(monkeypatch)
    orch._wait_done_events[("p", "backend")] = {"ts": 200, "failed": False}
    orch._ps("p::backend").assign_ts = 50
    assert orch._revalidate_system_notice("p", "[backend FAILED] old gate", queued_ts=100) == ""
    fresh = "[backend FAILED] new blocker"
    assert orch._revalidate_system_notice("p", fresh, queued_ts=300) == fresh
    orch._ps("p::backend").assign_ts = 250
    assert orch._revalidate_system_notice("p", fresh, queued_ts=100) == fresh


def test_stale_send_stuck_never_reopens_finished_pane(monkeypatch) -> None:
    orch, _ = _inbox(monkeypatch)
    with patch.object(orch, "list_status", return_value={"frontend": "done"}):
        assert orch._revalidate_system_notice("p", "⚠️ [send-stuck] ข้อความไปยัง frontend ค้าง") == ""
