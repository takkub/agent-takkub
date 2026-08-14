from __future__ import annotations

from agent_takkub.resource_governor import (
    GovernorLimits,
    ResourceClass,
    ResourceGovernor,
)


def _limits(**overrides) -> GovernorLimits:
    values = {
        "max_heavy_global": 2,
        "max_heavy_per_project": 1,
        "max_browser_global": 1,
        "max_build_global": 1,
        "max_test_global": 1,
        "max_package_install_global": 1,
        "cpu_pause_percent": 85,
        "cpu_resume_percent": 65,
        "min_available_ram_percent": 20,
        "resume_ram_percent": 25,
    }
    values.update(overrides)
    return GovernorLimits(**values)


def _request(governor, project, pane, resource_class=ResourceClass.HEAVY):
    return governor.request_slot(
        project_id=project,
        pane_id=pane,
        task_id=f"{project}-{pane}",
        resource_class=resource_class,
    )


def test_global_per_project_and_class_limits_release_cleanly() -> None:
    governor = ResourceGovernor(_limits())
    a = _request(governor, "a", "one")
    assert a.allowed and a.token
    assert not _request(governor, "a", "two").allowed
    browser = _request(governor, "b", "browser", ResourceClass.BROWSER)
    assert browser.allowed and browser.token
    assert not _request(governor, "c", "browser", ResourceClass.BROWSER).allowed

    assert governor.release_slot(a.token)
    assert _request(governor, "c", "heavy").allowed


def test_cpu_and_memory_hysteresis_is_non_blocking() -> None:
    samples = iter([(90.0, 50.0, 10), (70.0, 50.0, 10), (60.0, 50.0, 10)])
    governor = ResourceGovernor(_limits(), sampler=lambda: next(samples))
    assert governor.sample()["overloaded"]
    assert governor.sample()["overloaded"]  # above resume threshold: remains paused
    assert not governor.sample()["overloaded"]


def test_waiting_queue_is_round_robin_by_project() -> None:
    governor = ResourceGovernor(_limits(max_heavy_global=1, max_heavy_per_project=1))
    held = _request(governor, "held", "one")
    order: list[str] = []
    governor.enqueue(
        project_id="a",
        pane_id="a1",
        task_id="a1",
        resource_class=ResourceClass.HEAVY,
        on_admitted=lambda _token: order.append("a1"),
    )
    governor.enqueue(
        project_id="b",
        pane_id="b1",
        task_id="b1",
        resource_class=ResourceClass.HEAVY,
        on_admitted=lambda _token: order.append("b1"),
    )
    governor.release_slot(held.token)
    admitted = governor.dispatch_waiting(max_dispatch=1)
    assert len(admitted) == 1
    assert order == ["a1"]
    governor.release_slot(admitted[0])
    governor.dispatch_waiting(max_dispatch=1)
    assert order == ["a1", "b1"]


def test_cancel_waiting_on_pane_close() -> None:
    governor = ResourceGovernor(_limits())
    governor.enqueue(
        project_id="a",
        pane_id="qa",
        task_id="task",
        resource_class=ResourceClass.BROWSER,
    )
    assert governor.cancel_waiting(project_id="a", pane_id="qa") == 1
    assert governor.snapshot()["queued_resource_tasks"] == 0


def test_live_limit_update_preserves_active_tokens_and_waiting_queue() -> None:
    governor = ResourceGovernor(_limits(max_heavy_global=1, max_heavy_per_project=1))
    held = _request(governor, "a", "one")
    governor.enqueue(
        project_id="b",
        pane_id="two",
        task_id="b-two",
        resource_class=ResourceClass.HEAVY,
    )
    governor.update_limits(_limits(max_heavy_global=3, max_heavy_per_project=2))
    snapshot = governor.snapshot()
    assert snapshot["active_heavy_tasks"] == 1
    assert snapshot["queued_resource_tasks"] == 1
    assert snapshot["resource_limits"]["max_heavy_global"] == 3
    assert governor.release_slot(held.token)
