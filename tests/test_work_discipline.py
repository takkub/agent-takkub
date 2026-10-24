from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from agent_takkub.work_discipline import (
    DEFAULT_LIMITS,
    cap_reason,
    confirmation_digest,
    needs_spec_confirmation,
    task_limits,
)


def test_spec_confirmation_detects_numeric_and_business_rule_tasks():
    assert needs_spec_confirmation("Set the refund threshold to 14 days")
    assert needs_spec_confirmation("Set commission turnover 65 to 3% when payout > 100,000 baht")
    assert not needs_spec_confirmation("Implement the eligibility condition for refunds")
    assert not needs_spec_confirmation("Fix typo in issue #514")


def test_real_task_corpus_ignores_technical_numbers_and_code_spans():
    # Representative excerpts from the five live task documents named in the
    # 655512b2 fix brief. Keep their recurring code/version/count syntax here
    # so CI does not depend on the operator's private runtime directory.
    corpus = (
        "ทำ 4 ข้อ · ข้อ 3 จำกัดรอบแก้ pane backend issue #762",
        "fix รอบ 1 (#762) · test 3 ตัว · backlog_for_assign",
        "config.py:297, 355 · Codex · DATA_HOME · project skill",
        "ใบงานยาว (~2.5KB ไทย) · 2 ครั้งติดวันนี้ 09:30 และ 09:31",
        "worktree wt/backend-1790649023 · 0 commit ahead · --backlog · ต่อยอด fix_loop",
        "provider_spec.py:1627 v2.1.46 10s 390px 5 files ≤2 rounds `tax 8%`",
    )
    assert all(not needs_spec_confirmation(task) for task in corpus)
    assert not needs_spec_confirmation(
        "Token cap counts context total 200,000 tokens, cached input and 77.5M usage"
    )
    assert not needs_spec_confirmation(
        "BLOCKER token cap: current_usage total snapshot; normal task budget 200,000 tokens"
    )
    assert not needs_spec_confirmation(
        'Fix false-positive spec gate: token total is too high; for example "commission turnover 65 to 3%"'
    )


def test_confirmation_detects_numeric_business_rules_but_not_technical_refs():
    assert needs_spec_confirmation("Update commission handling in provider_spec.py:1627") is False
    assert needs_spec_confirmation("Change tax to 7% in src/tax.py:1627")
    assert needs_spec_confirmation("ปรับค่าคอม turnover 65 เป็น 3% เมื่อยอด > 100,000 บาท")


def test_confirmation_detects_decimal_rates_but_ignores_versions_and_floating_numbers():
    assert needs_spec_confirmation("ค่าคอม 2.5%")
    assert needs_spec_confirmation("3.75 เปอร์เซ็นต์")
    assert needs_spec_confirmation("fee 1.5% ต่อรายการ")
    assert needs_spec_confirmation("fee is 2.5 percent of each deposit")
    assert needs_spec_confirmation("withdrawal fee 3.75 บาท")
    assert not needs_spec_confirmation("v2.1.46 / 2.1.46")
    assert not needs_spec_confirmation("ตัวอย่างค่าคอม saas ใกล้เลข 2 ลอยๆ")
    assert not needs_spec_confirmation("ค่าคอม saas แล้วเลขข้อ 2")


def test_detector_review_corpus_does_not_treat_incidental_numbers_as_business_values():
    maintenance_note = (
        "Review the detector false positive: the example 'commission turnover' appears "
        "in the report, and 2 files still need checking."
    )
    assert not needs_spec_confirmation(maintenance_note)


def test_confirmation_requires_business_domain_for_percent_unit_values():
    assert not needs_spec_confirmation("RAM 90%")
    assert not needs_spec_confirmation("coverage 80%")
    assert not needs_spec_confirmation("5h 70% left")
    assert needs_spec_confirmation("ค่าคอม 2.5%")


def test_confirmation_digest_binds_exact_task_text():
    assert confirmation_digest("calculate 12%") == confirmation_digest(" calculate 12% ")
    assert confirmation_digest("calculate 12%") != confirmation_digest("calculate 15%")


def test_default_limits_apply_and_settings_can_override(tmp_path):
    assert task_limits("normal", tmp_path).minutes == DEFAULT_LIMITS["normal"]["minutes"]
    (tmp_path / "work-discipline.json").write_text(
        json.dumps({"limits": {"normal": {"minutes": 7}}}),
        encoding="utf-8",
    )
    assert task_limits("normal", tmp_path).minutes == 7


def test_limits_validate_values_and_detect_first_crossing():
    assert cap_reason(elapsed_s=120, minutes=2) == "time"
    assert cap_reason(elapsed_s=30, minutes=2) is None


def test_no_per_task_token_cap():
    """User 2026-10-24: tasks are never stopped for token usage."""
    assert all(set(row) == {"minutes"} for row in DEFAULT_LIMITS.values())


def test_provider_discipline_gaps_are_visible_in_capability_matrix():
    from agent_takkub.provider_spec import PROVIDER_REGISTRY, capability_matrix

    assert capability_matrix(PROVIDER_REGISTRY["codex"])["lead_context_recovery"] == "supported"
    assert capability_matrix(PROVIDER_REGISTRY["gemini"])["lead_context_recovery"] == "unsupported"
    assert "task_token_budget" not in capability_matrix(PROVIDER_REGISTRY["cursor"])


def test_assign_does_not_treat_digest_flag_as_user_confirmation(monkeypatch):
    from agent_takkub import orchestrator as orch_mod
    from agent_takkub.orchestrator import Orchestrator

    events = []
    monkeypatch.setattr(
        orch_mod, "_log_event", lambda event, **fields: events.append((event, fields))
    )
    orch = Orchestrator.__new__(Orchestrator)
    orch._resolve_project = lambda project=None: project or "test-project"
    ok, message = Orchestrator.assign(
        orch,
        "backend",
        None,
        "Set refund threshold to 14 days",
        project="test-project",
        spec_confirmation=confirmation_digest("Set refund threshold to 14 days"),
    )
    assert not ok
    assert "ผู้ใช้" in message
    assert events[0][0] == "task_spec_confirmation_denied"
    assert events[0][1]["digest"] == confirmation_digest("Set refund threshold to 14 days")


def test_rejected_spec_removes_its_unstarted_auto_backlog_card(monkeypatch, tmp_path):
    from agent_takkub import backlog
    from agent_takkub.orchestrator import Orchestrator

    monkeypatch.setattr(backlog, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(
        Orchestrator, "_resolve_project", staticmethod(lambda project=None: project or "p")
    )
    orch = Orchestrator()
    orch.shutdown_timers()
    task = "Set refund threshold to 14 days"
    linked, _, card_id = orch.backlog_for_assign("p", "backend", task)
    assert linked and backlog.get_item("p", card_id)["status"] == "doing"
    monkeypatch.setattr(orch, "_confirm_task_discipline", lambda *_args: False)
    ok, _ = orch.assign("backend", None, task, project="p")
    assert not ok
    assert backlog.get_item("p", card_id) is None


def test_cockpit_confirmation_slot_returns_the_users_button_choice(monkeypatch):
    from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit

    from agent_takkub import main_window

    app = main_window.QApplication.instance() or main_window.QApplication([])
    long_task = "Set tax to 7%\n" + "รายละเอียดงานยาวมาก\n" * 80

    accepted = []
    for choice in (True, False):

        def fake_exec(dialog, _choice=choice):
            body = dialog.findChild(QPlainTextEdit, "taskConfirmationText")
            buttons = dialog.findChild(QDialogButtonBox)
            assert body is not None and body.isReadOnly()
            assert body.toPlainText() == long_task
            assert buttons is not None and len(buttons.buttons()) == 2
            assert dialog.maximumHeight() <= app.primaryScreen().availableGeometry().height() - 32
            dialog.show()
            app.processEvents()
            assert body.verticalScrollBar().maximum() > 0
            assert buttons.geometry().bottom() <= dialog.contentsRect().bottom()
            dialog.hide()
            return QDialog.DialogCode.Accepted if _choice else QDialog.DialogCode.Rejected

        monkeypatch.setattr(main_window.QDialog, "exec", fake_exec)
        request = {
            "kind": "spec",
            "role": "backend",
            "project": "test-project",
            "task": long_task,
            "digest": "digest",
            "done": threading.Event(),
            "confirmed": False,
        }
        main_window.MainWindow._confirm_task_discipline(object(), request)
        assert request["done"].is_set()
        assert request["confirmed"] is choice
        accepted.append(request["confirmed"])
    assert accepted == [True, False]


def test_task_budget_watchdog_interrupts_and_logs_once(monkeypatch, tmp_path):
    from agent_takkub import config
    from agent_takkub import orchestrator as orch_mod

    writes = []
    write_results = iter((False, True))

    def write(data):
        writes.append(data)
        return next(write_results)

    session = SimpleNamespace(is_alive=True, write=write)
    pane = SimpleNamespace(
        provider="codex",
        model=SimpleNamespace(provider_name="codex"),
        session=session,
    )
    state = SimpleNamespace(
        last_assigned_task="ongoing",
        task_budget_halted=False,
        assign_ts=100.0,
        last_assigned_scope="tiny",
        last_turn_end_ts=99,
    )
    notices = []
    orch = SimpleNamespace(
        _panes_by_project={"proj": {"backend": pane}},
        _ps=lambda key: state,
        _notify_lead=lambda *args, **kwargs: None,
        taskBudgetNotice=SimpleNamespace(emit=lambda *args: notices.append(args)),
    )
    # Use pytest's per-test directory for both DATA_HOME-backed settings and
    # events.log, proving the runtime event without touching the user's home.
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    (tmp_path / "work-discipline.json").write_text(
        json.dumps({"limits": {"tiny": {"minutes": 1}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TAKKUB_EVENTS_LOG_SYNC", "1")
    orch_mod.Orchestrator._check_task_work_limits(orch, now=101)
    assert writes == []  # under the time cap: untouched
    orch_mod.Orchestrator._check_task_work_limits(orch, now=161)
    assert not state.task_budget_halted  # first write failed
    orch_mod.Orchestrator._check_task_work_limits(orch, now=162)
    orch_mod.Orchestrator._check_task_work_limits(orch, now=163)
    rows = [
        json.loads(line)
        for line in (tmp_path / "events.log").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["event"] == "task_work_budget_exceeded"
    assert rows[0]["provider"] == "codex"
    assert rows[0]["reason"] == "time"
    assert writes == ["\x03", "\x03"]
    assert state.task_budget_halted
    assert len(notices) == 1
    assert len(rows) == 1


def test_budget_resume_uses_a_real_qt_signal_receiver():
    from PyQt6.QtCore import QObject

    from agent_takkub.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    QObject.__init__(orch)
    signal = orch.taskDisciplineConfirmation
    assert orch.receivers(signal) == 0

    def confirm(request):
        request["confirmed"] = True
        request["done"].set()

    signal.connect(confirm)
    assert orch.receivers(signal) == 1
    assert Orchestrator._confirm_task_discipline(
        orch, "budget", "backend", "project", "retry task", "digest"
    )
