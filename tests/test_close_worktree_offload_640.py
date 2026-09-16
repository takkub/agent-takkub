"""#640: `close()`'s worktree git work runs off the Qt thread.

The watchdog recorded a 1.6s main-thread stall with
`worktree_manager.snapshot_dirty_worktree` → `git` on top of
`orchestrator.close`. The snapshot and the finalize now run on one worker
(order preserved), and `_notify_lead` re-posts itself to the Qt thread when
called from there. The rest of the suite keeps the synchronous timing via
`TAKKUB_CLOSE_WORKTREE_SYNC=1` (conftest); these tests turn it off.
"""

from __future__ import annotations

import threading
import time

from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator


def _pump(app, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_close_worktree_git_runs_off_the_qt_thread_in_order(_qt_session_app, monkeypatch):
    monkeypatch.delenv("TAKKUB_CLOSE_WORKTREE_SYNC", raising=False)
    orch = Orchestrator()
    orch.shutdown_timers()
    main_ident = threading.get_ident()
    calls: list[tuple[str, int]] = []

    monkeypatch.setattr(
        orch,
        "_snapshot_dirty_worktree_if_needed",
        lambda *a, **k: calls.append(("snapshot", threading.get_ident())),
    )
    monkeypatch.setattr(
        orch,
        "_finalize_worktree",
        lambda *a, **k: calls.append(("finalize", threading.get_ident())),
    )

    orch._close_worktree_git("p", "backend", {"path": "x"})

    assert _pump(QCoreApplication.instance(), lambda: len(calls) == 2)
    assert [name for name, _ in calls] == ["snapshot", "finalize"]
    assert all(ident != main_ident for _, ident in calls), "must not run on the Qt thread"


def test_sync_switch_keeps_the_old_inline_timing(_qt_session_app, monkeypatch):
    monkeypatch.setenv("TAKKUB_CLOSE_WORKTREE_SYNC", "1")
    orch = Orchestrator()
    orch.shutdown_timers()
    calls: list[str] = []
    monkeypatch.setattr(
        orch, "_snapshot_dirty_worktree_if_needed", lambda *a, **k: calls.append("snapshot")
    )
    monkeypatch.setattr(orch, "_finalize_worktree", lambda *a, **k: calls.append("finalize"))

    orch._close_worktree_git("p", "backend", {"path": "x"})

    assert calls == ["snapshot", "finalize"]


def test_notify_lead_from_a_worker_is_delivered_on_the_qt_thread(_qt_session_app, monkeypatch):
    """A notice raised by the off-thread finalize must be handled on the Qt
    thread — the inbox queues, timers and pane writes all live there."""
    orch = Orchestrator()
    orch.shutdown_timers()
    main_ident = threading.get_ident()
    seen: list[int] = []
    real_log = orch._redact_forwarded_text

    def _spy(body, *a, **k):
        seen.append(threading.get_ident())
        return real_log(body, *a, **k)

    monkeypatch.setattr(orch, "_redact_forwarded_text", _spy)

    t = threading.Thread(target=lambda: orch._notify_lead("p", "hello from a worker", kind="test"))
    t.start()
    t.join()
    assert seen == [], "the worker must not run the notice body itself"

    assert _pump(QCoreApplication.instance(), lambda: bool(seen))
    assert seen == [main_ident]


def test_ledger_index_is_written_off_thread_and_coalesced(tmp_path, monkeypatch):
    """#640: INDEX.md (derived view) is written by a background writer; a
    burst of regenerations collapses to the latest text."""
    from agent_takkub import task_ledger as tl

    monkeypatch.delenv("TAKKUB_LEDGER_INDEX_SYNC", raising=False)
    main_ident = threading.get_ident()
    writers: list[int] = []
    real = tl._atomic_write

    def _spy(path, text):
        writers.append(threading.get_ident())
        real(path, text)

    monkeypatch.setattr(tl, "_atomic_write", _spy)
    target = tmp_path / "INDEX.md"
    for i in range(20):
        tl._write_index_text(target, f"version {i}")
    assert tl.flush_index_writes(5.0)
    assert target.read_text(encoding="utf-8") == "version 19"
    assert writers and all(w != main_ident for w in writers)
    assert len(writers) < 20, "a burst must be coalesced, not written 20 times"


def test_orphan_worktree_sweep_no_longer_blocks_orchestrator_boot(_qt_session_app, monkeypatch):
    """#640 root cause of the ~30 s post-update boot: the orphan-worktree
    sweep classified every worktree dir with git, inline in
    Orchestrator.__init__ — 25 s measured on real data, before the window or
    the stall watchdog existed. It runs in the background now."""
    from agent_takkub import disk_usage

    monkeypatch.delenv("TAKKUB_BOOT_SWEEP_SYNC", raising=False)
    started = threading.Event()
    release = threading.Event()
    ran_on: list[int] = []

    def _slow_sweep(*_a, **_k):
        ran_on.append(threading.get_ident())
        started.set()
        release.wait(5)
        return 0

    monkeypatch.setattr(disk_usage, "prune_orphan_worktrees_boot", _slow_sweep)
    t0 = time.monotonic()
    orch = Orchestrator()
    built_in = time.monotonic() - t0
    orch.shutdown_timers()
    try:
        assert started.wait(5), "the sweep must still run"
        assert built_in < 4, f"construction waited on the sweep ({built_in:.1f}s)"
        assert ran_on[0] != threading.get_ident()
    finally:
        release.set()
