"""Tests for CliServer dispatch — focus on the async spawn/assign path that
acks the client immediately and runs the heavy pane spawn on the next event
loop tick (so a slow spawn never blows the CLI's 15 s timeout / freezes IPC;
see docs/cockpit-freeze-rca-2026-05-29.md)."""

from __future__ import annotations

import json
import re

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.cli_server import CliServer


def _delay_ms(reply_msg: str) -> int:
    """Extract the `+<n>ms` stagger suffix the dispatcher reports."""
    m = re.search(r"\+(\d+)ms", reply_msg)
    assert m is not None, f"no +Nms suffix in {reply_msg!r}"
    return int(m.group(1))


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


class _FakeSock:
    def __init__(self) -> None:
        self.written = b""

    def write(self, b) -> None:
        self.written += bytes(b)

    def flush(self) -> None:
        pass


class _FakeOrch:
    _lead_token = "tok"

    def __init__(self) -> None:
        self.assign_calls: list[tuple] = []
        self.spawn_calls: list[tuple] = []

    def assign(
        self,
        role,
        cwd=None,
        task="",
        requires_commit=False,
        auto_chain=False,
        shard_total=0,
        project=None,
    ):
        self.assign_calls.append((role, cwd, task, requires_commit, auto_chain))
        return True, "ok"

    def spawn(self, role, cwd=None, project=None):
        self.spawn_calls.append((role, cwd))
        return True, "ok"


def _replies(sock: _FakeSock) -> list[dict]:
    return [json.loads(line) for line in sock.written.decode().splitlines() if line.strip()]


def _auth(extra: dict) -> dict:
    base = {"from": "lead", "auth": "tok"}
    base.update(extra)
    return base


class TestAsyncSpawnDispatch:
    def test_assign_acked_immediately_then_deferred(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        srv = CliServer(orch)
        sock = _FakeSock()

        srv._dispatch(sock, _auth({"cmd": "assign", "role": "backend", "task": "do x"}))

        # Replied right away, before the orchestrator did any spawn work.
        r = _replies(sock)
        assert len(r) == 1 and r[0]["ok"] is True
        assert orch.assign_calls == [], "assign must be deferred, not run inline"

        # Runs on the next event-loop tick.
        qapp.processEvents()
        assert orch.assign_calls == [("backend", None, "do x", False, False)]

    def test_assign_passes_flags(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        srv = CliServer(orch)
        sock = _FakeSock()
        srv._dispatch(
            sock,
            _auth(
                {
                    "cmd": "assign",
                    "role": "backend",
                    "cwd": "C:/x",
                    "task": "t",
                    "requires_commit": True,
                    "auto_chain": True,
                }
            ),
        )
        qapp.processEvents()
        assert orch.assign_calls == [("backend", "C:/x", "t", True, True)]

    def test_spawn_acked_immediately_then_deferred(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        srv = CliServer(orch)
        sock = _FakeSock()
        srv._dispatch(sock, _auth({"cmd": "spawn", "role": "frontend"}))
        assert _replies(sock)[0]["ok"] is True
        assert orch.spawn_calls == []
        qapp.processEvents()
        assert orch.spawn_calls == [("frontend", None)]

    def test_missing_role_is_immediate_error(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        srv = CliServer(orch)
        sock = _FakeSock()
        srv._dispatch(sock, _auth({"cmd": "assign", "task": "x"}))
        r = _replies(sock)
        assert r[0]["ok"] is False and "role" in r[0]["msg"]
        qapp.processEvents()
        assert orch.assign_calls == []  # nothing scheduled

    def test_unauthorized_assign_rejected_not_deferred(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        srv = CliServer(orch)
        sock = _FakeSock()
        # Wrong token → the lead-only gate rejects before any scheduling.
        srv._dispatch(sock, {"cmd": "assign", "from": "lead", "auth": "WRONG", "role": "backend"})
        r = _replies(sock)
        assert r[0]["ok"] is False
        qapp.processEvents()
        assert orch.assign_calls == []

    def test_non_lead_assign_rejected(self, qapp: QCoreApplication) -> None:
        orch = _FakeOrch()
        srv = CliServer(orch)
        sock = _FakeSock()
        srv._dispatch(sock, {"cmd": "assign", "from": "backend", "role": "qa"})
        r = _replies(sock)
        assert r[0]["ok"] is False and "lead" in r[0]["msg"].lower()
        qapp.processEvents()
        assert orch.assign_calls == []


class TestSpawnStagger:
    """Concurrent assigns must be spaced apart so back-to-back ConPTY spawns
    don't collide on one event-loop tick (#44); codex gets a bigger gap so its
    npm self-update windows don't overlap (#38). Non-blocking — QTimer only."""

    def test_first_assign_has_zero_delay(self, qapp: QCoreApplication) -> None:
        srv = CliServer(_FakeOrch())
        sock = _FakeSock()
        srv._dispatch(sock, _auth({"cmd": "assign", "role": "backend", "task": "x"}))
        assert _delay_ms(_replies(sock)[0]["msg"]) == 0  # lone assign unchanged

    def test_parallel_assigns_are_staggered(self, qapp: QCoreApplication) -> None:
        srv = CliServer(_FakeOrch())
        srv._spawn_gap_ms = 400
        delays = []
        for _ in range(3):
            sock = _FakeSock()
            srv._dispatch(sock, _auth({"cmd": "assign", "role": "backend", "task": "x"}))
            delays.append(_delay_ms(_replies(sock)[0]["msg"]))
        d0, d1, d2 = delays
        assert d0 == 0
        assert 0 < d1 <= 400
        assert d1 < d2 <= 800  # spawns spaced ~one gap apart, not all on one tick

    def test_codex_gets_larger_gap(self, qapp: QCoreApplication) -> None:
        srv = CliServer(_FakeOrch())
        srv._spawn_gap_ms = 400
        srv._codex_gap_ms = 10_000
        s1, s2 = _FakeSock(), _FakeSock()
        srv._dispatch(s1, _auth({"cmd": "assign", "role": "codex", "task": "x"}))
        srv._dispatch(s2, _auth({"cmd": "assign", "role": "codex", "task": "y"}))
        assert _delay_ms(_replies(s1)[0]["msg"]) == 0
        # second codex waits the (much larger) codex gap, not the 400ms general gap.
        assert _delay_ms(_replies(s2)[0]["msg"]) > 5_000

    def test_non_codex_after_codex_not_penalized(self, qapp: QCoreApplication) -> None:
        srv = CliServer(_FakeOrch())
        srv._spawn_gap_ms = 400
        srv._codex_gap_ms = 10_000
        s1, s2 = _FakeSock(), _FakeSock()
        srv._dispatch(s1, _auth({"cmd": "assign", "role": "codex", "task": "x"}))
        srv._dispatch(s2, _auth({"cmd": "assign", "role": "backend", "task": "y"}))
        backend_delay = _delay_ms(_replies(s2)[0]["msg"])
        # backend is spaced by the general gap, NOT held back the full codex gap.
        assert 0 < backend_delay <= 400
