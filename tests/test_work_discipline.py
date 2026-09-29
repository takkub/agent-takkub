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
    task_turn_tokens,
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


def test_confirmation_digest_binds_exact_task_text():
    assert confirmation_digest("calculate 12%") == confirmation_digest(" calculate 12% ")
    assert confirmation_digest("calculate 12%") != confirmation_digest("calculate 15%")


def test_default_limits_apply_and_settings_can_override(tmp_path):
    assert task_limits("normal", tmp_path).minutes == DEFAULT_LIMITS["normal"]["minutes"]
    (tmp_path / "work-discipline.json").write_text(
        json.dumps({"limits": {"normal": {"minutes": 7, "tokens": 12_000}}}),
        encoding="utf-8",
    )
    assert task_limits("normal", tmp_path).minutes == 7
    assert task_limits("normal", tmp_path).tokens == 12_000


def test_limits_validate_values_and_detect_first_crossing():
    assert cap_reason(elapsed_s=60, minutes=2, context_tokens=20, token_cap=10) == "tokens"
    assert cap_reason(elapsed_s=120, minutes=2, context_tokens=0, token_cap=10) == "time"
    assert cap_reason(elapsed_s=30, minutes=2, context_tokens=0, token_cap=10) is None


def test_default_token_limits_leave_room_for_normal_work_and_catch_extreme_usage():
    assert DEFAULT_LIMITS["normal"]["tokens"] >= 500_000
    assert DEFAULT_LIMITS["deep"]["tokens"] < 77_500_000
    assert (
        cap_reason(
            elapsed_s=1,
            minutes=120,
            context_tokens=77_500_000,
            token_cap=DEFAULT_LIMITS["deep"]["tokens"],
        )
        == "tokens"
    )


def test_task_turn_tokens_excludes_replayed_cache_and_handles_provider_shapes():
    assert (
        task_turn_tokens("codex", {"input": 60_000, "cache_read": 59_000, "output": 100}) == 1_100
    )
    assert (
        task_turn_tokens(
            "claude", {"input": 800, "cache_creation": 200, "cache_read": 59_000, "output": 100}
        )
        == 1_100
    )
    assert (
        task_turn_tokens(
            "opencode", {"input": 800, "cache_creation": 200, "cache_read": 59_000, "output": 100}
        )
        == 1_100
    )
    assert task_turn_tokens("gemini", {"input": 900, "output": 100}) is None
    assert task_turn_tokens("kimi", {"input": 900, "output": 100}) is None


def test_provider_discipline_gaps_are_visible_in_capability_matrix():
    from agent_takkub.provider_spec import PROVIDER_REGISTRY, capability_matrix

    assert capability_matrix(PROVIDER_REGISTRY["codex"])["lead_context_recovery"] == "supported"
    assert capability_matrix(PROVIDER_REGISTRY["gemini"])["lead_context_recovery"] == "unsupported"
    assert capability_matrix(PROVIDER_REGISTRY["cursor"])["task_token_budget"] == "unsupported"


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


def test_cockpit_confirmation_slot_returns_the_users_button_choice(monkeypatch):
    from agent_takkub import main_window

    class FakeMessageBox:
        class Icon:
            Question = 1
            Warning = 2

        class ButtonRole:
            AcceptRole = 1
            RejectRole = 2

        clicked = None

        def __init__(self, _parent):
            self.buttons = []

        def setIcon(self, _value):
            pass

        def setWindowTitle(self, _value):
            pass

        def setText(self, _value):
            pass

        def setInformativeText(self, value):
            self.task = value

        def setDetailedText(self, _value):
            pass

        def addButton(self, label, _role):
            button = object()
            self.buttons.append((label, button))
            return button

        def setDefaultButton(self, _button):
            pass

        def exec(self):
            self.clicked = self.buttons[0][1] if self.accept else self.buttons[1][1]

        def clickedButton(self):
            return self.clicked

    accepted = []
    for choice in (True, False):
        FakeMessageBox.accept = choice
        monkeypatch.setattr(main_window, "QMessageBox", FakeMessageBox)
        request = {
            "kind": "spec",
            "role": "backend",
            "project": "test-project",
            "task": "Set tax to 7%",
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
        session=session,
        current_usage=lambda: {"total": 1_000, "input": 900, "output": 100, "model": "test"},
    )
    state = SimpleNamespace(
        last_assigned_task="ongoing",
        task_budget_halted=False,
        assign_ts=100.0,
        last_assigned_scope="tiny",
        task_token_total=0,
        task_last_usage_marker=(999, 900, 100, "test", "older-turn"),
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
        json.dumps({"limits": {"tiny": {"minutes": 30, "tokens": 1_000}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TAKKUB_EVENTS_LOG_SYNC", "1")
    orch_mod.Orchestrator._check_task_work_limits(orch, now=101)
    assert not state.task_budget_halted
    orch_mod.Orchestrator._check_task_work_limits(orch, now=102)
    orch_mod.Orchestrator._check_task_work_limits(orch, now=103)
    rows = [
        json.loads(line)
        for line in (tmp_path / "events.log").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["event"] == "task_work_budget_exceeded"
    assert rows[0]["provider"] == "codex"
    assert rows[0]["reason"] == "tokens"
    assert writes == ["\x03", "\x03"]
    assert state.task_budget_halted
    assert len(notices) == 1
    assert len(rows) == 1


def test_task_budget_does_not_sum_repeated_context_snapshots(monkeypatch, tmp_path):
    from agent_takkub import config
    from agent_takkub import orchestrator as orch_mod

    snapshot = {
        "total": 60_000,
        "input": 60_000,
        "cache_read": 59_000,
        "output": 100,
        "model": "test",
    }
    pane = SimpleNamespace(provider="codex", session=None, current_usage=lambda: snapshot)
    state = SimpleNamespace(
        last_assigned_task="ongoing",
        task_budget_halted=False,
        assign_ts=100.0,
        last_assigned_scope="tiny",
        task_token_total=0,
        # Baseline is the resumed session's latest usage at assignment.
        task_last_usage_marker=(60_000, 60_000, 100, "test", "baseline"),
        last_turn_end_ts=99,
    )
    orch = SimpleNamespace(
        _panes_by_project={"proj": {"backend": pane}},
        _ps=lambda _key: state,
        _notify_lead=lambda *args, **kwargs: None,
        taskBudgetNotice=SimpleNamespace(emit=lambda *args: None),
    )
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    (tmp_path / "work-discipline.json").write_text(
        json.dumps({"limits": {"tiny": {"minutes": 30, "tokens": 5_000}}}),
        encoding="utf-8",
    )

    # A changed turn-end timestamp alone does not count this same snapshot.
    snapshot["task_turn_id"] = "baseline"
    state.last_turn_end_ts = 100
    orch_mod.Orchestrator._check_task_work_limits(orch, now=101)
    assert state.task_token_total == 0  # timestamp changes alone are not a new turn
    # Four distinct turns keep the same 60k context snapshot. Only fresh input
    # and output are counted; the 59k cached context is replayed, not new work.
    for turn_end in range(100, 104):
        snapshot["task_turn_id"] = f"turn-{turn_end}"
        state.last_turn_end_ts = turn_end
        orch_mod.Orchestrator._check_task_work_limits(orch, now=turn_end + 1)

    assert state.task_token_total == 4_400
    assert not state.task_budget_halted


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
