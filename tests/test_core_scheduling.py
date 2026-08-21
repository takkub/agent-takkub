"""Core V2 Scheduler/Resource/Runtime (docs/v2/07_SCHEDULER_RESOURCE_RUNTIME.md,
epic #309 Phase 8a): pure `SlotPolicy` admission, the backpressure ladder,
priority ordering, `ProcessRegistry` crash recovery, `AgentRuntimeControl`,
and the fail-open `.facade` `resource_governor.py` calls into.
"""

from __future__ import annotations

import time

import pytest

from agent_takkub.core.scheduling import backpressure, policy
from agent_takkub.core.scheduling import facade as scheduling_facade
from agent_takkub.core.scheduling.backpressure import BackpressureSignal
from agent_takkub.core.scheduling.flag import v2_scheduler_enabled
from agent_takkub.core.scheduling.models import (
    ActiveCounts,
    BackpressureLevel,
    Priority,
    SlotPolicy,
    SlotRequest,
)
from agent_takkub.core.scheduling.priority_queue import order_by_priority
from agent_takkub.core.scheduling.process_registry import ProcessRegistry, ProcessStatus
from agent_takkub.core.scheduling.runtime_control import AgentRuntimeControl, RunState


@pytest.fixture(autouse=True)
def _isolate_core_v2_settings(tmp_path, monkeypatch):
    from agent_takkub import config, core_v2_settings

    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    scheduling_facade._policy_cache = None
    core_v2_settings._reset_cache()
    yield
    scheduling_facade._policy_cache = None
    core_v2_settings._reset_cache()


# ── flag ──────────────────────────────────────────────────────────────────


def test_flag_on_by_default(monkeypatch):
    """Default flipped ON in 1.0.84 (epic #309)."""
    monkeypatch.delenv("TAKKUB_V2_SCHEDULER", raising=False)
    assert v2_scheduler_enabled() is True


def test_flag_on_when_set_to_1(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")
    assert v2_scheduler_enabled() is True


# ── Priority ─────────────────────────────────────────────────────────────


def test_priority_orders_critical_first():
    assert Priority.CRITICAL < Priority.HIGH < Priority.NORMAL < Priority.LOW < Priority.BACKGROUND


# ── SlotPolicy / policy.evaluate ────────────────────────────────────────


def test_default_slot_policy_denies_nothing():
    request = SlotRequest(project_id="p", pane_id="pane", task_id="t")
    counts = ActiveCounts(
        agents_global=999,
        panes_global=999,
        by_project_agents={"p": 999},
        by_project_panes={"p": 999},
    )
    assert policy.evaluate(request, counts, SlotPolicy()) == ""


def test_global_agents_limit():
    slot_policy = SlotPolicy(max_agents_global=2)
    request = SlotRequest(project_id="p", pane_id="pane", task_id="t")
    assert policy.evaluate(request, ActiveCounts(agents_global=1), slot_policy) == ""
    assert (
        policy.evaluate(request, ActiveCounts(agents_global=2), slot_policy)
        == "global_agents_limit"
    )


def test_global_panes_limit():
    slot_policy = SlotPolicy(max_panes_global=1)
    request = SlotRequest(project_id="p", pane_id="pane", task_id="t")
    assert (
        policy.evaluate(request, ActiveCounts(panes_global=1), slot_policy) == "global_panes_limit"
    )


def test_provider_limit():
    slot_policy = SlotPolicy(provider_max_concurrent={"claude": 2})
    request = SlotRequest(project_id="p", pane_id="pane", task_id="t", provider_id="claude")
    ok = ActiveCounts(by_provider={"claude": 1})
    full = ActiveCounts(by_provider={"claude": 2})
    assert policy.evaluate(request, ok, slot_policy) == ""
    assert policy.evaluate(request, full, slot_policy) == "provider_limit"
    # A request with no provider_id is never gated by provider limits.
    anon = SlotRequest(project_id="p", pane_id="pane", task_id="t")
    assert policy.evaluate(anon, full, slot_policy) == ""


def test_account_limit():
    slot_policy = SlotPolicy(account_max_concurrent={"acct-1": 1})
    request = SlotRequest(project_id="p", pane_id="pane", task_id="t", account_id="acct-1")
    assert policy.evaluate(request, ActiveCounts(by_account={"acct-1": 0}), slot_policy) == ""
    assert (
        policy.evaluate(request, ActiveCounts(by_account={"acct-1": 1}), slot_policy)
        == "account_limit"
    )


def test_project_agents_and_panes_limits():
    slot_policy = SlotPolicy(project_max_agents={"p": 1}, project_max_panes={"p": 3})
    request = SlotRequest(project_id="p", pane_id="pane", task_id="t")
    assert (
        policy.evaluate(request, ActiveCounts(by_project_agents={"p": 1}), slot_policy)
        == "project_agents_limit"
    )
    assert (
        policy.evaluate(request, ActiveCounts(by_project_panes={"p": 3}), slot_policy)
        == "project_panes_limit"
    )
    # A different, unconfigured project is unaffected.
    other = SlotRequest(project_id="q", pane_id="pane", task_id="t")
    assert policy.evaluate(other, ActiveCounts(by_project_agents={"p": 5}), slot_policy) == ""


# ── Backpressure ladder ──────────────────────────────────────────────────


def _signal(**overrides) -> BackpressureSignal:
    values = dict(
        cpu_percent=50.0,
        available_ram_percent=40.0,
        overloaded=False,
        cpu_pause_percent=85.0,
        cpu_resume_percent=65.0,
        min_available_ram_percent=20.0,
        queued_count=0,
        active_capacity_hint=1,
    )
    values.update(overrides)
    return BackpressureSignal(**values)


def test_backpressure_normal_when_healthy():
    assert backpressure.classify(_signal()) == BackpressureLevel.NORMAL


def test_backpressure_throttles_before_hard_overload():
    # resume=65, pause=85 -> throttle engages at 75, well before the
    # overloaded latch (85) trips.
    level = backpressure.classify(_signal(cpu_percent=76.0, overloaded=False))
    assert level == BackpressureLevel.THROTTLE_NEW


def test_backpressure_pauses_background_when_overloaded():
    level = backpressure.classify(_signal(overloaded=True, cpu_percent=90.0, queued_count=0))
    assert level == BackpressureLevel.PAUSE_BACKGROUND


def test_backpressure_escalates_to_queue_on_sustained_backlog():
    level = backpressure.classify(
        _signal(overloaded=True, cpu_percent=95.0, queued_count=10, active_capacity_hint=2)
    )
    assert level == BackpressureLevel.QUEUE


def test_backpressure_admits_matches_ladder_rung():
    assert backpressure.admits(BackpressureLevel.NORMAL, Priority.BACKGROUND) is True
    assert backpressure.admits(BackpressureLevel.THROTTLE_NEW, Priority.LOW) is False
    assert backpressure.admits(BackpressureLevel.THROTTLE_NEW, Priority.HIGH) is True
    assert backpressure.admits(BackpressureLevel.PAUSE_BACKGROUND, Priority.BACKGROUND) is False
    assert backpressure.admits(BackpressureLevel.PAUSE_BACKGROUND, Priority.LOW) is True
    assert backpressure.admits(BackpressureLevel.QUEUE, Priority.HIGH) is False
    assert backpressure.admits(BackpressureLevel.QUEUE, Priority.CRITICAL) is True


# ── priority_queue ───────────────────────────────────────────────────────


def test_order_by_priority_moves_higher_tiers_earlier_and_keeps_ties_stable():
    priorities = {"a": Priority.LOW, "b": Priority.CRITICAL, "c": Priority.LOW, "d": Priority.HIGH}
    ordered = order_by_priority(["a", "b", "c", "d"], lambda pid: priorities[pid])
    assert ordered == ["b", "d", "a", "c"]  # a before c preserved (stable sort)


# ── ProcessRegistry ──────────────────────────────────────────────────────


def _register(registry: ProcessRegistry, pid: int) -> None:
    registry.register(
        pid=pid,
        agent_id="agent-1",
        project_id="proj",
        task_id="task-1",
        pane_id="pane-1",
        provider_id="claude",
        account_id="acct-1",
    )


def test_process_registry_register_and_snapshot():
    registry = ProcessRegistry(clock=lambda: 100.0)
    _register(registry, 111)
    record = registry.get(111)
    assert record is not None
    assert record.status == ProcessStatus.RUNNING
    assert record.started_at == 100.0
    assert registry.for_project("proj") == [record]
    assert registry.snapshot() == [record]


def test_process_registry_mark_and_unregister():
    registry = ProcessRegistry()
    _register(registry, 222)
    updated = registry.mark(222, ProcessStatus.DONE)
    assert updated is not None and updated.status == ProcessStatus.DONE
    assert registry.mark(999, ProcessStatus.DONE) is None
    removed = registry.unregister(222)
    assert removed is not None and removed.pid == 222
    assert registry.get(222) is None


def test_process_registry_detect_stale_marks_interrupted_not_dropped():
    registry = ProcessRegistry()
    _register(registry, 1)
    _register(registry, 2)
    registry.mark(2, ProcessStatus.DONE)  # not RUNNING -> never inspected

    interrupted = registry.detect_stale(is_alive=lambda pid: pid != 1)

    assert [r.pid for r in interrupted] == [1]
    assert registry.get(1).status == ProcessStatus.INTERRUPTED
    assert registry.get(2).status == ProcessStatus.DONE  # untouched


def test_process_registry_detect_stale_is_alive_exception_marks_interrupted():
    registry = ProcessRegistry()
    _register(registry, 3)

    def boom(pid: int) -> bool:
        raise RuntimeError("no such process")

    interrupted = registry.detect_stale(is_alive=boom)
    assert [r.pid for r in interrupted] == [3]


# ── AgentRuntimeControl ──────────────────────────────────────────────────


def test_runtime_control_pause_resume_cancel():
    control = AgentRuntimeControl()
    assert control.state == RunState.RUNNING
    assert control.may_dispatch_new() is True

    control.pause()
    assert control.state == RunState.PAUSED
    assert control.may_dispatch_new() is False

    control.resume()
    assert control.state == RunState.RUNNING

    control.cancel()
    assert control.state == RunState.CANCELLED
    control.resume()  # cancel is terminal
    assert control.state == RunState.CANCELLED


def test_runtime_control_limit_wait_park_and_resume():
    # #322: usage-limit wait modeled as a first-class RunState, distinct
    # from an operator pause() — resume() must never clear it, and
    # resume_from_limit() must never clear a plain operator pause().
    control = AgentRuntimeControl()
    reset_at = time.time() + 3600

    control.park_for_limit(reset_at)
    assert control.state == RunState.LIMIT_WAIT
    assert control.limit_reset_at == reset_at
    assert control.may_dispatch_new() is False

    control.resume()  # plain resume must not touch LIMIT_WAIT
    assert control.state == RunState.LIMIT_WAIT

    control.resume_from_limit()
    assert control.state == RunState.RUNNING
    assert control.limit_reset_at is None
    assert control.may_dispatch_new() is True


def test_runtime_control_limit_wait_from_paused():
    control = AgentRuntimeControl()
    control.pause()
    control.park_for_limit(time.time() + 60)
    assert control.state == RunState.LIMIT_WAIT

    control.resume_from_limit()
    assert control.state == RunState.RUNNING  # not back to PAUSED — reset wins


def test_runtime_control_resume_from_limit_is_noop_on_plain_pause():
    control = AgentRuntimeControl()
    control.pause()
    control.resume_from_limit()  # never parked — must not clear the pause
    assert control.state == RunState.PAUSED


def test_runtime_control_limit_wait_terminal_states_ignore_park():
    control = AgentRuntimeControl()
    control.cancel()
    control.park_for_limit(time.time() + 60)
    assert control.state == RunState.CANCELLED  # terminal — park is a no-op

    control2 = AgentRuntimeControl()
    control2.mark_done()
    control2.park_for_limit(time.time() + 60)
    assert control2.state == RunState.DONE


def test_runtime_control_cancel_from_limit_wait():
    control = AgentRuntimeControl()
    control.park_for_limit(time.time() + 60)
    control.cancel()
    assert control.state == RunState.CANCELLED


def test_runtime_control_checkpoint_is_noop_when_flag_off(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "0")
    calls = []
    control = AgentRuntimeControl(checkpoint_fn=lambda: calls.append(1))
    assert control.checkpoint() is False
    assert calls == []


def test_runtime_control_checkpoint_calls_fn_when_flag_on(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")
    calls = []
    control = AgentRuntimeControl(checkpoint_fn=lambda: calls.append(1))
    assert control.checkpoint() is True
    assert calls == [1]


def test_runtime_control_checkpoint_fail_open(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")

    def boom():
        raise RuntimeError("disk full")

    control = AgentRuntimeControl(checkpoint_fn=boom)
    assert control.checkpoint() is False  # never raises


# ── facade: flag-off no-op, flag-on delegates, fail-open ────────────────


def test_facade_flag_off_is_all_noop(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "0")
    request = SlotRequest(project_id="p", pane_id="pane", task_id="t")
    tight_policy = SlotPolicy(max_agents_global=0)
    assert scheduling_facade.extended_denial_reason(request, ActiveCounts(), tight_policy) == ""

    level = scheduling_facade.backpressure_level(_signal(overloaded=True, queued_count=999))
    assert level == BackpressureLevel.NORMAL
    assert (
        scheduling_facade.backpressure_admits(BackpressureLevel.QUEUE, Priority.BACKGROUND) is True
    )

    assert scheduling_facade.order_projects(["b", "a"], lambda pid: Priority.NORMAL) == ["b", "a"]


def test_facade_flag_on_delegates_to_policy(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")
    request = SlotRequest(project_id="p", pane_id="pane", task_id="t")
    tight_policy = SlotPolicy(max_agents_global=0)
    assert (
        scheduling_facade.extended_denial_reason(
            request, ActiveCounts(agents_global=0), tight_policy
        )
        == "global_agents_limit"
    )

    level = scheduling_facade.backpressure_level(_signal(overloaded=True, queued_count=0))
    assert level == BackpressureLevel.PAUSE_BACKGROUND
    assert scheduling_facade.backpressure_admits(level, Priority.BACKGROUND) is False

    priorities = {"a": Priority.LOW, "b": Priority.CRITICAL}
    assert scheduling_facade.order_projects(["a", "b"], lambda pid: priorities[pid]) == ["b", "a"]


def test_facade_extended_denial_reason_fails_open(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")

    def boom(*args, **kwargs):
        raise RuntimeError("policy blew up")

    monkeypatch.setattr(scheduling_facade.policy, "evaluate", boom)
    request = SlotRequest(project_id="p", pane_id="pane", task_id="t")
    assert scheduling_facade.extended_denial_reason(request, ActiveCounts(), SlotPolicy()) == ""


# ── facade.effective_slot_policy ────────────────────────────────────────


def test_effective_slot_policy_flag_off_never_touches_settings_file(monkeypatch):
    # env explicitly "0" so v2_scheduler_enabled() short-circuits without
    # itself reading core_v2_settings to resolve the flag (see flag.py) —
    # isolates the "flag off never touches the file" claim to this function.
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "0")
    from agent_takkub import core_v2_settings

    def boom():
        raise AssertionError("settings file must not be read when the flag is off")

    monkeypatch.setattr(core_v2_settings, "path", boom)
    assert scheduling_facade.effective_slot_policy() == SlotPolicy()


def test_effective_slot_policy_flag_on_reads_persisted_config(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")
    from agent_takkub import core_v2_settings

    core_v2_settings.save_scheduler_policy(
        core_v2_settings.SchedulerPolicyConfig(
            max_agents_global=4, provider_max_concurrent={"codex": 2}
        )
    )
    got = scheduling_facade.effective_slot_policy()
    assert got.max_agents_global == 4
    assert got.provider_max_concurrent == {"codex": 2}


def test_effective_slot_policy_missing_file_fails_open_to_default(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")
    assert scheduling_facade.effective_slot_policy() == SlotPolicy()


def test_effective_slot_policy_caches_until_file_mtime_changes(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")
    from agent_takkub import core_v2_settings

    core_v2_settings.save_scheduler_policy(
        core_v2_settings.SchedulerPolicyConfig(max_agents_global=1)
    )
    first = scheduling_facade.effective_slot_policy()
    assert first.max_agents_global == 1

    calls = []
    real_load = core_v2_settings.load_scheduler_policy
    monkeypatch.setattr(
        core_v2_settings,
        "load_scheduler_policy",
        lambda: (calls.append(1), real_load())[1],
    )
    second = scheduling_facade.effective_slot_policy()
    assert second.max_agents_global == 1
    assert calls == []  # unchanged mtime -> cache hit, no reparse

    core_v2_settings.save_scheduler_policy(
        core_v2_settings.SchedulerPolicyConfig(max_agents_global=2)
    )
    third = scheduling_facade.effective_slot_policy()
    assert third.max_agents_global == 2
    assert calls == [1]  # mtime changed -> reloaded once


def test_effective_slot_policy_fails_open_on_load_exception(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")
    from agent_takkub import core_v2_settings

    core_v2_settings.path().parent.mkdir(parents=True, exist_ok=True)
    core_v2_settings.path().write_text("{}", encoding="utf-8")

    def boom():
        raise RuntimeError("config blew up")

    monkeypatch.setattr(core_v2_settings, "load_scheduler_policy", boom)
    assert scheduling_facade.effective_slot_policy() == SlotPolicy()


def test_facade_backpressure_level_fails_open(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")

    def boom(*args, **kwargs):
        raise RuntimeError("backpressure blew up")

    monkeypatch.setattr(scheduling_facade.backpressure, "classify", boom)
    assert scheduling_facade.backpressure_level(_signal()) == BackpressureLevel.NORMAL
