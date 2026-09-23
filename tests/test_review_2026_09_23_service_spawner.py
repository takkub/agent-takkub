"""Regression tests for the 2026-09-23 system review, `service_spawner` group.

A registry row must name its process by ``(pid, create_time)``, never by the
bare PID: once the service dies the OS hands its PID to an unrelated process
(every reboot for sure, within minutes on Windows under pane churn), and
`service-stop` used to kill that stranger's whole tree while `service-list`
kept reporting the dead service as alive and `registered_pids` shielded the
stranger from the old-cockpit cleanup.

Hermetic: every registry lives under `tmp_path`; the only real processes are
`sys.executable` sleepers owned (and reaped) by the test.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from agent_takkub import service_spawner

_SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def stranger():
    """A live process that is NOT the service — the stale row will point at
    its PID, exactly the state PID reuse / a reboot leaves behind."""
    proc = subprocess.Popen(
        _SLEEPER,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _write_row(runtime: Path, project: str, **overrides) -> Path:
    row = {
        "name": "demo",
        "pid": 0,
        "cmd": ["cloudflared", "tunnel", "run"],
        "cwd": str(runtime),
        "log_path": str(runtime / "demo.log"),
        "started_ts": time.time(),
        "by_role": "devops",
        "project": project,
    }
    row.update(overrides)
    path = service_spawner.registry_path(runtime, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([row]), encoding="utf-8")
    return path


def _rows(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


class TestReusedPidIsNotTheService:
    def test_stop_never_kills_a_stranger_wearing_the_pid(self, tmp_path, stranger):
        born = psutil.Process(stranger.pid).create_time()
        # The row remembers a process created an hour before the stranger
        # (the service that died and gave its PID away).
        path = _write_row(tmp_path, "proj", pid=stranger.pid, create_time=born - 3600)

        ok, msg = service_spawner.stop(tmp_path, "proj", "demo")

        assert stranger.poll() is None, "service-stop killed an unrelated process"
        assert ok and "already gone" in msg
        assert _rows(path) == []

    def test_stop_never_kills_a_stranger_on_a_legacy_row(self, tmp_path, stranger):
        """Rows written by <= 2.1.32 carry no `create_time`; `started_ts`
        (when the real service was provably alive) must still rule out a
        process born after it."""
        born = psutil.Process(stranger.pid).create_time()
        path = _write_row(tmp_path, "proj", pid=stranger.pid, started_ts=born - 3600)

        ok, msg = service_spawner.stop(tmp_path, "proj", "demo")

        assert stranger.poll() is None, "service-stop killed an unrelated process"
        assert ok and "already gone" in msg
        assert _rows(path) == []

    def test_list_prunes_the_stale_row(self, tmp_path, stranger):
        born = psutil.Process(stranger.pid).create_time()
        path = _write_row(tmp_path, "proj", pid=stranger.pid, create_time=born - 3600)

        assert service_spawner.list_services(tmp_path, "proj") == []
        assert _rows(path) == []
        assert stranger.poll() is None

    def test_registered_pids_does_not_shield_the_stranger(self, tmp_path, stranger):
        born = psutil.Process(stranger.pid).create_time()
        _write_row(tmp_path, "proj", pid=stranger.pid, create_time=born - 3600)

        assert stranger.pid not in service_spawner.registered_pids(tmp_path)

    def test_spawn_prunes_stale_rows_instead_of_keeping_them(self, tmp_path, stranger):
        born = psutil.Process(stranger.pid).create_time()
        _write_row(tmp_path, "proj", pid=stranger.pid, create_time=born - 3600)

        rec = service_spawner.spawn(
            tmp_path, "proj", "real", _SLEEPER, cwd=str(tmp_path), by_role="devops"
        )
        live = psutil.Process(rec.pid)
        try:
            names = [r["name"] for r in _rows(service_spawner.registry_path(tmp_path, "proj"))]
            assert names == ["real"]
        finally:
            service_spawner.stop(tmp_path, "proj", "real")
        live.wait(timeout=10)
        assert stranger.poll() is None


class TestTheRealServiceStillWorks:
    """The identity check must never turn a genuine service into a 'stranger'
    on either OS — otherwise `service-stop` stops working at all."""

    def test_spawn_records_create_time_and_stop_matches_it(self, tmp_path):
        rec = service_spawner.spawn(
            tmp_path, "proj", "sleeper", _SLEEPER, cwd=str(tmp_path), by_role="devops"
        )
        live = psutil.Process(rec.pid)
        try:
            assert rec.create_time > 0
            assert abs(rec.create_time - live.create_time()) < 1.0
            row = _rows(service_spawner.registry_path(tmp_path, "proj"))[0]
            assert row["create_time"] == rec.create_time
            assert rec.pid in service_spawner.registered_pids(tmp_path)
            rows = service_spawner.list_services(tmp_path, "proj")
            assert [(r["name"], r["alive"]) for r in rows] == [("sleeper", True)]
        finally:
            ok, msg = service_spawner.stop(tmp_path, "proj", "sleeper")
        assert ok and msg.startswith("stopped service"), msg
        live.wait(timeout=10)
        assert service_spawner.list_services(tmp_path, "proj") == []

    def test_legacy_row_with_matching_started_ts_is_still_ours(self, tmp_path, stranger):
        """A <= 2.1.32 row for a service that is genuinely still running:
        recorded right after the spawn, so `started_ts` is later than the
        process's birth — it must stay listed and stoppable."""
        path = _write_row(tmp_path, "proj", pid=stranger.pid, started_ts=time.time())

        rows = service_spawner.list_services(tmp_path, "proj")
        assert [(r["name"], r["alive"]) for r in rows] == [("demo", True)]
        assert stranger.pid in service_spawner.registered_pids(tmp_path)

        ok, msg = service_spawner.stop(tmp_path, "proj", "demo")
        assert ok and msg.startswith("stopped service"), msg
        assert stranger.wait(timeout=10) is not None
        assert _rows(path) == []
