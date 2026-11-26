"""#1055: the re-dispatch a close→respawn step leaves on a timer must never
end silently — the CLI already told the Lead "task queued"."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator


def _fake(dispatch):
    warned: list[tuple] = []
    fake = SimpleNamespace(
        _resolve_project=lambda p: p or "default",
        _assign_dispatch=dispatch,
        _warn_lead_spawn_failed=lambda role, project, reason: warned.append(
            (role, project, reason)
        ),
    )
    return fake, warned


@pytest.fixture(autouse=True)
def _timers_run_now(monkeypatch):
    monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))
    monkeypatch.setattr(orch_mod, "_log_event", lambda *a, **k: None)


def test_success_is_quiet():
    calls = []
    fake, warned = _fake(lambda *a, **k: calls.append(a) or (True, "ok"))
    Orchestrator._redispatch_assign(fake, "frontend", "/w", "task", project="p")
    assert len(calls) == 1 and warned == []


def test_refusal_is_retried_once_then_reported():
    calls = []
    fake, warned = _fake(lambda *a, **k: calls.append(a) or (False, "pane still closing"))
    Orchestrator._redispatch_assign(fake, "frontend", "/w", "task", project="p")
    assert len(calls) == 2
    assert len(warned) == 1 and "pane still closing" in warned[0][2]


def test_exception_in_the_timer_callback_is_reported_not_swallowed():
    def boom(*a, **k):
        raise RuntimeError("worktree gone")

    fake, warned = _fake(boom)
    Orchestrator._redispatch_assign(fake, "frontend", "/w", "task", project="p")
    assert len(warned) == 1 and "worktree gone" in warned[0][2]


def test_second_attempt_succeeding_is_quiet():
    results = iter([(False, "not yet"), (True, "ok")])
    fake, warned = _fake(lambda *a, **k: next(results))
    Orchestrator._redispatch_assign(fake, "frontend", "/w", "task", project="p")
    assert warned == []


def test_dispatch_that_already_warned_is_not_warned_twice():
    fake, warned = _fake(None)

    def dispatch(*a, **k):
        fake._spawn_failed_warn_seq = getattr(fake, "_spawn_failed_warn_seq", 0) + 1
        return False, "spawn failed"

    fake._assign_dispatch = dispatch
    calls_before = len(warned)
    Orchestrator._redispatch_assign(fake, "frontend", "/w", "task", project="p")
    assert len(warned) == calls_before  # the dispatch's own #26 warning stands alone
