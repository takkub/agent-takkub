"""#658 batch 2: the psutil child-process scan behind the idle/stuck
watchdogs is served from a per-pid cache and refreshed off the Qt thread —
prod 2026-09-18 captured 101 main-thread stalls (0.9-3 s each) inside
`psutil.Process.parent()/status()` from these scans in one afternoon."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import bg_pool
from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "stallproj"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _session(pid: int = 4242) -> MagicMock:
    s = MagicMock()
    s._pid = pid
    s.is_alive = True
    return s


def _proc(name: str) -> MagicMock:
    p = MagicMock()
    p.name.return_value = name
    return p


class TestChildScanCache:
    def test_first_probe_is_inline_and_later_ones_are_served_from_cache(self, orch) -> None:
        sess = _session()
        with patch.object(orch, "_scan_child_procs", return_value=[_proc("vitest")]) as scan:
            first = orch._live_non_scaffolding_children(TEST_PROJECT, "qa", sess)
            second = orch._live_non_scaffolding_children(TEST_PROJECT, "qa", sess)
            third = orch._live_non_scaffolding_child_procs(TEST_PROJECT, "qa", sess)
        assert first == ["vitest"] and second == ["vitest"]
        assert [p.name() for p in third] == ["vitest"]
        scan.assert_called_once()

    def test_sync_probe_always_rescans(self, orch) -> None:
        sess = _session()
        with patch.object(orch, "_scan_child_procs", return_value=[]) as scan:
            orch._live_non_scaffolding_children(TEST_PROJECT, "qa", sess)
            orch._live_non_scaffolding_children(TEST_PROJECT, "qa", sess, sync=True)
            orch._warn_if_live_children(TEST_PROJECT, "qa", sess)
        assert scan.call_count == 3

    def test_stale_entry_is_served_and_refreshed_in_the_background(self, orch, monkeypatch):
        sess = _session()
        submitted: list = []
        monkeypatch.setattr(bg_pool, "submit", lambda fn, *a, **kw: submitted.append(fn))
        with patch.object(orch, "_scan_child_procs", return_value=[_proc("old")]) as scan:
            orch._live_non_scaffolding_children(TEST_PROJECT, "qa", sess)
            # age the cache entry past the TTL
            ts, procs = orch._child_scan_cache[sess._pid]
            orch._child_scan_cache[sess._pid] = (ts - orch._CHILD_SCAN_TTL_S - 1, procs)
            scan.return_value = [_proc("new")]
            stale = orch._live_non_scaffolding_children(TEST_PROJECT, "qa", sess)
            assert stale == ["old"]  # main thread never waited for the rescan
            assert len(submitted) == 1
            submitted[0]()  # the pool runs the refresh
            fresh = orch._live_non_scaffolding_children(TEST_PROJECT, "qa", sess)
        assert fresh == ["new"]
        assert sess._pid not in orch._child_scan_inflight

    def test_no_pid_short_circuits_without_scanning(self, orch) -> None:
        sess = MagicMock()
        sess._pid = None
        with patch.object(orch, "_scan_child_procs") as scan:
            assert orch._live_non_scaffolding_children(TEST_PROJECT, "qa", sess) == []
        scan.assert_not_called()


class TestBgPool:
    def test_submit_runs_on_a_worker_and_returns_a_future(self) -> None:
        fut = bg_pool.submit(lambda: (time.sleep(0.01), "ok")[1])
        assert fut.result(timeout=5) == "ok"

    def test_exceptions_stay_on_the_future(self) -> None:
        def boom() -> None:
            raise RuntimeError("x")

        fut = bg_pool.submit(boom)
        with pytest.raises(RuntimeError):
            fut.result(timeout=5)
