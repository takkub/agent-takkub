"""Regression tests for issues #827, #828, #829, #830, #831, and #832."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Issue #827: Git Snapshot Baseline on Assign ─────────────────────────────


def test_issue_827_default_cwd_fallback_to_lead_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When a role has no staging dir and paths is empty, config.default_cwd_for_role
    falls back to lead_cwd instead of returning None and defaulting to DATA_HOME."""
    from agent_takkub import config

    project_root = tmp_path / "my_project"
    project_root.mkdir()

    monkeypatch.setattr(config, "lead_cwd", lambda p: str(project_root))
    monkeypatch.setattr(config, "_project_dict", lambda p: {"paths": {}})

    resolved = config.default_cwd_for_role("lead", "test_proj")
    assert resolved == str(project_root)


# ── Issue #828: Note Incomplete on Report File / Markdown Headers ───────────


def test_issue_828_thai_and_markdown_headings(tmp_path: Path):
    """Thai headers and variations (e.g. ผลการทดสอบ, แก้ไข) must not alarm as incomplete."""
    from agent_takkub.orchestrator_text import done_report_warnings

    report_file = tmp_path / "report.md"
    report_file.write_text(
        "## การแก้ไข\n- แก้ไขบั๊ก queue stacking และ session resume\n\n"
        "## ผลการทดสอบ\n- รัน pytest ผ่าน 100%\n",
        encoding="utf-8",
    )

    note = f"ดูรายละเอียดที่ report: {report_file}"
    warnings = done_report_warnings(note, report_path=str(report_file))
    assert not any("incomplete" in w.lower() for w in warnings)


def test_issue_828_inline_markdown_headers():
    """Markdown headers like ## CHANGES and ## VERIFICATION are accepted."""
    from agent_takkub.orchestrator_text import done_report_warnings

    note = "## CHANGES\n- updated logic\n\n## VERIFICATION\n- tested manually"
    warnings = done_report_warnings(note)
    assert not any("incomplete" in w.lower() for w in warnings)


# ── Issue #829: Codex Subagent Thread Resume Crash ─────────────────────────


def test_issue_829_codex_helper_ignores_subagent_threads(tmp_path: Path):
    """resolve_newest_codex_session_for_cwd must ignore child rollouts from fan-out."""
    from agent_takkub import codex_helper

    work_dir = tmp_path / "workspace"
    work_dir.mkdir()

    sessions_dir = tmp_path / "sessions" / "2026" / "10" / "10"
    sessions_dir.mkdir(parents=True)

    parent_rollout = sessions_dir / "rollout-parent.jsonl"
    subagent_rollout = sessions_dir / "rollout-subagent.jsonl"

    # Parent session: older timestamp, no parent_thread_id
    parent_meta = {
        "type": "session_meta",
        "payload": {
            "id": "thread-parent-1234",
            "cwd": str(work_dir),
        },
    }
    parent_rollout.write_text(json.dumps(parent_meta) + "\n", encoding="utf-8")

    # Child session: newer timestamp, thread_source=subagent or parent_thread_id
    child_meta = {
        "type": "session_meta",
        "payload": {
            "id": "thread-child-5678",
            "cwd": str(work_dir),
            "parent_thread_id": "thread-parent-1234",
            "thread_source": "subagent",
        },
    }
    subagent_rollout.write_text(json.dumps(child_meta) + "\n", encoding="utf-8")

    with (
        patch.object(codex_helper, "codex_sessions_root", return_value=tmp_path / "sessions"),
        patch.object(
            codex_helper, "codex_archived_sessions_root", return_value=tmp_path / "archived"
        ),
    ):
        # parent_only=True must skip child rollout even though child has later mtime
        resolved = codex_helper.resolve_newest_codex_session_for_cwd(
            str(work_dir), parent_only=True
        )
        assert resolved == parent_rollout


# ── Issue #830: Codex Lead AGENTS.md Policy Delivery & Checker Provenance ───


def test_issue_830_render_lead_agents_md_preserves_user_owned(tmp_path: Path):
    """User-owned AGENTS.md without TAKKUB_MARKER must NOT be overwritten, and
    render_lead_agents_md_with_reason must return (None, 'user-owned', text)."""
    from agent_takkub.lead_context import render_lead_agents_md_with_reason

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    agents_file = project_dir / "AGENTS.md"
    user_content = "# Project Custom Instructions\n- Do not touch this\n"
    agents_file.write_text(user_content, encoding="utf-8")

    path, reason, text = render_lead_agents_md_with_reason("proj", str(project_dir))
    assert path is None
    assert reason == "user-owned"
    assert text is not None
    assert agents_file.read_text(encoding="utf-8") == user_content


def test_issue_830_render_lead_agents_md_writes_when_managed(tmp_path: Path):
    """Managed AGENTS.md with TAKKUB_MARKER is refreshed and returns ('written')."""
    from agent_takkub.codex_agents_md import TAKKUB_MARKER
    from agent_takkub.lead_context import render_lead_agents_md_with_reason

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    agents_file = project_dir / "AGENTS.md"
    agents_file.write_text(f"{TAKKUB_MARKER}\nold\n", encoding="utf-8")

    path, reason, _text = render_lead_agents_md_with_reason("proj", str(project_dir))
    assert path == str(agents_file)
    assert reason == "written"
    assert TAKKUB_MARKER in agents_file.read_text(encoding="utf-8")


def test_issue_830_verify_checker_provenance():
    """verify_checker_provenance enforces registered done contract."""
    from agent_takkub import team_preset

    # Solo-lead (self-verify)
    with patch.object(
        team_preset, "current", return_value={"preset": "solo-lead", "checker": None}
    ):
        ok, reason, _details = team_preset.verify_checker_provenance("proj")
        assert ok is True
        assert reason == "self-verify"

    # Pair (checker=reviewer) without registered done in task_ledger
    with (
        patch.object(
            team_preset, "current", return_value={"preset": "pair", "checker": "reviewer"}
        ),
        patch("agent_takkub.task_ledger.load_state", return_value={"groups": []}),
    ):
        ok, reason, _details = team_preset.verify_checker_provenance("proj")
        assert ok is False
        assert "no registered cockpit done report" in reason

    # Pair with registered done in task_ledger
    ledger_state = {
        "groups": [
            {"features": [{"rows": [{"role": "reviewer", "status": "ok", "task_id": "abc12345"}]}]}
        ]
    }
    with (
        patch.object(
            team_preset, "current", return_value={"preset": "pair", "checker": "reviewer"}
        ),
        patch("agent_takkub.task_ledger.load_state", return_value=ledger_state),
    ):
        ok, reason, _details = team_preset.verify_checker_provenance("proj")
        assert ok is True
        assert "verified by registered reviewer" in reason


def test_issue_830_lead_edits_uncovered_warning():
    """get_lead_edits_status flags providers without file guards as covered=False."""
    from agent_takkub import pane_guard

    with patch("agent_takkub.provider_config.effective_provider_for", return_value="codex"):
        status = pane_guard.get_lead_edits_status(project="test_proj")
        assert status["covered"] is False
        assert status["provider"] == "codex"


# ── Issue #831: Gemini Queue Stacking & Mode Alias Cancellation ─────────────


def test_issue_831_gemini_busy_queue_markers():
    """gemini_spec must declare busy_queue_marker and confirm markers."""
    from agent_takkub.provider_spec import spec_for

    spec = spec_for("gemini")
    assert spec is not None
    assert spec.busy_queue_marker == "press up to edit queued messages"
    assert "queued messages" in spec.busy_queue_confirm_markers


# ── Issue #832: PWA Provider and Account Switching ──────────────────────────


def test_issue_832_notify_codex_sessions_root_per_project():
    """notify._codex_sessions_root passes project_ns to codex_sessions_root."""
    from agent_takkub.remote import notify

    with patch("agent_takkub.codex_helper.codex_sessions_root") as mock_root:
        mock_root.return_value = Path("/tmp/codex-profile")
        res = notify._codex_sessions_root("custom_proj")
        mock_root.assert_called_once_with("custom_proj")
        assert res == Path("/tmp/codex-profile")


def test_issue_832_remote_api_lead_history_returns_account():
    """api.lead_history must include 'account' in its response."""
    from agent_takkub.remote import api

    mock_orch = MagicMock()
    mock_orch._panes_by_project = {}

    with (
        patch("agent_takkub.remote.notify.lead_history_snapshot", return_value=("codex", [])),
        patch("agent_takkub.remote.notify.lead_mirror_diagnosis", return_value=None),
        patch("agent_takkub.user_profile.profile_for", return_value="monchai500"),
    ):
        res = api.lead_history(mock_orch, "test_proj")
        assert res["provider"] == "codex"
        assert res["account"] == "monchai500"


# ── Issue #833: Spec Confirmation Does Not Pop Up Dialog ────────────────────


def test_issue_833_spec_confirmation_does_not_block_backlog_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When a task includes numbers/currency (e.g. 2.4192 บาท), it must NOT pop up
    a dialog or block; it must create the backlog card and assign immediately."""
    from agent_takkub import backlog
    from agent_takkub.orchestrator import Orchestrator
    from agent_takkub.work_discipline import is_spec_confirmation_enabled, needs_spec_confirmation

    monkeypatch.setattr(backlog, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(
        Orchestrator, "_resolve_project", staticmethod(lambda project=None: project or "p")
    )

    task = "ให้สร้างชีต กวางกับมินดา มีอย่าง 2 ภาพ รวม 2.4192 บาท แล้วตรวจภาพ"
    assert needs_spec_confirmation(task) is True
    assert is_spec_confirmation_enabled() is False

    orch = Orchestrator()
    orch.shutdown_timers()
    monkeypatch.setattr(orch, "spawn", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(orch, "_send_when_ready", lambda *a, **kw: None)

    linked, _, card_id = orch.backlog_for_assign("p", "backend", task)
    assert linked and card_id

    # assign must succeed and keep the backlog card without calling _confirm_task_discipline
    confirm_called = []
    original_confirm = orch._confirm_task_discipline

    def wrapped_confirm(*args, **kwargs):
        confirm_called.append(args)
        return original_confirm(*args, **kwargs)

    # Do not attach mock to __dict__ directly so we test normal runtime path
    monkeypatch.setattr(Orchestrator, "_confirm_task_discipline", wrapped_confirm)

    ok, _msg = orch.assign("backend", None, task, project="p")
    assert ok is True
    assert len(confirm_called) == 0
    item = backlog.get_item("p", card_id)
    assert item is not None
    assert item["status"] == "doing"
