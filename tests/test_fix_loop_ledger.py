"""#762: the fix-loop ceiling is enforced on the real assign → `done --fail` →
reassign path, keyed by work identity + failure signature (not role/provider).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import backlog, fix_loop
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import LEAD, Orchestrator, PaneState, _exit_key

PROJ = "proj"
FAIL_A = "tests/test_login.py::test_login fails: KeyError 'token' in auth/session.py line 42"
FAIL_B = "database migration 0007 crashes: IntegrityError on users.email in db/migrate.py"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
    monkeypatch.setattr(orch_mod, "_resolve_vault_dir", lambda: None)
    monkeypatch.setattr(orch_mod, "active_project", lambda: (PROJ, {}))
    monkeypatch.setattr(fix_loop, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(backlog, "RUNTIME_DIR", tmp_path)
    with patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None):
        o = Orchestrator.__new__(Orchestrator)
        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
        o._pending_done_notices = {}
    monkeypatch.setattr(o, "_write_hot_md", MagicMock())
    o.notices = []
    o._notify_lead = lambda ns, notice, **kw: o.notices.append(notice)  # type: ignore[assignment]
    return o


def _pane(orch: Orchestrator, role: str) -> None:
    p = MagicMock()
    s = MagicMock()
    s.is_alive = True
    p.session = s
    p.state = "working"
    orch._panes_by_project.setdefault(PROJ, {})[role] = p


def _assign(orch: Orchestrator, role: str, card: str, task: str, scope: str = "normal") -> None:
    """What cli_server + assign() do for a new dispatch: link the card, then
    seed the pane's per-assignment state from it."""
    _pane(orch, role)
    orch._stash_assign_backlog(PROJ, role, card)
    orch._pane_state[_exit_key(PROJ, role)] = PaneState(
        last_assigned_task=task,
        last_assigned_scope=scope,
        backlog_id=orch._take_assign_backlog(PROJ, role),
        assign_ts=time.time(),
        task_id=f"t-{role}-{time.time_ns()}",
    )


def _fail(orch: Orchestrator, role: str, note: str) -> str:
    orch.notices.clear()
    _pane(orch, LEAD.name)
    orch.done(role, note=note, project=PROJ, failed=True)
    return orch.notices[0]


def test_two_retries_then_third_repeat_is_stopped_across_role_and_provider(orch) -> None:
    _assign(orch, "backend", "card1", "fix login #900")
    n1 = _fail(orch, "backend", FAIL_A)
    assert "1/2" in n1 and "CEILING" not in n1

    # Reassign to a DIFFERENT role: the ledger rides along in its brief.
    brief = fix_loop.brief_block(PROJ, fix_loop.identity_for("card1", "x"))
    assert "test_login" in brief and "backend" in brief

    _assign(orch, "frontend", "card1", "fix login again #900")
    n2 = _fail(orch, "frontend", FAIL_A + " (still)")
    assert "2/2" in n2 and "CEILING" not in n2
    assert "backend" in n2  # prior attempt evidence shown

    _assign(orch, "codex", "card1", "third try #900")
    n3 = _fail(orch, "codex", FAIL_A)
    assert "CEILING" in n3 and "ครั้งที่ 3" in n3
    assert "เสนอ fix loop" not in n3
    assert "backend" in n3 and "frontend" in n3


def test_unrelated_failure_is_not_blocked(orch) -> None:
    _assign(orch, "backend", "card1", "fix login #900")
    _fail(orch, "backend", FAIL_A)
    _assign(orch, "backend", "card1", "fix login #900")
    _fail(orch, "backend", FAIL_A)
    _assign(orch, "backend", "card2", "migrate #901")
    n = _fail(orch, "backend", FAIL_B)
    assert "CEILING" not in n and "1/2" in n
    # Same card, different failure signature → its own sequence too.
    _assign(orch, "backend", "card1", "fix login #900")
    n = _fail(orch, "backend", FAIL_B)
    assert "CEILING" not in n and "1/2" in n


def test_blocked_and_delivery_failures_do_not_count(orch) -> None:
    _assign(orch, "backend", "card1", "fix login #900")
    orch.done(
        "backend", note="ไม่มีรหัสผ่าน super admin เข้าไม่ได้", project=PROJ, failed=True, blocked=True
    )
    assert fix_loop.brief_block(PROJ, fix_loop.identity_for("card1", "")) == ""


def test_same_assignment_reported_twice_is_one_attempt(orch) -> None:
    _assign(orch, "backend", "card1", "fix login #900")
    _fail(orch, "backend", FAIL_A)
    ps = orch._pane_state.get(_exit_key(PROJ, "backend"))
    if ps is None:  # done() pops pane state; a duplicate report reuses the task id
        ps = PaneState(last_assigned_task="fix login #900", backlog_id="card1")
        orch._pane_state[_exit_key(PROJ, "backend")] = ps
    out = fix_loop.record_failure(
        PROJ, fix_loop.identity_for("card1", ""), FAIL_A, role="backend", attempt_token="dup"
    )
    out2 = fix_loop.record_failure(
        PROJ, fix_loop.identity_for("card1", ""), FAIL_A, role="backend", attempt_token="dup"
    )
    assert out2.attempt == out.attempt


def test_deep_scope_always_proposes_and_verify_pass_resets(orch) -> None:
    _assign(orch, "backend", "card1", "fix login #900", scope="deep")
    n = _fail(orch, "backend", FAIL_A)
    assert "deep" in n and "confirm" in n

    _assign(orch, "backend", "card1", "fix login #900")
    _fail(orch, "backend", FAIL_A)
    # qa verifies card1 OK → its sequences close; card2's stay.
    _assign(orch, "backend", "card2", "other #901")
    _fail(orch, "backend", FAIL_B)
    _assign(orch, "qa", "card1", "verify #900")
    orch.done("qa", note="all green", project=PROJ)
    assert fix_loop.brief_block(PROJ, fix_loop.identity_for("card1", "")) == ""
    assert fix_loop.brief_block(PROJ, fix_loop.identity_for("card2", "")) != ""


def _dispatch(orch: Orchestrator, role: str, task: str, **kw):
    """The real gate a `takkub assign` passes: backlog_for_assign, then the
    pane state assign() seeds from the card it stashed."""
    ok, note, cid = orch.backlog_for_assign(PROJ, role, task, **kw)
    if ok:
        _pane(orch, role)
        orch._pane_state[_exit_key(PROJ, role)] = PaneState(
            last_assigned_task=task,
            backlog_id=orch._take_assign_backlog(PROJ, role),
            assign_ts=time.time(),
            task_id=f"t-{role}-{time.time_ns()}",
        )
    return ok, note, cid


TASK = "fix the login KeyError token in auth/session.py so sessions persist"


def test_ceiling_refuses_assign_then_ack_passes_and_logs(orch, monkeypatch) -> None:
    events: list[tuple] = []
    monkeypatch.setattr(orch_mod, "_log_event", lambda name, **kw: events.append((name, kw)))
    ok, _, cid = _dispatch(orch, "backend", TASK)
    assert ok
    for role in ("backend", "frontend", "codex"):
        _fail(orch, role, FAIL_A)
        if role != "codex":
            assert _dispatch(
                orch, "frontend" if role == "backend" else "codex", TASK, backlog_id=cid
            )[0]
    ok, note, _ = orch.backlog_for_assign(PROJ, "backend", "another try", backlog_id=cid)
    assert not ok and "ceiling" in note and "--ack-ceiling" in note
    # Read-only diagnosis is never refused.
    assert orch.backlog_for_assign(PROJ, "reviewer", "find root cause", backlog_id=cid)[0]
    ok, _, _ = orch.backlog_for_assign(
        PROJ, "backend", "another try", backlog_id=cid, ack_ceiling="user said go"
    )
    assert ok
    assert any(n == "fix_loop_ceiling_acked" and kw["reason"] == "user said go" for n, kw in events)
    assert any(n == "fix_loop_assign_refused" for n, _ in events)


def test_reassign_without_backlog_flag_keeps_counting(orch) -> None:
    ok, _, cid = _dispatch(orch, "backend", TASK)
    _fail(orch, "backend", FAIL_A)
    # Lead re-assigns with reworded text and NO --backlog: same card, count continues.
    ok, note, cid2 = _dispatch(orch, "backend", TASK + " (retry)")
    assert ok and cid2 == cid and "นับความพยายามต่อ" in note
    n2 = _fail(orch, "backend", FAIL_A)
    assert "2/2" in n2
    _dispatch(orch, "backend", TASK + " again")
    assert "CEILING" in _fail(orch, "backend", FAIL_A)
    ok, note, _ = orch.backlog_for_assign(PROJ, "backend", TASK + " once more")
    assert not ok and "ceiling" in note


def test_unrelated_task_is_not_bound_or_refused(orch) -> None:
    ok, _, cid = _dispatch(orch, "backend", TASK)
    for _i in range(3):
        _fail(orch, "backend", FAIL_A)
        _dispatch(orch, "backend", TASK, backlog_id=cid)
    ok, note, cid2 = orch.backlog_for_assign(
        PROJ, "backend", "add pagination to the invoices listing endpoint"
    )
    assert ok and cid2 != cid and "นับความพยายามต่อ" not in note
