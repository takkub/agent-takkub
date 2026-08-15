"""Targeted tests for #242 (`takkub wait`).

Covers the orchestrator-side registration/poll/resolve logic
(`LeadWaitMixin.begin_wait` / `poll_wait` / `end_wait`) directly — these
tests manipulate `_wait_done_events` / queue state the same way
`test_inbox_report.py` manipulates the notice queues, rather than driving
the full `done()` machinery (file writes, vault mirror, hot.md refresh)
which is already covered by `test_done_note_symmetrize.py`.
"""

from __future__ import annotations

import collections
import time

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import cli
from agent_takkub.orchestrator import Orchestrator

PROJECT = "wait-test"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _register_working(orch: Orchestrator, role: str, project: str = PROJECT) -> None:
    from unittest.mock import MagicMock

    pane = MagicMock()
    pane.state = "working"
    pane.session = MagicMock()
    pane.session.is_blocked_on_tty_prompt.return_value = None
    orch._panes_by_project.setdefault(project, {})[role] = pane


class TestBeginWait:
    def test_explicit_roles_register(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")

        result = orch.begin_wait(PROJECT, ["backend"], 60.0)

        assert result["ok"] is True
        assert result["roles"] == ["backend"]
        assert result["attached"] is False
        assert result["wait_id"]

    def test_empty_roles_default_to_active_roles(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        _register_working(orch, "frontend")

        result = orch.begin_wait(PROJECT, [], 60.0)

        assert result["ok"] is True
        assert set(result["roles"]) == {"backend", "frontend"}

    def test_empty_roles_and_nothing_active_fails(self, orch: Orchestrator) -> None:
        result = orch.begin_wait(PROJECT, [], 60.0)

        assert result["ok"] is False
        assert "nothing to wait on" in result["msg"]

    def test_lead_excluded_from_default_roles(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        _register_working(orch, "lead")

        result = orch.begin_wait(PROJECT, [], 60.0)

        assert result["roles"] == ["backend"]

    def test_second_call_attaches_and_unions_roles(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        _register_working(orch, "frontend")

        first = orch.begin_wait(PROJECT, ["backend"], 60.0)
        second = orch.begin_wait(PROJECT, ["frontend"], 60.0)

        assert second["attached"] is True
        assert second["wait_id"] == first["wait_id"]
        assert set(second["roles"]) == {"backend", "frontend"}
        # Only one registration exists — no duplicate poll loop was created.
        assert len(orch._active_waits) == 1

    def test_abandoned_registration_is_replaced(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        first = orch.begin_wait(PROJECT, ["backend"], 1.0)
        # Simulate the owning CLI process having died long ago without
        # calling end_wait: last_poll_ts is far in the past.
        orch._active_waits[PROJECT]["last_poll_ts"] = time.time() - 10_000

        second = orch.begin_wait(PROJECT, ["backend"], 60.0)

        assert second["attached"] is False
        assert second["wait_id"] != first["wait_id"]


class TestPollWait:
    def test_unknown_wait_id_fails(self, orch: Orchestrator) -> None:
        result = orch.poll_wait(PROJECT, "nope")

        assert result["ok"] is False
        assert "no longer active" in result["msg"]

    def test_still_working_role_is_pending(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["ok"] is True
        assert result["pending"] == {"backend": "ยังทำงานอยู่"}
        assert not result["done"]
        assert not result["failed"]

    def test_role_resolves_to_done_after_started(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)

        orch._wait_done_events[(PROJECT, "backend")] = {"ts": time.time(), "failed": False}
        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["done"] == {"backend": "delivered"}
        assert not result["pending"]

    def test_role_resolves_to_failed(self, orch: Orchestrator) -> None:
        _register_working(orch, "qa")
        begin = orch.begin_wait(PROJECT, ["qa"], 60.0)

        orch._wait_done_events[(PROJECT, "qa")] = {"ts": time.time(), "failed": True}
        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["failed"] == {"qa": "delivered"}
        assert not result["done"]

    def test_completion_before_wait_started_is_ignored(self, orch: Orchestrator) -> None:
        """#241-style staleness rule: wait only reacts to NEW completions."""
        _register_working(orch, "backend")
        orch._wait_done_events[(PROJECT, "backend")] = {
            "ts": time.time() - 1000,
            "failed": False,
        }

        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)
        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["pending"]
        assert "backend" in result["pending"]

    def test_resolved_but_still_queued_stays_pending(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)

        orch._wait_done_events[(PROJECT, "backend")] = {"ts": time.time(), "failed": False}
        orch._lead_digest_queue = {
            PROJECT: collections.deque([("[backend done] x", None, time.time())])
        }
        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert not result["done"]
        assert "backend" in result["pending"]
        assert "queued" in result["pending"]["backend"] or "รอ" in result["pending"]["backend"]

    def test_unknown_role_reports_not_found_reason(self, orch: Orchestrator) -> None:
        begin = orch.begin_wait(PROJECT, ["ghost"], 60.0)

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert "ghost" in result["pending"]

    def test_registration_auto_removed_once_all_resolved(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)
        orch._wait_done_events[(PROJECT, "backend")] = {"ts": time.time(), "failed": False}

        orch.poll_wait(PROJECT, begin["wait_id"])

        assert PROJECT not in orch._active_waits
        # A stale poll against the now-gone registration fails cleanly.
        follow_up = orch.poll_wait(PROJECT, begin["wait_id"])
        assert follow_up["ok"] is False

    def test_timeout_marks_expired_and_removes_registration(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 1.0)
        # begin_wait floors timeout_s at 1.0 — push started_ts into the past
        # instead of sleeping a full second in a unit test.
        orch._active_waits[PROJECT]["started_ts"] = time.time() - 10.0

        result = orch.poll_wait(PROJECT, begin["wait_id"])

        assert result["expired"] is True
        assert "backend" in result["pending"]
        assert PROJECT not in orch._active_waits


class TestEndWait:
    def test_end_wait_removes_matching_registration(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        begin = orch.begin_wait(PROJECT, ["backend"], 60.0)

        assert orch.end_wait(PROJECT, begin["wait_id"]) is True
        assert PROJECT not in orch._active_waits

    def test_end_wait_is_noop_for_mismatched_id(self, orch: Orchestrator) -> None:
        _register_working(orch, "backend")
        orch.begin_wait(PROJECT, ["backend"], 60.0)

        assert orch.end_wait(PROJECT, "not-the-id") is False
        assert PROJECT in orch._active_waits


class TestCliWaitCommand:
    def test_resolves_immediately_when_nothing_pending(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        calls: list[dict] = []

        def fake_request(payload: dict) -> dict:
            calls.append(payload)
            if payload["cmd"] == "wait-begin":
                return {
                    "ok": True,
                    "msg": "watching 1 role(s)",
                    "wait_id": "w1",
                    "roles": ["backend"],
                    "started_ts": time.time(),
                    "attached": False,
                }
            if payload["cmd"] == "wait-poll":
                return {
                    "ok": True,
                    "msg": "resolved",
                    "done": {"backend": "delivered"},
                    "failed": {},
                    "pending": {},
                    "elapsed": 1.0,
                    "expired": False,
                }
            raise AssertionError(f"unexpected cmd: {payload['cmd']}")

        monkeypatch.setattr(cli, "_request", fake_request)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        rc = cli.main(["wait", "--role", "backend"])
        out = capsys.readouterr().out

        assert rc == 0
        assert "backend" in out
        assert [c["cmd"] for c in calls] == ["wait-begin", "wait-poll"]

    def test_begin_failure_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cli,
            "_request",
            lambda payload: {"ok": False, "msg": "nothing to wait on"},
        )
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        rc = cli.main(["wait"])

        assert rc == 1

    def test_timeout_is_clamped_into_range(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[dict] = []

        def fake_request(payload: dict) -> dict:
            sent.append(payload)
            if payload["cmd"] == "wait-begin":
                return {
                    "ok": True,
                    "msg": "watching 0 role(s)",
                    "wait_id": "w1",
                    "roles": [],
                    "started_ts": time.time(),
                    "attached": False,
                }
            return {
                "ok": True,
                "msg": "resolved",
                "done": {},
                "failed": {},
                "pending": {},
                "elapsed": 0.0,
                "expired": False,
            }

        monkeypatch.setattr(cli, "_request", fake_request)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)

        cli.main(["wait", "--timeout", "999999"])

        assert sent[0]["timeout"] == cli._WAIT_MAX_TIMEOUT_S

    def test_teammate_role_blocked_by_cli_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        rc = cli.main(["wait"])
        assert rc == 1
