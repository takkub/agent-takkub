from __future__ import annotations

from agent_takkub.resource_governor import (
    GovernorLimits,
    ResourceClass,
    ResourceGovernor,
    classify_resource,
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


def test_request_slot_reports_cpu_high_only_when_cpu_actually_over_pause() -> None:
    samples = iter([(90.0, 50.0, 10)])
    governor = ResourceGovernor(_limits(), sampler=lambda: next(samples))
    governor.sample()
    decision = _request(governor, "p", "pane")
    assert not decision.allowed
    assert decision.reason == "cpu_high"


def test_request_slot_reports_memory_low_when_ram_actually_under_pause() -> None:
    samples = iter([(29.0, 15.0, 10)])
    governor = ResourceGovernor(_limits(), sampler=lambda: next(samples))
    governor.sample()
    decision = _request(governor, "p", "pane")
    assert not decision.allowed
    assert decision.reason == "memory_low"


def test_request_slot_reports_waiting_resume_when_latched_but_neither_metric_over_pause() -> None:
    # Issue #274: RAM trips the latch (15% < 20% pause), then recovers to
    # 21.5% — above the 20% pause line but still below the stricter 25%
    # resume line — while CPU (29%) was never anywhere near either of its
    # own thresholds. The old code blamed "cpu_high" here unconditionally.
    samples = iter([(29.0, 15.0, 10), (29.0, 21.5, 10)])
    governor = ResourceGovernor(_limits(), sampler=lambda: next(samples))
    governor.sample()
    snap = governor.sample()
    assert snap["overloaded"]
    assert snap["overload_reason"] == "waiting_resume"
    decision = _request(governor, "p", "pane")
    assert not decision.allowed
    assert decision.reason == "waiting_resume"
    assert decision.reason != "cpu_high"


def test_request_slot_unblocks_once_both_resume_thresholds_clear() -> None:
    samples = iter([(29.0, 15.0, 10), (29.0, 30.0, 10)])
    governor = ResourceGovernor(_limits(), sampler=lambda: next(samples))
    governor.sample()
    snap = governor.sample()
    assert not snap["overloaded"]
    assert snap["overload_reason"] == ""
    decision = _request(governor, "p", "pane")
    assert decision.allowed


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


def test_holders_for_class_reports_pane_holding_the_slot() -> None:
    """Issue #240 point 3: a denied caller should be able to find out *who*
    it's waiting behind, not just the bare limit-name reason."""
    governor = ResourceGovernor(_limits())
    held = _request(governor, "a", "backend#1", ResourceClass.PACKAGE_INSTALL)
    assert held.allowed
    assert governor.holders_for_class(ResourceClass.PACKAGE_INSTALL) == ["backend#1"]
    assert governor.snapshot()["resource_holders"]["package_install"] == ["backend#1"]
    assert governor.holders_for_class(ResourceClass.BUILD) == []


def test_gate_block_heartbeat_throttles_long_running_waits() -> None:
    """Issue #240 point 4: #195's 1/2/5/15s backoff still floods events.log
    once it settles at its 15s floor for a task blocked a long time (field
    evidence: 1154 lines for one pane in a single wave). Ramp-up attempts
    (the first 4, already pinned by `test_gate_block_backoff_reduces_retry_frequency`)
    must still log every time; once an item is past the ramp, further lines
    are throttled to at most one per `_GATE_BLOCK_LOG_HEARTBEAT_S`."""
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

    # Ramp-up: t=1,2,4,9 (4 attempts) all log, per the pinned backoff test.
    # Steady state settles at a 15s retry cadence — walk out to t=90 (~6 more
    # retries at 15s) and expect the heartbeat to suppress most of them.
    for _ in range(90):
        clock[0] += 1.0
        governor.dispatch_waiting()

    block_events = [e for e, _ in events if e == "resource_gate_block"]
    # Without the heartbeat this would be ~10 lines (4 ramp-up + ~6 steady
    # 15s retries out to t=90); the 60s heartbeat caps steady-state logging
    # to roughly one per minute after the ramp, well under that.
    assert 4 <= len(block_events) <= 7, block_events
    # And the ramp-up prefix is untouched — first 4 lines still land at
    # exactly the pinned schedule.
    assert len(block_events) >= 4


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


# ─────────────────────────────────────────────────────────────
# Issue #240 point 1 — classifier must not fire on prohibition/negation
# sentences that merely *mention* a marker in order to forbid it.
# ─────────────────────────────────────────────────────────────


def test_classify_ignores_prohibition_sentence_from_real_task_spec() -> None:
    """Verbatim from issue #240's own field report — the exact sentence that
    caused a 3-pane parallel wave to serialize on wave 1 of agent-takkub
    itself, 2026-08-15 06:51-06:54: a task spec written to FORBID `pip
    install -e .` was itself classified as a package-install task."""
    task = (
        "ก่อนแก้ไฟล์ใน src/agent_takkub/ อ่าน docs/architecture/godfile-map.md ก่อน\n"
        "ห้ามรัน `pip install -e .` เด็ดขาด (จะไป repoint venv ของ repo หลัก — บั๊ก #202)\n"
        "commit ใน worktree ของตัวเอง ห้าม push ห้ามแตะ main"
    )
    assert classify_resource("backend#2", task) != ResourceClass.PACKAGE_INSTALL


def test_classify_ignores_prohibition_sentence_from_this_sessions_own_task() -> None:
    """Same shape, drawn from the Lead's own spawn-prompt boilerplate used
    across every backend task in this project (not just the #240 repro) —
    a second independent fixture proving the fix generalizes."""
    task = (
        "**ห้ามติดตั้ง venv ซ้ำแบบ editable (คำสั่งตระกูล `pip install -e`) เด็ดขาด** "
        "— จะไป repoint venv ของ repo หลัก (บั๊ก #202)"
    )
    assert classify_resource("backend#3", task) != ResourceClass.PACKAGE_INSTALL


def test_classify_ignores_english_negation_cues() -> None:
    for phrasing in (
        "Don't run `npm install` in this repo, it will corrupt the lockfile.",
        "Never run pip install here — use the shared venv instead.",
        "Avoid npm ci during this task; the deps are already synced.",
    ):
        assert classify_resource("backend#1", phrasing) != ResourceClass.PACKAGE_INSTALL, phrasing


def test_classify_still_detects_genuine_install_instruction() -> None:
    """The negation filter must not swallow real instructions — a positive
    control alongside the negation fixtures above."""
    assert (
        classify_resource("backend#1", "รัน `pip install -r requirements.txt` ก่อนเริ่มงาน")
        == ResourceClass.PACKAGE_INSTALL
    )
    assert (
        classify_resource("frontend#1", "run `npm install` then start the dev server")
        == ResourceClass.PACKAGE_INSTALL
    )


def test_classify_negation_on_one_line_does_not_suppress_marker_on_another() -> None:
    """A prohibition earlier in a multi-line spec must not blind the
    classifier to a genuine install instruction on a different line."""
    task = "ห้ามรัน `pip install -e .` เด็ดขาด\nแต่ต้องรัน `npm install` ก่อน build ปกติ"
    assert classify_resource("frontend#1", task) == ResourceClass.PACKAGE_INSTALL
