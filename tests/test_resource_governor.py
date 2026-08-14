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


def test_gate_block_backoff_reduces_retry_frequency() -> None:
    """Issue #195: a queue head that stays blocked used to be retried (and
    logged via `resource_gate_block`) unconditionally on every dispatch tick
    — proven in the field as 15 identical lines in 15s for one task. Pins
    the 1s/2s/5s/15s backoff schedule: over 10 one-second ticks starting
    from enqueue, an attempt (and its log line) should land only at
    t=1, 2, 4, 9 — 4 lines instead of 10."""
    clock = [0.0]
    events: list[tuple[str, dict]] = []
    governor = ResourceGovernor(
        _limits(max_heavy_global=1, max_heavy_per_project=1),
        clock=lambda: clock[0],
        event_sink=lambda event, details: events.append((event, details)),
    )
    held = _request(governor, "held", "one")
    assert held.allowed
    governor.enqueue(
        project_id="a",
        pane_id="a1",
        task_id="a1",
        resource_class=ResourceClass.HEAVY,
    )
    events.clear()

    for _ in range(10):
        clock[0] += 1.0
        governor.dispatch_waiting()

    block_events = [e for e, _ in events if e == "resource_gate_block"]
    assert len(block_events) == 4, block_events


def test_gate_unblock_emits_single_summary_event() -> None:
    """Issue #195: once the blocked task is finally admitted, a single
    `resource_gate_unblocked` event carries blocked_for_s + attempts instead
    of the retry loop's per-attempt `resource_gate_block` flood."""
    clock = [0.0]
    events: list[tuple[str, dict]] = []
    governor = ResourceGovernor(
        _limits(max_heavy_global=1, max_heavy_per_project=1),
        clock=lambda: clock[0],
        event_sink=lambda event, details: events.append((event, details)),
    )
    held = _request(governor, "held", "one")
    governor.enqueue(
        project_id="a",
        pane_id="a1",
        task_id="a1",
        resource_class=ResourceClass.HEAVY,
    )
    events.clear()
    for _ in range(3):
        clock[0] += 1.0
        governor.dispatch_waiting()  # 2 denials land at t=1, t=2 (see backoff test)

    governor.release_slot(held.token)
    clock[0] += 1.0  # t=4 — exactly when the 2nd denial's backoff next allows a retry
    events_before_admit = len(events)
    admitted = governor.dispatch_waiting()
    events_since_admit = events[events_before_admit:]

    assert len(admitted) == 1
    unblock_events = [d for e, d in events if e == "resource_gate_unblocked"]
    assert len(unblock_events) == 1
    assert unblock_events[0]["attempts"] == 2
    assert unblock_events[0]["blocked_for_s"] == 4.0
    assert not any(e == "resource_gate_block" for e, _ in events_since_admit), (
        "the successful admission attempt itself must not log another gate_block"
    )


def test_freed_slot_admits_immediately_without_backoff_delay() -> None:
    """A queue head must not be held back by an up-front backoff on its
    very first dispatch attempt — only an actual denial inside the retry
    loop should push next_retry_at forward. (Regression guard: this is what
    `test_waiting_queue_is_round_robin_by_project` already relies on.)"""
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
    governor.release_slot(held.token)
    admitted = governor.dispatch_waiting()
    assert order == ["a1"]
    assert len(admitted) == 1


def test_freed_slot_admits_backed_off_item_without_waiting_for_backoff_issue_201() -> None:
    """Issue #201: dispatch_waiting used to gate a queue head purely on
    elapsed time — after release_slot() freed a slot mid-backoff, the freed
    slot sat idle until the item's own next_retry_at (up to 15s) instead of
    being reused right away. A slot release must invalidate every queue
    head's backoff so the very next dispatch_waiting() call retries it
    immediately, no matter how little wall-clock time has passed since the
    backoff was set."""
    clock = [0.0]
    governor = ResourceGovernor(
        _limits(max_heavy_global=1, max_heavy_per_project=1),
        clock=lambda: clock[0],
    )
    held = _request(governor, "held", "one")
    assert held.allowed
    order: list[str] = []
    governor.enqueue(
        project_id="a",
        pane_id="a1",
        task_id="a1",
        resource_class=ResourceClass.HEAVY,
        on_admitted=lambda _token: order.append("a1"),
    )
    clock[0] += 1.0
    governor.dispatch_waiting()  # denied -> next_retry_at pushed out to t=2

    governor.release_slot(held.token)
    admitted = governor.dispatch_waiting()  # still t=1: must not wait for t=2
    assert len(admitted) == 1
    assert order == ["a1"]


def test_dispatch_waiting_accepts_injectable_now() -> None:
    """Issue #201 point 3: callers (the stress harness) need to fast-forward
    past a backoff deterministically without a real sleep."""
    governor = ResourceGovernor(_limits(max_heavy_global=1, max_heavy_per_project=1))
    held = _request(governor, "held", "one")
    governor.enqueue(
        project_id="a",
        pane_id="a1",
        task_id="a1",
        resource_class=ResourceClass.HEAVY,
    )
    governor.dispatch_waiting(now=1_000.0)  # denied -> next_retry_at = 1001.0
    governor.release_slot(held.token)
    admitted = governor.dispatch_waiting(now=1_000.5)  # before backoff would elapse
    assert len(admitted) == 1


def test_waiting_tasks_snapshot_exposes_reason() -> None:
    """Issue #195 point 3: the status surface needs *why* a task is
    waiting, not just a bare count."""
    governor = ResourceGovernor(_limits(max_heavy_global=1, max_heavy_per_project=1))
    _request(governor, "held", "one")
    governor.enqueue(
        project_id="a",
        pane_id="a1",
        task_id="a1",
        resource_class=ResourceClass.HEAVY,
        reason="heavy_global_limit",
    )
    waiting = governor.snapshot()["waiting_tasks"]
    assert len(waiting) == 1
    assert waiting[0]["reason"] == "heavy_global_limit"


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
