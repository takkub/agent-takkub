"""Tests for #141 — spawn-queue wedge diagnostics.

Covers the three layers: the read-only poller (spawn_queue_health.py), the
cli_server.py TCP endpoint that exposes it, and the `takkub doctor --live`
interpretation (doctor.py's check_spawn_queue_live).
"""

from __future__ import annotations

import inspect
import json

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.cli_server import CliServer
from agent_takkub.doctor import Status, check_spawn_queue_live
from agent_takkub.spawn_queue_health import SpawnQueueHealthMonitor


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


class _FakeOrch:
    def __init__(self) -> None:
        self._spawn_in_progress = False
        self._spawn_queue: list = []


def _fake_clock(monkeypatch: pytest.MonkeyPatch, start: float = 1000.0):
    """Replace time.monotonic() in spawn_queue_health.py with a controllable clock."""
    state = {"t": start}

    def _now() -> float:
        return state["t"]

    monkeypatch.setattr("agent_takkub.spawn_queue_health.time.monotonic", _now)
    return state


# ---------------------------------------------------------------------------
# SpawnQueueHealthMonitor — read-only poller
# ---------------------------------------------------------------------------


class TestSpawnQueueHealthMonitor:
    def test_idle_snapshot_is_empty(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        mon = SpawnQueueHealthMonitor(orch, interval_ms=999_999)
        snap = mon.snapshot()
        assert snap.queue_depth == 0
        assert snap.spawn_in_progress is False
        assert snap.spawn_in_progress_age_s is None
        assert snap.oldest_queued_age_s is None

    def test_spawn_in_progress_age_tracks_since_first_observed(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _fake_clock(monkeypatch)
        orch = _FakeOrch()
        # Large interval — we drive polls manually via snapshot()/poll().
        mon = SpawnQueueHealthMonitor(orch, interval_ms=999_999)

        orch._spawn_in_progress = True
        mon._poll()  # first observation of True → since = t=1000.0

        clock["t"] += 65.0  # 65s later
        snap = mon.snapshot()
        assert snap.spawn_in_progress is True
        assert snap.spawn_in_progress_age_s == pytest.approx(65.0, abs=0.05)

        # Clearing the flag resets the age.
        orch._spawn_in_progress = False
        snap2 = mon.snapshot()
        assert snap2.spawn_in_progress is False
        assert snap2.spawn_in_progress_age_s is None

    def test_oldest_queued_age_tracks_fifo_head(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _fake_clock(monkeypatch)
        orch = _FakeOrch()
        mon = SpawnQueueHealthMonitor(orch, interval_ms=999_999)

        item_a = ("backend", None, "default", False, 0, None)
        orch._spawn_queue.append(item_a)
        mon._poll()  # head first seen at t=1000.0

        clock["t"] += 30.0
        item_b = ("qa", None, "default", False, 0, None)
        orch._spawn_queue.append(item_b)
        mon._poll()  # head unchanged (still item_a) → since stays 1000.0

        clock["t"] += 10.0
        snap = mon.snapshot()
        assert snap.queue_depth == 2
        assert snap.oldest_queued_age_s == pytest.approx(40.0, abs=0.05)

        # Head drains → the new head's age starts fresh.
        orch._spawn_queue.pop(0)
        snap2 = mon.snapshot()
        assert snap2.queue_depth == 1
        assert snap2.oldest_queued_age_s == pytest.approx(0.0, abs=0.05)

    def test_never_mutates_orchestrator_state(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        orch._spawn_queue.append(("backend", None, "default", False, 0, None))
        before_queue = list(orch._spawn_queue)
        before_flag = orch._spawn_in_progress
        mon = SpawnQueueHealthMonitor(orch, interval_ms=999_999)
        mon.snapshot()
        assert list(orch._spawn_queue) == before_queue
        assert orch._spawn_in_progress == before_flag

    def test_tolerates_malformed_orchestrator(self, qapp: QCoreApplication) -> None:
        """A test double / partially-built orchestrator must never crash the poller."""

        class _Weird:
            pass

        mon = SpawnQueueHealthMonitor(_Weird(), interval_ms=999_999)
        snap = mon.snapshot()
        assert snap.queue_depth == 0
        assert snap.spawn_in_progress is False


# ---------------------------------------------------------------------------
# cli_server.py — "spawn-queue-status" TCP command
# ---------------------------------------------------------------------------


class _FakeSock:
    def __init__(self) -> None:
        self.written = b""

    def write(self, b) -> None:
        self.written += bytes(b)

    def flush(self) -> None:
        pass


def _replies(sock: _FakeSock) -> list[dict]:
    return [json.loads(line) for line in sock.written.decode().splitlines() if line.strip()]


class TestSpawnQueueStatusCommand:
    def test_open_no_auth_required(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        orch._lead_token = "tok"
        srv = CliServer(orch)
        sock = _FakeSock()

        srv._dispatch(sock, {"cmd": "spawn-queue-status"})

        r = _replies(sock)
        assert len(r) == 1
        assert r[0]["ok"] is True
        assert r[0]["queue_depth"] == 0
        assert r[0]["spawn_in_progress"] is False
        assert r[0]["spawn_in_progress_age_s"] is None
        assert r[0]["oldest_queued_age_s"] is None

    def test_reports_live_queue_depth(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        orch._lead_token = "tok"
        orch._spawn_in_progress = True
        orch._spawn_queue.append(("backend", None, "default", False, 0, None))
        orch._spawn_queue.append(("qa", None, "default", False, 0, None))
        srv = CliServer(orch)
        sock = _FakeSock()

        srv._dispatch(sock, {"cmd": "spawn-queue-status"})

        r = _replies(sock)[0]
        assert r["ok"] is True
        assert r["queue_depth"] == 2
        assert r["spawn_in_progress"] is True


# ---------------------------------------------------------------------------
# doctor.check_spawn_queue_live — interpretation layer
# ---------------------------------------------------------------------------


class TestCheckSpawnQueueLive:
    """check_spawn_queue_live is a PURE interpreter of an already-fetched
    response dict (doctor.py is a leaf-modules-pure module per the
    import-linter contracts and must never import cli/orchestrator itself —
    even lazily; the linter's static analysis flags any import statement
    regardless of laziness). The TCP round-trip lives in cli.cmd_doctor
    instead — see TestCmdDoctorLiveWiring below."""

    def test_cockpit_not_running_is_skip_not_fail(self) -> None:
        findings = check_spawn_queue_live(None)
        assert len(findings) == 1
        assert findings[0].status is Status.SKIP
        assert findings[0].category == "spawn-queue"

    def test_empty_queue_is_ok(self) -> None:
        findings = check_spawn_queue_live(
            {
                "ok": True,
                "queue_depth": 0,
                "spawn_in_progress": False,
                "spawn_in_progress_age_s": None,
                "oldest_queued_age_s": None,
            }
        )
        assert len(findings) == 1
        assert findings[0].status is Status.OK

    def test_busy_but_fresh_queue_is_ok_not_fail(self) -> None:
        # Queue has depth but ages are well under the stuck threshold — normal
        # fan-out load, not a wedge.
        findings = check_spawn_queue_live(
            {
                "ok": True,
                "queue_depth": 3,
                "spawn_in_progress": True,
                "spawn_in_progress_age_s": 2.0,
                "oldest_queued_age_s": 1.0,
            }
        )
        assert findings[0].status is Status.OK

    def test_stuck_queue_is_fail(self) -> None:
        findings = check_spawn_queue_live(
            {
                "ok": True,
                "queue_depth": 4,
                "spawn_in_progress": True,
                "spawn_in_progress_age_s": 300.0,
                "oldest_queued_age_s": 250.0,
            }
        )
        assert len(findings) == 1
        assert findings[0].status is Status.FAIL
        assert "wedged" in findings[0].fix_hint

    def test_stuck_oldest_queued_without_in_progress_is_fail(self) -> None:
        # Arbiter flag itself isn't stuck, but an item has sat in the FIFO
        # far past the drain window — still a wedge worth flagging.
        findings = check_spawn_queue_live(
            {
                "ok": True,
                "queue_depth": 1,
                "spawn_in_progress": False,
                "spawn_in_progress_age_s": None,
                "oldest_queued_age_s": 120.0,
            }
        )
        assert findings[0].status is Status.FAIL

    def test_request_failure_is_warn_not_fail(self) -> None:
        findings = check_spawn_queue_live({"ok": False, "msg": "connection refused"})
        assert findings[0].status is Status.WARN

    def test_not_in_run_all_checks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`takkub doctor` (no --live) must stay orchestrator-socket-free.

        Stubs every default check to a cheap no-op (matches
        TestRunAllChecks.test_returns_list_of_findings in test_doctor.py) so
        this only exercises run_all_checks()'s own check table, never the
        real subprocess probes / git fetch check_version does.
        """
        import agent_takkub.doctor as doctor_mod
        from agent_takkub.doctor import run_all_checks

        for name, _fn in inspect.getmembers(doctor_mod, inspect.isfunction):
            if name.startswith("check_") and name != "check_spawn_queue_live":
                monkeypatch.setattr(doctor_mod, name, lambda: [])

        names = {(f.category, f.name) for f in run_all_checks()}
        assert ("spawn-queue", "wedge") not in names


# ---------------------------------------------------------------------------
# cli.cmd_doctor — fetch-and-pass wiring for --live
# ---------------------------------------------------------------------------


class TestCmdDoctorLiveWiring:
    """cmd_doctor owns the TCP round-trip (doctor.py can't — see
    TestCheckSpawnQueueLive's docstring); these tests cover that wiring:
    port-file presence, and turning a raised exception into a WARN finding
    instead of letting `takkub doctor --live` crash the whole command."""

    def _args(self, **overrides) -> object:
        import argparse

        defaults = {"fix": False, "install_providers": False, "json": False, "live": True}
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_live_false_never_calls_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub import cli

        monkeypatch.setattr("agent_takkub.doctor.run_all_checks", lambda: [])
        called = []
        monkeypatch.setattr(cli, "_request", lambda payload: called.append(payload) or {"ok": True})
        cli.cmd_doctor(self._args(live=False))
        assert called == []

    def test_cockpit_not_running_skips_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub import cli

        monkeypatch.setattr("agent_takkub.doctor.run_all_checks", lambda: [])
        monkeypatch.setattr(cli, "read_port", lambda: None)
        called = []
        monkeypatch.setattr(cli, "_request", lambda payload: called.append(payload) or {"ok": True})
        result = cli.cmd_doctor(self._args())
        assert called == []
        assert result["ok"] is True

    def test_request_exception_becomes_warn_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import cli

        monkeypatch.setattr("agent_takkub.doctor.run_all_checks", lambda: [])
        monkeypatch.setattr(cli, "read_port", lambda: 12345)

        def _boom(payload):
            raise OSError("connection refused")

        monkeypatch.setattr(cli, "_request", _boom)
        # Must not raise — a live-check failure degrades to a WARN finding.
        result = cli.cmd_doctor(self._args())
        assert result["ok"] is True  # WARN doesn't count toward n_fail

    def test_live_response_reaches_the_finding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub import cli

        monkeypatch.setattr("agent_takkub.doctor.run_all_checks", lambda: [])
        monkeypatch.setattr(cli, "read_port", lambda: 12345)
        monkeypatch.setattr(
            cli,
            "_request",
            lambda payload: {
                "ok": True,
                "queue_depth": 4,
                "spawn_in_progress": True,
                "spawn_in_progress_age_s": 300.0,
                "oldest_queued_age_s": 250.0,
            },
        )
        result = cli.cmd_doctor(self._args())
        assert result["ok"] is False
        assert "1 fail" in result["msg"]
