"""Regression tests for the 2026-11-15 issue batch (#1007–#1022).

Each test names the field incident it locks in. The quota-detector half
(#1008/#1009/#1010/#1020), Lead restoration (#1012/#1022) and compose
auto-down (#1015) live next to the code they exercise — `test_limit_autoresume`,
`test_quota_detection`, `test_compose_autodown_984`.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import browser_chrome, cli, lead_inbox, role_messages, venv_integrity
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator, _exit_key
from agent_takkub.pipeline_executor import PipelineMixin
from agent_takkub.spawn_engine import PaneState

PROJECT = "batch1007"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator, "_resolve_project", staticmethod(lambda project: project or PROJECT)
    )
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _live_pane(state: str = "working", *, at_prompt: bool = True) -> MagicMock:
    pane = MagicMock()
    pane.state = state
    pane.session.is_alive = True
    pane.session.is_at_ready_prompt.return_value = at_prompt
    pane.session.is_at_ready_prompt_cached.return_value = at_prompt
    pane.session.has_background_work.return_value = False
    pane._session_cwd = "C:/work"
    pane._transcript_path = None
    pane.set_state.side_effect = lambda s, **kw: setattr(pane, "state", s)
    return pane


# ── #1021 / #1016 / #1013: status-only notices never reach an AI pane ────────


def _notify_host(panes: dict | None = None):
    host = SimpleNamespace(
        _post_to_qt_thread_if_needed=lambda fn: False,
        statusChanged=MagicMock(),
        _redact_forwarded_text=lambda body, *a, **k: body,
        _project_panes=MagicMock(return_value=panes or {}),
        _pending_done_notices={},
        _save_pending_done_notices=MagicMock(),
    )
    return host


@pytest.mark.parametrize(
    "kind",
    [
        "quota-hit",  # "[system] backend (claude) hit quota — resets in 56m"
        "quota-rerouted",  # "[auto-resume] claude ชนโควตา → backend ย้ายไป codex"
        "quota-reset",
        "quota-false-positive",
        "limit-parked",
        "limit-resumed",
        "rate-limit-reset",
        "skill-learning",  # "🧠 Skill Learning (backend): patch …" — 5 empty Lead turns in 1.5 h
        "subprocess-cleanup",  # "🧹 [devops closing] Cleaned up 2 child subprocess(es)"
        "assign-provider-switch",  # the notice #1013 repeated 11 times
    ],
)
def test_status_only_notice_is_logged_but_never_queued(kind: str) -> None:
    host = _notify_host()
    with patch.object(lead_inbox, "_log_event") as log:
        lead_inbox.LeadInboxMixin._notify_lead(host, "proj", "status text", kind=kind)
    assert log.call_args.args[0] == "lead_notice" and log.call_args.kwargs["kind"] == kind
    host._project_panes.assert_not_called()  # never even looked for a Lead pane
    assert host._pending_done_notices == {}  # nor parked it for a later replay
    host._save_pending_done_notices.assert_not_called()
    host.statusChanged.emit.assert_called_once()


@pytest.mark.parametrize("kind", ["quota-reroute-failed", "limit-autoresume-stopped", "failed"])
def test_a_failed_recovery_still_reaches_the_lead(kind: str) -> None:
    host = _notify_host()  # no live Lead → durable inbox
    with patch.object(lead_inbox, "_log_event"):
        lead_inbox.LeadInboxMixin._notify_lead(host, "proj", "needs a decision", kind=kind)
    assert [i["body"] for i in host._pending_done_notices["proj"]] == ["needs a decision"]


# ── #1013: assign to a live Lead never loops close→re-dispatch ───────────────


def test_assign_to_live_lead_with_another_provider_delivers_once_without_closing(
    orch, tmp_path
) -> None:
    """The Lead is close-protected: the idle provider-switch branch closed
    nothing, re-dispatched itself 2 s later, found the same live Lead and
    announced the same switch again — 11 identical notices in 20 s."""
    lead = _live_pane("active")
    orch._panes_by_project.setdefault(PROJECT, {})["lead"] = lead
    orch._ps(_exit_key(PROJECT, "lead")).provider_override = "gemini"
    with (
        patch.object(orch, "spawn", return_value=(True, "lead already running")),
        patch.object(orch, "_send_when_ready") as send,
        patch.object(orch, "_notify_lead") as notify,
        patch.object(orch, "close") as close,
        patch.object(orch_mod.QTimer, "singleShot") as timer,
    ):
        ok, msg = orch._assign_dispatch(
            "lead", str(tmp_path), "kickoff", project=PROJECT, provider="claude"
        )
    assert ok, msg
    assert "respawning" not in msg
    close.assert_not_called()
    timer.assert_not_called()  # no delayed re-dispatch → no loop
    assert "assign-provider-switch" not in [c.kwargs.get("kind") for c in notify.call_args_list]
    send.assert_called_once()


# ── #1014: a task that forbids committing is never committed for ─────────────


@pytest.mark.parametrize(
    "task",
    [
        "implement OAuth. ห้าม commit/push/deploy เด็ดขาด",
        "ห้าม push/commit จนกว่า Lead จะตรวจ",
        "แก้ตามนี้ แล้วไม่ต้อง commit",
        "อย่า commit นะ",
        "Do NOT commit or push — leave the changes in the working tree.",
        "refactor only, no commits",
        "don't commit; I will review the diff first",
        "never commit to this branch",
        "run with --no-commit semantics",
    ],
)
def test_task_forbids_commit_recognised(task: str) -> None:
    assert Orchestrator._task_forbids_commit(task) is True


@pytest.mark.parametrize(
    "task",
    [
        "implement OAuth แล้ว commit เมื่อเสร็จ",
        "commit เป็นก้อนเล็ก ๆ ห้าม push",  # a push ban is not a commit ban
        "fix the bug that says 'nothing to commit'",
        "review the last commit and summarise it",
        "",
        None,
    ],
)
def test_ordinary_task_is_not_read_as_a_commit_ban(task) -> None:
    assert Orchestrator._task_forbids_commit(task) is False


class _FakeMgr:
    def __init__(self, dirty: bool = True) -> None:
        self.dirty = dirty
        self.snapshots: list[str] = []
        self.dirty_snapshots: list[str] = []

    def real_dirty(self, info) -> bool:
        return self.dirty

    def auto_commit_snapshot(self, info, role, summary="") -> bool:
        self.snapshots.append(role)
        return True

    def snapshot_dirty_worktree(self, info, role, reason):
        self.dirty_snapshots.append(reason)
        return True, None


def _wt(**extra) -> dict:
    return {"path": "C:/wt", "branch": "feature/mcp-oauth", "git_root": "C:/repo", **extra}


def test_done_snapshot_respects_no_commit_task() -> None:
    """PMS 2026-11-12: the task forbade commit/push/deploy; `takkub done`
    left the worktree clean at an auto WIP commit anyway."""
    mgr = _FakeMgr()
    info = SimpleNamespace(branch="feature/mcp-oauth")
    host = SimpleNamespace()
    with patch.object(orch_mod, "_log_event") as log:
        made = Orchestrator._snapshot_worktree_before_report(
            host, "pms", "backend", _wt(no_commit=True), info, mgr, "done note"
        )
    assert made is False and mgr.snapshots == []
    assert log.call_args.args[0] == "worktree_auto_commit_skipped_task_restriction"
    # ...while a task without the ban still gets the #525 snapshot.
    assert Orchestrator._snapshot_worktree_before_report(
        host, "pms", "backend", _wt(), info, mgr, "done note"
    )
    assert mgr.snapshots == ["backend"]


def test_close_snapshot_respects_no_commit_task(monkeypatch) -> None:
    """The pane's auto-close 2.5 s after done runs its own safety-net
    snapshot — it must honour the same ban."""
    from agent_takkub import worktree_manager as wm

    mgr = _FakeMgr()
    monkeypatch.setattr(wm, "WorktreeManager", lambda: mgr)
    host = SimpleNamespace(_notify_lead=MagicMock())
    with patch.object(orch_mod, "_log_event"):
        Orchestrator._snapshot_dirty_worktree_if_needed(
            host, "pms", "backend", _wt(no_commit=True), "close"
        )
        assert mgr.dirty_snapshots == []
        Orchestrator._snapshot_dirty_worktree_if_needed(host, "pms", "backend", _wt(), "close")
    assert mgr.dirty_snapshots == ["close"]


def test_assign_stamps_and_clears_the_no_commit_flag(orch, tmp_path) -> None:
    """The ban rides in the worktree dict (done, close and give-up all read
    it there) and is re-evaluated for each task handed to that worktree."""
    worktree = _wt(path=str(tmp_path))
    with (
        patch.object(orch, "spawn", return_value=(True, "backend spawned")),
        patch.object(orch, "_send_when_ready"),
        patch.object(orch, "_notify_lead"),
    ):
        orch._assign_dispatch(
            "backend", str(tmp_path), "แก้ X ห้าม commit", project=PROJECT, worktree=worktree
        )
        assert worktree.get("no_commit") is True
        orch._assign_dispatch(
            "backend", str(tmp_path), "แก้ Y แล้ว commit", project=PROJECT, worktree=worktree
        )
    assert "no_commit" not in worktree


def test_digest_is_recomputed_after_the_done_snapshot() -> None:
    """The digest said "0 commit ahead / 10 uncommitted" about a worktree
    that was clean at an auto-commit: facts were read BEFORE the snapshot
    (and could even come from an earlier off-thread gather)."""
    from agent_takkub import worktree_manager as wm

    state = {"commits": 0, "dirty": True}

    class _Mgr:
        def commit_count(self, info):
            return state["commits"]

        def real_dirty(self, info):
            return state["dirty"]

        def real_uncommitted_count(self, info):
            return 10 if state["dirty"] else 0

        def crlf_phantom(self, info):
            return False

        def merge_conflicts_with_base(self, git_root, ref):
            return []

        def merge_ref(self, info):
            return info.branch

        def diffstat(self, info):
            return " src/a.py | 2 +-\n 1 file changed"

        def remote_branch_exists(self, git_root, branch):
            return False

    def snapshot(info, mgr) -> bool:
        state.update(commits=1, dirty=False)
        return True

    stale = {"kind": "worktree", "commits": 0, "dirty": True, "uncommitted": 10}
    with patch.object(wm, "WorktreeManager", _Mgr):
        facts, precomputed = Orchestrator._compute_digest_facts(
            "backend",
            None,
            "done",
            None,
            _wt(),
            "C:/wt",
            None,
            None,
            None,
            git_facts=stale,
            snapshot=snapshot,
        )
    assert precomputed["commits"] == 1 and precomputed["dirty"] is False
    assert facts.commits_ahead == 1 and facts.uncommitted == 0


# ── #1011: browsers a pane's task left open are closed on done ───────────────


@pytest.mark.parametrize(
    "name",
    [
        "chrome.exe",
        "chrome",
        "chrome-headless-shell.exe",
        "Chromium",
        "Google Chrome Helper (Renderer)",
        "msedge.exe",
        "firefox",
        "headless_shell",
        "Playwright",
    ],
)
def test_browser_process_names(name: str) -> None:
    assert browser_chrome.is_browser_process_name(name) is True


@pytest.mark.parametrize("name", ["node.exe", "python.exe", "pwsh.exe", "docker", "git", "zsh"])
def test_non_browser_process_names(name: str) -> None:
    assert browser_chrome.is_browser_process_name(name) is False


class _Proc:
    def __init__(self, pid: int, name: str, ppid: int) -> None:
        self.pid, self._name, self._ppid = pid, name, ppid
        self.terminated = self.killed = False

    def name(self) -> str:
        return self._name

    def ppid(self) -> int:
        return self._ppid

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_close_browser_descendants_touches_only_this_panes_browsers(monkeypatch) -> None:
    """Ownership is the process tree: the MCP server (node), the shell and
    the CLI itself survive; only the browser roots under the pane are asked
    to exit, and a helper that outlives the grace is killed."""
    import psutil

    node = _Proc(11, "node.exe", 10)
    chrome = _Proc(12, "chrome.exe", 11)
    renderer = _Proc(13, "chrome.exe", 12)
    shell = _Proc(14, "pwsh.exe", 10)
    tree = [node, chrome, renderer, shell]
    monkeypatch.setattr(
        psutil, "Process", lambda pid: SimpleNamespace(children=lambda recursive: tree)
    )
    monkeypatch.setattr(psutil, "wait_procs", lambda procs, timeout: ([chrome], [renderer]))

    assert browser_chrome.close_browser_descendants(10) == ["chrome.exe"]
    assert chrome.terminated and not renderer.terminated  # only the root is asked
    assert renderer.killed  # the straggler after the grace
    assert not (node.terminated or node.killed or shell.terminated or shell.killed)


def test_close_browser_descendants_with_no_browser_is_a_noop(monkeypatch) -> None:
    import psutil

    tree = [_Proc(11, "node.exe", 10)]
    monkeypatch.setattr(
        psutil, "Process", lambda pid: SimpleNamespace(children=lambda recursive: tree)
    )
    monkeypatch.setattr(psutil, "wait_procs", lambda *a, **k: pytest.fail("nothing to wait for"))
    assert browser_chrome.close_browser_descendants(10) == []


def test_done_pane_no_longer_holds_the_shared_chrome(monkeypatch) -> None:
    """Panes outlive `done` (pane reuse) — an idle done qa pane pinned the
    cockpit's Chrome (~500 MB) for as long as it stayed open."""
    monkeypatch.setattr(browser_chrome, "should_manage_native_chrome", lambda role, shard: True)
    pane = SimpleNamespace(session=object(), state="done")
    host = SimpleNamespace(_panes_by_project={"p": {"qa": pane}})
    assert Orchestrator._native_chrome_in_use(host) is False
    pane.state = "working"
    assert Orchestrator._native_chrome_in_use(host) is True


# ── #1017: QA planner → fan-out keeps its plan and its backlog card ──────────


def test_planner_done_does_not_close_the_verification_card() -> None:
    """Planner `done` = "the plan is written"; every runtime test was still
    NOT RUN when the card was closed, and the follow-up `--backlog` assign
    then failed with a bare "queued 0/2 shards"."""
    from agent_takkub import backlog

    ps = PaneState(backlog_id="b34f274f")
    ps.plan_fanout = {"shards": 2, "task": "verify", "plan_file": "plan.json"}
    with patch.object(backlog, "on_ledger_done") as moved:
        line = Orchestrator._done_move_backlog_card(
            SimpleNamespace(), ps, False, False, "qa", "t1", "chat-p2p"
        )
        assert line is None
        moved.assert_not_called()
        # an ordinary done still moves its card
        ps.plan_fanout = None
        moved.return_value = {"status": "review", "id": "b34f274f"}
        line = Orchestrator._done_move_backlog_card(
            SimpleNamespace(), ps, False, False, "qa", "t1", "chat-p2p"
        )
    assert moved.call_count == 1 and "b34f274f" in line


def test_plan_shard_runs_under_the_planners_card_and_reports_a_refusal() -> None:
    stash = MagicMock()
    host = SimpleNamespace(
        _stash_assign_backlog=stash,
        assign=MagicMock(return_value=(True, "task queued")),
        _notify_lead=MagicMock(),
    )
    PipelineMixin._assign_plan_shard(host, "p", "qa#1", "C:/w", "task", 2, None, "b34f274f")
    stash.assert_called_once_with("p", "qa#1", "b34f274f", "task")
    host._notify_lead.assert_not_called()

    host.assign.return_value = (False, "backlog b34f274f ปิดไปแล้ว")
    PipelineMixin._assign_plan_shard(host, "p", "qa#2", "C:/w", "task", 2, None, "b34f274f")
    body = host._notify_lead.call_args.args[1]
    assert "qa#2" in body and "backlog b34f274f ปิดไปแล้ว" in body


def test_plan_fanout_carries_the_backlog_card_to_every_shard(tmp_path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text('{"shards": [{"scope": "auth"}, {"scope": "chat"}]}', encoding="utf-8")
    calls: list[tuple] = []
    host = SimpleNamespace(
        _defer=lambda delay, fn: fn(),
        _stash_assign_backlog=lambda project, role, card, task: calls.append((role, card)),
        assign=MagicMock(return_value=(True, "ok")),
        _notify_lead=MagicMock(),
    )
    cfg = {"shards": 2, "task": "verify", "plan_file": str(plan), "backlog_id": "b34f274f"}
    PipelineMixin._fire_qa_plan_fanout(host, "chat-p2p", "qa", cfg)
    assert calls == [("qa#1", "b34f274f"), ("qa#2", "b34f274f")]
    assert [c.args[0] for c in host.assign.call_args_list] == ["qa#1", "qa#2"]


# ── #1019: the shim's interpreter is the venv's, never the base install's ────


def _fake_posix_venv(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "uv" / "python" / "cpython-3.11" / "bin"
    base.mkdir(parents=True)
    (base / "python").write_bytes(b"\x7fELF" + b"\0" * 2048)
    venv = tmp_path / "home" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = " + str(base), encoding="utf-8")
    try:
        os.symlink(base / "python", venv / "bin" / "python")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    return venv, base


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX venv's bin/python is a symlink")
def test_shim_python_is_the_venv_symlink_not_its_target(tmp_path, monkeypatch) -> None:
    """macOS 2026-11-15: ~/.agent-takkub/bin/takkub was rewritten with
    PY=<uv base>/bin/python and every `takkub` call died with "No module
    named agent_takkub" — one resolve() of the venv symlink did it."""
    from agent_takkub import config

    venv, base = _fake_posix_venv(tmp_path)
    monkeypatch.setattr(sys, "executable", str(venv / "bin" / "python"))
    py = venv_integrity.resolve_cockpit_python()
    assert py == (venv / "bin").resolve() / "python"
    assert base not in py.parents

    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(config, "DATA_HOME", tmp_path / "home")
    assert config._resolve_cli_bin_dir() == (venv / "bin").resolve()


def test_shim_python_is_this_venvs_console_interpreter() -> None:
    py = venv_integrity.resolve_cockpit_python()
    assert py.name == ("python.exe" if sys.platform == "win32" else "python")
    assert (py.parent.parent / "pyvenv.cfg").is_file() or py == Path(sys.executable)


def test_doctor_writes_the_same_interpreter_into_the_shims() -> None:
    """The two shim writers (spawn and doctor) disagreed: doctor derived the
    interpreter from CLI_BIN_DIR, which had followed the symlink."""
    src = Path(orch_mod.__file__).with_name("doctor.py").read_text(encoding="utf-8")
    assert "ensure_cli_shims(pane_bin, _resolve_cockpit_python())" in src
    assert "ensure_cli_shims(pane_bin, CLI_BIN_DIR" not in src


# ── #1007: `takkub send` reports a real receipt, not "queued" ────────────────


def _sent(msg_id: str = "0123456789ab") -> dict:
    return {"ok": True, "msg": f"sent to devops (id {msg_id}) — เขียนเข้า session แล้ว"}


def test_send_waits_for_the_receipt_and_reports_delivered(monkeypatch) -> None:
    states = iter([{"state": "sent"}, {"state": "sent"}, {"state": "delivered"}])
    monkeypatch.setattr(role_messages, "find_record", lambda runtime, msg_id: next(states))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    out = cli._await_send_receipt(_sent(), "devops", 8.0)
    assert out["ok"] is True
    assert out["msg"].startswith("delivered to devops (id 0123456789ab)")


def test_send_reports_a_failed_delivery_as_a_failure(monkeypatch) -> None:
    rec = {"state": "abandoned", "abandoned_reason": "writer_rejected"}
    monkeypatch.setattr(role_messages, "find_record", lambda runtime, msg_id: rec)
    out = cli._await_send_receipt(_sent(), "devops", 8.0)
    assert out["ok"] is False and "writer_rejected" in out["msg"] and out["exit_code"] == 1


def test_send_never_claims_delivery_it_did_not_see(monkeypatch) -> None:
    """The old reply was "queued … ยืนยันด้วย takkub messages" — Lead had to
    poll to learn anything. An unconfirmed send now says so, in those words."""
    monkeypatch.setattr(role_messages, "find_record", lambda runtime, msg_id: {"state": "sent"})
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    clock = iter([0.0, 0.0, 5.0, 9.0, 9.0, 9.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
    out = cli._await_send_receipt(_sent(), "devops", 8.0)
    assert out["ok"] is True
    assert "delivered" not in out["msg"] and "ยังไม่ยืนยัน" in out["msg"]
    assert "queued" not in out["msg"]


def test_send_receipt_wait_is_skipped_when_not_applicable(monkeypatch) -> None:
    monkeypatch.setattr(
        role_messages, "find_record", lambda *a: pytest.fail("must not read the log")
    )
    failed = {"ok": False, "msg": "backend is not running (spawn it first)"}
    assert cli._await_send_receipt(failed, "backend", 8.0) is failed
    no_wait = _sent()
    assert cli._await_send_receipt(no_wait, "devops", 0.0) is no_wait
    no_pane = {"ok": True, "msg": "'qa' has no pane open right now — message queued"}
    assert cli._await_send_receipt(no_pane, "qa", 8.0) is no_pane


def test_find_record_reads_the_log_across_projects(tmp_path) -> None:
    msg_id = role_messages.append(
        tmp_path, "chat-p2p", to_role="devops", from_role="lead", body="hi", generation=1
    )
    assert role_messages.find_record(tmp_path, msg_id)["state"] == "sent"
    role_messages.mark_delivered(tmp_path, "chat-p2p", msg_id)
    rec = role_messages.find_record(tmp_path, msg_id)
    assert rec["state"] == "delivered" and rec["delivered_ts"] > 0
    assert role_messages.find_record(tmp_path, "ffffffffffff") is None
    assert role_messages.find_record(tmp_path / "missing", msg_id) is None


@pytest.mark.parametrize(
    ("field", "expect"),
    [("limit_parked", "park รอโควตา"), ("quota_reroute_pending", "กำลังย้าย provider")],
)
def test_send_to_a_pane_that_cannot_read_is_refused_with_the_reason(orch, field, expect) -> None:
    """ "หาก pane ยังไม่พร้อม … ให้แจ้งว่า deliver ไม่ได้พร้อมเหตุผล ไม่ซ่อนเป็นคิว"."""
    pane = _live_pane("working", at_prompt=False)
    orch._panes_by_project.setdefault(PROJECT, {})["devops"] = pane
    ps = orch._ps(_exit_key(PROJECT, "devops"))
    setattr(ps, field, True)
    ps.rate_limited_until = time.time() + 3600
    ok, msg = orch.send("devops", "new instruction", from_role="lead", project=PROJECT)
    assert ok is False and "ส่งไม่ถึง" in msg and expect in msg
    pane.session.write.assert_not_called()


# ── #1016: progress updates are merged instead of one Lead turn each ─────────


def test_progress_updates_inside_the_window_are_merged(orch) -> None:
    """devops reported progress 8 times in 7 minutes — 8 Lead turns, none
    needing a decision."""
    orch._panes_by_project.setdefault(PROJECT, {})["devops"] = _live_pane("working")
    timers: list = []
    with (
        patch.object(orch, "_notify_lead") as notify,
        patch.object(orch_mod.QTimer, "singleShot", side_effect=lambda ms, fn: timers.append(fn)),
    ):
        assert orch.progress("devops", "building image", project=PROJECT)[0]
        ok, msg = orch.progress("devops", "pushing layer 3/9", project=PROJECT)
        assert ok and "รวม" in msg
        orch.progress("devops", "pushing layer 8/9", project=PROJECT)
        assert notify.call_count == 1  # only the first one went out
        assert len(timers) == 1  # one flush armed for the window, not one per update
        timers[0]()
        assert notify.call_count == 2
        body = notify.call_args.args[1]
    assert "pushing layer 8/9" in body and "+1" in body and "layer 3/9" not in body


def test_progress_that_asks_for_something_is_never_held(orch) -> None:
    orch._panes_by_project.setdefault(PROJECT, {})["devops"] = _live_pane("working")
    with (
        patch.object(orch, "_notify_lead") as notify,
        patch.object(orch_mod.QTimer, "singleShot"),
    ):
        orch.progress("devops", "building image", project=PROJECT)
        orch.progress("devops", "blocked: registry returns 401, need a token", project=PROJECT)
    assert notify.call_count == 2


def test_held_progress_is_dropped_once_the_task_is_reported(orch) -> None:
    orch._panes_by_project.setdefault(PROJECT, {})["devops"] = _live_pane("working")
    timers: list = []
    with (
        patch.object(orch, "_notify_lead") as notify,
        patch.object(orch_mod.QTimer, "singleShot", side_effect=lambda ms, fn: timers.append(fn)),
    ):
        orch.progress("devops", "step 1", project=PROJECT)
        orch.progress("devops", "step 2", project=PROJECT)
        orch._pane_state.pop(_exit_key(PROJECT, "devops"))  # done() retired the task state
        timers[0]()
    assert notify.call_count == 1
