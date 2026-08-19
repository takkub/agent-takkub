"""`resource_governor.py` EXTEND for epic #309 Phase 8a
(docs/v2/REUSE_VS_REWRITE_MATRIX.md §5): provider/account/project/global
`SlotPolicy` dimensions, priority-ordered dispatch, and the backpressure
ladder — all gated behind `TAKKUB_V2_SCHEDULER` and wired through the ONE
`core.scheduling.facade`. `tests/test_resource_governor.py`'s full flag-off
suite passing unmodified is this file's parity proof; the tests below only
add NEW behavior that only exists once the flag/policy is explicitly set.
"""

from __future__ import annotations

import pytest

from agent_takkub.core.scheduling import facade as scheduling_facade
from agent_takkub.core.scheduling.models import Priority, SlotPolicy
from agent_takkub.resource_governor import GovernorLimits, ResourceClass, ResourceGovernor


@pytest.fixture(autouse=True)
def _isolate_core_v2_settings(tmp_path, monkeypatch):
    from agent_takkub import config

    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    scheduling_facade._policy_cache = None
    yield
    scheduling_facade._policy_cache = None


def _limits(**overrides) -> GovernorLimits:
    values = {
        "max_heavy_global": 10,
        "max_heavy_per_project": 10,
        "max_browser_global": 10,
        "max_build_global": 10,
        "max_test_global": 10,
        "max_package_install_global": 10,
        "cpu_pause_percent": 85,
        "cpu_resume_percent": 65,
        "min_available_ram_percent": 20,
        "resume_ram_percent": 25,
    }
    values.update(overrides)
    return GovernorLimits(**values)


def test_flag_off_slot_policy_is_ignored(monkeypatch):
    monkeypatch.delenv("TAKKUB_V2_SCHEDULER", raising=False)
    governor = ResourceGovernor(_limits(), slot_policy=SlotPolicy(max_agents_global=0))
    decision = governor.request_slot(
        project_id="p", pane_id="pane", task_id="t", resource_class=ResourceClass.NORMAL
    )
    assert decision.allowed  # a policy this tight would deny everything if it were consulted


def test_flag_on_default_slot_policy_still_denies_nothing(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")
    governor = ResourceGovernor(_limits())  # no slot_policy given -> SlotPolicy()
    decision = governor.request_slot(
        project_id="p", pane_id="pane", task_id="t", resource_class=ResourceClass.HEAVY
    )
    assert decision.allowed


def test_flag_on_no_explicit_policy_uses_settings_persisted_policy(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")
    from agent_takkub import core_v2_settings

    core_v2_settings.save_scheduler_policy(
        core_v2_settings.SchedulerPolicyConfig(provider_max_concurrent={"claude": 1})
    )
    governor = ResourceGovernor(_limits())  # no slot_policy given -> Settings policy applies
    first = governor.request_slot(
        project_id="p",
        pane_id="one",
        task_id="t1",
        resource_class=ResourceClass.NORMAL,
        provider_id="claude",
    )
    assert first.allowed
    second = governor.request_slot(
        project_id="p",
        pane_id="two",
        task_id="t2",
        resource_class=ResourceClass.NORMAL,
        provider_id="claude",
    )
    assert not second.allowed and second.reason == "provider_limit"


def test_explicit_slot_policy_wins_over_settings_persisted_policy(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")
    from agent_takkub import core_v2_settings

    core_v2_settings.save_scheduler_policy(
        core_v2_settings.SchedulerPolicyConfig(max_agents_global=0)
    )
    governor = ResourceGovernor(_limits(), slot_policy=SlotPolicy())  # explicit override wins
    decision = governor.request_slot(
        project_id="p", pane_id="pane", task_id="t", resource_class=ResourceClass.LIGHT
    )
    assert decision.allowed


def test_flag_on_provider_limit_denies_and_releases(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")
    governor = ResourceGovernor(
        _limits(), slot_policy=SlotPolicy(provider_max_concurrent={"claude": 1})
    )
    first = governor.request_slot(
        project_id="p",
        pane_id="one",
        task_id="t1",
        resource_class=ResourceClass.NORMAL,
        provider_id="claude",
    )
    assert first.allowed
    second = governor.request_slot(
        project_id="p",
        pane_id="two",
        task_id="t2",
        resource_class=ResourceClass.NORMAL,
        provider_id="claude",
    )
    assert not second.allowed
    assert second.reason == "provider_limit"

    assert governor.release_slot(first.token)
    third = governor.request_slot(
        project_id="p",
        pane_id="three",
        task_id="t3",
        resource_class=ResourceClass.NORMAL,
        provider_id="claude",
    )
    assert third.allowed


def test_flag_on_account_limit_denies():
    import os

    os.environ["TAKKUB_V2_SCHEDULER"] = "1"
    try:
        governor = ResourceGovernor(
            _limits(), slot_policy=SlotPolicy(account_max_concurrent={"acct-1": 1})
        )
        assert governor.request_slot(
            project_id="p",
            pane_id="one",
            task_id="t1",
            resource_class=ResourceClass.NORMAL,
            account_id="acct-1",
        ).allowed
        blocked = governor.request_slot(
            project_id="p",
            pane_id="two",
            task_id="t2",
            resource_class=ResourceClass.NORMAL,
            account_id="acct-1",
        )
        assert not blocked.allowed and blocked.reason == "account_limit"
    finally:
        del os.environ["TAKKUB_V2_SCHEDULER"]


def test_flag_on_project_max_agents_denies(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")
    governor = ResourceGovernor(_limits(), slot_policy=SlotPolicy(project_max_agents={"p": 1}))
    assert governor.request_slot(
        project_id="p", pane_id="one", task_id="t1", resource_class=ResourceClass.LIGHT
    ).allowed
    blocked = governor.request_slot(
        project_id="p", pane_id="two", task_id="t2", resource_class=ResourceClass.LIGHT
    )
    assert not blocked.allowed and blocked.reason == "project_agents_limit"
    # A different project is unaffected by "p"'s cap.
    assert governor.request_slot(
        project_id="q", pane_id="three", task_id="t3", resource_class=ResourceClass.LIGHT
    ).allowed


def test_flag_on_dispatch_waiting_orders_by_priority(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")
    governor = ResourceGovernor(_limits(max_heavy_global=1, max_heavy_per_project=1))
    holder = governor.request_slot(
        project_id="busy", pane_id="holder", task_id="h", resource_class=ResourceClass.HEAVY
    )
    assert holder.allowed

    admitted_order: list[str] = []
    governor.enqueue(
        project_id="low-proj",
        pane_id="low",
        task_id="low",
        resource_class=ResourceClass.HEAVY,
        priority=Priority.LOW,
        on_admitted=lambda token: admitted_order.append("low"),
    )
    governor.enqueue(
        project_id="critical-proj",
        pane_id="crit",
        task_id="crit",
        resource_class=ResourceClass.HEAVY,
        priority=Priority.CRITICAL,
        on_admitted=lambda token: admitted_order.append("critical"),
    )

    assert governor.release_slot(holder.token)
    governor.dispatch_waiting(max_dispatch=1)
    assert admitted_order == ["critical"]


def test_flag_off_dispatch_waiting_keeps_fifo_project_order(monkeypatch):
    monkeypatch.delenv("TAKKUB_V2_SCHEDULER", raising=False)
    governor = ResourceGovernor(_limits(max_heavy_global=1, max_heavy_per_project=1))
    holder = governor.request_slot(
        project_id="busy", pane_id="holder", task_id="h", resource_class=ResourceClass.HEAVY
    )
    admitted_order: list[str] = []
    governor.enqueue(
        project_id="low-proj",
        pane_id="low",
        task_id="low",
        resource_class=ResourceClass.HEAVY,
        priority=Priority.LOW,
        on_admitted=lambda token: admitted_order.append("low"),
    )
    governor.enqueue(
        project_id="critical-proj",
        pane_id="crit",
        task_id="crit",
        resource_class=ResourceClass.HEAVY,
        priority=Priority.CRITICAL,
        on_admitted=lambda token: admitted_order.append("critical"),
    )
    assert governor.release_slot(holder.token)
    governor.dispatch_waiting(max_dispatch=1)
    # Priority is ignored while the flag is off -- plain round-robin/FIFO
    # order applies, same as before Phase 8a existed.
    assert admitted_order == ["low"]


def test_flag_on_backpressure_throttles_low_priority_new_work(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_SCHEDULER", "1")
    samples = iter([(76.0, 50.0, 10)])  # cpu above the throttle ramp, below hard pause (85)
    governor = ResourceGovernor(_limits(), sampler=lambda: next(samples))
    governor.sample()
    assert not governor.overloaded()

    denied = governor.request_slot(
        project_id="p",
        pane_id="pane",
        task_id="t",
        resource_class=ResourceClass.LIGHT,
        priority=Priority.LOW,
    )
    assert not denied.allowed
    assert denied.reason == "backpressure_throttle_new"

    allowed = governor.request_slot(
        project_id="p",
        pane_id="pane2",
        task_id="t2",
        resource_class=ResourceClass.LIGHT,
        priority=Priority.CRITICAL,
    )
    assert allowed.allowed
