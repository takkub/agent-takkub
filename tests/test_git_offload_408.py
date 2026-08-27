"""#408 — git subprocesses that used to run inline on the Qt main thread
(`done()`'s digest/merge-proposal reads, `assign --isolation worktree`'s
`git worktree add`) now run on a worker thread in cli_server and hand a
plain result back into the orchestrator.

Covers: the pure `collect_done_git_facts` shape, `_compute_digest_facts`
consuming it WITHOUT touching git, `_assign_with_worktree(prepared=...)`
skipping `create`, the cheap main-thread input helpers, and cli_server's
worker→Qt-thread hop end to end (done + worktree assign)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import worktree_manager as wm_mod
from agent_takkub.cli_server import CliServer
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.worktree_manager import GitResult, WorktreeInfo, WorktreeManager

from ._qt_timer_leak_guard import stop_timers_after


@pytest.fixture(autouse=True)
def _stop_cli_server_timers(monkeypatch):
    finalize = stop_timers_after(monkeypatch, CliServer, "shutdown_timers")
    yield
    finalize()


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _pump(qapp: QCoreApplication, until, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not until() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert until(), "condition not reached within timeout"


def _info() -> WorktreeInfo:
    return WorktreeInfo(path="/wt/x", branch="wt/x-1", base_sha="b", git_root="/repo")


# ── collect_done_git_facts (pure) ─────────────────────────────────────────


def _scripted_runner(script: dict[str, GitResult]):
    calls: list[str] = []

    def run(args: list[str], cwd):
        key = " ".join(args)
        calls.append(key)
        for k, v in script.items():
            if k in key:
                return v
        return GitResult(1, "", "no script")

    return run, calls


def test_collect_done_git_facts_worktree_shape() -> None:
    run, _calls = _scripted_runner(
        {
            "rev-list --count": GitResult(0, "2\n", ""),
            "status --porcelain": GitResult(0, " M a.py\n", ""),
            "merge-base": GitResult(0, "abc\n", ""),
            "merge-tree": GitResult(0, "clean tree", ""),
            "diff --stat": GitResult(0, " a.py | 1 +\n", ""),
        }
    )
    mgr = WorktreeManager(runner=run)

    facts = mgr.collect_done_git_facts(_info().as_dict(), None, None, None)

    assert facts["kind"] == "worktree"
    assert facts["commits"] == 2
    assert facts["dirty"] is True
    assert facts["uncommitted"] == 1
    assert facts["merge_conflicts"] is False
    assert facts["diffstat"].strip() == "a.py | 1 +"


def test_collect_done_git_facts_shared_shape_skips_diff_when_status_fails() -> None:
    run, calls = _scripted_runner(
        {
            "rev-parse --abbrev-ref": GitResult(0, "main\n", ""),
            "rev-list --count": GitResult(0, "1\n", ""),
            "status --porcelain": GitResult(128, "", "fatal"),
        }
    )
    mgr = WorktreeManager(runner=run)

    facts = mgr.collect_done_git_facts(None, "/repo", "base", "/repo")

    assert facts["kind"] == "shared"
    assert facts["branch"] == "main"
    assert facts["commits_ahead"] == 1
    assert facts["porcelain"] is None
    assert facts["diffstat"] == ""
    assert not any("diff --stat" in c for c in calls)


def test_collect_done_git_facts_rediscovers_lost_worktree_bookkeeping() -> None:
    """#410: PaneState.worktree can go missing (most commonly a cockpit
    restart between `assign(isolation="worktree")` and this pane's `done()`)
    even though its cwd really is sitting in an isolated `wt/*` checkout with
    real commits on it. Without reconstruction this would fall through to
    the shared-tree branch, which has no baseline for the cwd at all and
    reports "ตรวจไม่ได้" instead of proposing the merge."""
    run, _calls = _scripted_runner(
        {
            "rev-parse --abbrev-ref HEAD": GitResult(0, "wt/backend-1\n", ""),
            "rev-parse --show-toplevel": GitResult(0, "/wt/backend-1\n", ""),
            "worktree list --porcelain": GitResult(
                0,
                "worktree /repo\nHEAD aaaa\nbranch refs/heads/main\n\n"
                "worktree /wt/backend-1\nHEAD bbbb\nbranch refs/heads/wt/backend-1\n",
                "",
            ),
            "merge-base": GitResult(0, "deadbeef\n", ""),
            "rev-list --count": GitResult(0, "2\n", ""),
            "status --porcelain": GitResult(0, "", ""),
            "merge-tree": GitResult(0, "clean tree", ""),
            "diff --stat": GitResult(0, " a.py | 1 +\n", ""),
        }
    )
    mgr = WorktreeManager(runner=run)

    facts = mgr.collect_done_git_facts(None, "/wt/backend-1", None, None)

    assert facts["kind"] == "worktree"
    assert facts["commits"] == 2
    assert facts["dirty"] is False
    assert facts["merge_conflicts"] is False
    assert facts["rediscovered_worktree"] == {
        "path": "/wt/backend-1",
        "branch": "wt/backend-1",
        "base_sha": "deadbeef",
        "git_root": "/repo",
        "links": [],
        "port": 0,
    }


def test_collect_done_git_facts_no_cwd_never_attempts_rediscovery() -> None:
    # No pane_cwd at all (e.g. the pane vanished) — must not raise, and must
    # not fabricate a "rediscovered_worktree" key on the shared-tree shape.
    run, _calls = _scripted_runner({})
    mgr = WorktreeManager(runner=run)

    facts = mgr.collect_done_git_facts(None, None, None, None)

    assert facts["kind"] == "shared"
    assert "rediscovered_worktree" not in facts


# ── _compute_digest_facts consumes git_facts without git ──────────────────


class _NoGitMgr:
    def __getattr__(self, name):  # any git method call is a test failure
        raise AssertionError(f"git method {name}() must not run on the Qt thread")


def test_compute_digest_facts_uses_worktree_git_facts_without_git(monkeypatch) -> None:
    monkeypatch.setattr(wm_mod, "WorktreeManager", _NoGitMgr)
    facts, precomputed = Orchestrator._compute_digest_facts(
        "backend",
        "#1",
        "did it",
        None,
        _info().as_dict(),
        None,
        None,
        None,
        None,
        git_facts={
            "kind": "worktree",
            "commits": 3,
            "dirty": False,
            "uncommitted": 0,
            "merge_conflicts": False,
            "diffstat": " a.py | 2 ++",
        },
    )
    assert facts.commits_ahead == 3
    assert facts.merge_conflicts is False
    assert precomputed == {
        "commits": 3,
        "dirty": False,
        "uncommitted": 0,
        "merge_conflicts": False,
        "diffstat": " a.py | 2 ++",
    }


def test_compute_digest_facts_uses_shared_git_facts_without_git(monkeypatch) -> None:
    class _SnapshotOnlyMgr(_NoGitMgr):
        def dirty_snapshot(self, git_root, porcelain):
            return {"a.py": (" M", None, None)}

    monkeypatch.setattr(wm_mod, "WorktreeManager", _SnapshotOnlyMgr)
    facts, precomputed = Orchestrator._compute_digest_facts(
        "backend",
        None,
        "h",
        None,
        None,
        "/repo",
        "base",
        "/repo",
        {},
        git_facts={
            "kind": "shared",
            "branch": "main",
            "commits_ahead": 1,
            "porcelain": " M a.py\n",
            "diffstat": " a.py | 1 +",
        },
    )
    assert precomputed is None
    assert facts.branch == "main"
    assert facts.commits_ahead == 1
    assert facts.uncommitted == 1


# ── orchestrator input helpers + prepared worktree ───────────────────────


def test_assign_with_worktree_prepared_skips_inline_create(monkeypatch) -> None:
    class _MustNotCreate:
        def create(self, *a, **k):
            raise AssertionError("create() must not run inline when prepared is given")

    monkeypatch.setattr(wm_mod, "WorktreeManager", lambda *a, **k: _MustNotCreate())
    monkeypatch.setattr(
        wm_mod,
        "load_worktree_config",
        lambda root: (SimpleNamespace(post_create=[], base_port=0), ""),
    )
    fake = SimpleNamespace(
        _pane_state={},
        _resolve_project=lambda p: "proj",
        _notify_lead=MagicMock(),
        _assign_dispatch=MagicMock(return_value=(True, "ok")),
    )
    ok, _msg = Orchestrator._assign_with_worktree(
        fake, "frontend", "/repo", "task", False, False, 0, False, "proj", prepared=(_info(), "")
    )
    assert ok
    fake._assign_dispatch.assert_called_once()
    assert fake._assign_dispatch.call_args.kwargs["worktree"] == _info().as_dict()


def test_done_git_inputs_reads_state_only() -> None:
    ps = SimpleNamespace(worktree=None, assign_base_sha="b", assign_git_root="/repo")
    pane = SimpleNamespace(_session_cwd="/repo")
    fake = SimpleNamespace(
        _resolve_project=lambda p: "proj",
        _project_panes=lambda ns: {"backend": pane},
        _pane_state={"proj::backend": ps},
    )
    assert Orchestrator.done_git_inputs(fake, "backend") == {
        "worktree": None,
        "pane_cwd": "/repo",
        "assign_base_sha": "b",
        "assign_git_root": "/repo",
    }
    assert Orchestrator.done_git_inputs(fake, "nobody") is None


def test_worktree_assign_inputs_none_on_collision_or_no_cwd(monkeypatch) -> None:
    import agent_takkub.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "default_cwd_for_role", lambda role, project=None: None)
    fake = SimpleNamespace(
        _worktree_bare_role_collision=lambda r, p: "",
        _resolve_project=lambda p: "proj",
        _pane_state={},
    )
    assert Orchestrator.worktree_assign_inputs(fake, "frontend", None, None) is None
    got = Orchestrator.worktree_assign_inputs(fake, "frontend", "/repo", None)
    assert got is not None and got["base_cwd"] == "/repo" and got["role"] == "frontend"
    fake._worktree_bare_role_collision = lambda r, p: "collision"
    assert Orchestrator.worktree_assign_inputs(fake, "frontend", "/repo", None) is None


# ── cli_server: worker → Qt-thread hop ────────────────────────────────────


class _Sock:
    def __init__(self) -> None:
        self.written = b""

    def write(self, b) -> None:
        self.written += bytes(b)

    def flush(self) -> None:
        pass


_PANE_TOKEN = "pane-token-backend"


class _Orch:
    _lead_token = "tok"

    def __init__(self) -> None:
        self._pane_tokens = {_PANE_TOKEN: ("proj", "backend")}
        self.done_calls: list[tuple] = []
        self.assign_calls: list[dict] = []

    def done_git_inputs(self, role, project=None):
        return {
            "worktree": None,
            "pane_cwd": "/repo",
            "assign_base_sha": "b",
            "assign_git_root": "/repo",
        }

    def done(
        self, role, note="", project=None, failed=False, blocked=False, force=False, git_facts=None
    ):
        self.done_calls.append((role, git_facts))
        return True, "done ok"

    def worktree_assign_inputs(self, role, cwd, project):
        return {
            "base_cwd": "/repo",
            "project_ns": "proj",
            "role": role,
            "ts": 1,
            "exclude_ports": set(),
        }

    def assign(self, role, **kw):
        self.assign_calls.append({"role": role, **kw})
        return True, "ok"


def test_run_off_thread_delivers_result_on_qt_thread(qapp) -> None:
    import threading

    srv = CliServer(_Orch())
    seen: list = []
    worker_thread: list = []

    def work():
        worker_thread.append(threading.current_thread().name)
        return 42

    srv._run_off_thread(work, lambda r: seen.append((r, threading.current_thread().name)))
    _pump(qapp, lambda: bool(seen))
    assert seen[0][0] == 42
    assert seen[0][1] == threading.main_thread().name
    assert worker_thread[0] != threading.main_thread().name


def test_run_off_thread_exception_delivers_none(qapp) -> None:
    srv = CliServer(_Orch())
    seen: list = []

    def boom():
        raise RuntimeError("git exploded")

    srv._run_off_thread(boom, seen.append)
    _pump(qapp, lambda: bool(seen))
    assert seen == [None]


def test_done_collects_git_facts_off_thread_then_replies(qapp, monkeypatch) -> None:
    calls: list[str] = []

    class _FakeMgr:
        def collect_done_git_facts(self, **kw):
            import threading

            calls.append(threading.current_thread().name)
            return {"kind": "shared", "branch": "main"}

    monkeypatch.setattr(wm_mod, "WorktreeManager", _FakeMgr)
    orch = _Orch()
    srv = CliServer(orch)
    sock = _Sock()

    srv._dispatch(sock, {"cmd": "done", "from": "backend", "note": "x", "auth": _PANE_TOKEN})

    _pump(qapp, lambda: bool(orch.done_calls))
    assert orch.done_calls == [("backend", {"kind": "shared", "branch": "main"})]
    assert calls and calls[0] != __import__("threading").main_thread().name
    assert b"done ok" in sock.written


def test_worktree_assign_creates_off_thread_then_assigns_with_prepared(qapp, monkeypatch) -> None:
    info = _info()

    class _FakeMgr:
        def create(self, base_cwd, project_ns, role, ts, exclude_ports=frozenset()):
            return info, ""

    monkeypatch.setattr(wm_mod, "WorktreeManager", _FakeMgr)
    orch = _Orch()
    srv = CliServer(orch)
    monkeypatch.setattr(srv, "_next_spawn_delay_ms", lambda role, project: 0)
    sock = _Sock()

    srv._dispatch(
        sock,
        {
            "cmd": "assign",
            "from": "lead",
            "auth": "tok",
            "role": "frontend",
            "task": "build",
            "isolation": "worktree",
            "mode": "pane",
        },
    )

    _pump(qapp, lambda: bool(orch.assign_calls))
    call = orch.assign_calls[0]
    assert call["role"] == "frontend"
    assert call["isolation"] == "worktree"
    assert call["worktree_prepared"] == (info, "")
