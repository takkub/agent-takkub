from __future__ import annotations

from agent_takkub.task_delivery import (
    DeliveryManager,
    DeliveryState,
    NoticeDeduper,
    make_notice_id,
)


def _delivery(manager: DeliveryManager, generation: int = 1):
    return manager.create(
        task_id="task-1",
        project_id="project-a",
        pane_id="backend",
        session_generation=generation,
        payload="do work",
    )


def test_single_flight_per_pane_session() -> None:
    manager = DeliveryManager(default_ttl_sec=30)
    first = _delivery(manager)
    second = _delivery(manager)

    assert manager.begin_write(first.delivery_id, 1)
    assert not manager.begin_write(second.delivery_id, 1)
    assert manager.begin_submit(first.delivery_id, 1)
    assert not manager.begin_submit(second.delivery_id, 1)

    assert manager.mark_accepted(first.delivery_id)
    assert manager.begin_write(second.delivery_id, 1)


def test_old_generation_and_expired_delivery_never_write() -> None:
    now = [100.0]
    manager = DeliveryManager(default_ttl_sec=5, clock=lambda: now[0])
    stale_generation = _delivery(manager, generation=2)
    assert not manager.begin_write(stale_generation.delivery_id, 3)
    assert stale_generation.state is DeliveryState.EXPIRED

    expired = _delivery(manager, generation=3)
    now[0] = 106.0
    assert not manager.begin_write(expired.delivery_id, 3)
    assert expired.state is DeliveryState.EXPIRED


def test_retry_enter_never_changes_payload_or_creates_delivery() -> None:
    manager = DeliveryManager(default_ttl_sec=30)
    delivery = _delivery(manager)
    assert manager.begin_write(delivery.delivery_id, 1)
    assert manager.begin_submit(delivery.delivery_id, 1)
    assert manager.retry_enter(delivery.delivery_id, 1)
    assert delivery.payload == "do work"
    assert delivery.enter_retries == 1
    assert len(manager.snapshot()) == 1


def test_expired_uncertain_owner_does_not_block_next_delivery() -> None:
    now = [100.0]
    manager = DeliveryManager(default_ttl_sec=5, clock=lambda: now[0])
    first = _delivery(manager)
    assert manager.begin_write(first.delivery_id, 1)
    assert manager.mark_uncertain(first.delivery_id)
    now[0] = 106.0
    second = _delivery(manager)
    assert manager.begin_write(second.delivery_id, 1)
    assert first.state is DeliveryState.EXPIRED


def test_cancel_session_releases_single_flight() -> None:
    manager = DeliveryManager(default_ttl_sec=30)
    first = _delivery(manager)
    second = _delivery(manager)
    assert manager.begin_write(first.delivery_id, 1)
    assert manager.cancel_for_session("project-a", "backend", 1) == 2
    assert first.state is DeliveryState.CANCELLED
    assert second.state is DeliveryState.CANCELLED


def test_expire_stale_reaps_in_flight_deliveries_past_ttl() -> None:
    now = [100.0]
    manager = DeliveryManager(default_ttl_sec=5, clock=lambda: now[0])
    stuck = _delivery(manager)
    assert manager.begin_write(stuck.delivery_id, 1)
    assert manager.begin_submit(stuck.delivery_id, 1)
    now[0] = 106.0

    expired = manager.expire_stale()

    assert [d.delivery_id for d in expired] == [stuck.delivery_id]
    assert stuck.state is DeliveryState.EXPIRED


def test_expire_stale_never_reaps_accepted_or_running_delivery() -> None:
    """issue #255: ACCEPTED/RUNNING mean the task already landed and may
    legitimately run for hours — a creation-time TTL must never sweep it."""
    now = [100.0]
    manager = DeliveryManager(default_ttl_sec=5, clock=lambda: now[0])
    accepted = _delivery(manager)
    assert manager.begin_write(accepted.delivery_id, 1)
    assert manager.begin_submit(accepted.delivery_id, 1)
    assert manager.mark_accepted(accepted.delivery_id)
    running = _delivery(manager, generation=2)
    assert manager.mark_running(running.delivery_id)
    now[0] = 200.0  # far past the 5s TTL for both

    expired = manager.expire_stale()

    assert expired == []
    assert accepted.state is DeliveryState.ACCEPTED
    assert running.state is DeliveryState.RUNNING


def test_expire_stale_leaves_terminal_states_alone() -> None:
    now = [100.0]
    manager = DeliveryManager(default_ttl_sec=5, clock=lambda: now[0])
    done = _delivery(manager)
    assert manager.mark_done(done.delivery_id)
    now[0] = 200.0

    assert manager.expire_stale() == []
    assert done.state is DeliveryState.DONE


def test_notice_dedupe_is_durable_and_ttl_bounded(tmp_path) -> None:
    now = [100.0]
    path = tmp_path / "notice-dedupe.json"
    notice_id = make_notice_id("p", "qa", "task", 4)
    first = NoticeDeduper(path, ttl_sec=10, clock=lambda: now[0])
    assert first.mark_once(notice_id)
    assert not first.mark_once(notice_id)

    restored = NoticeDeduper(path, ttl_sec=10, clock=lambda: now[0])
    assert restored.seen(notice_id)
    now[0] = 111.0
    assert not restored.seen(notice_id)
    assert restored.mark_once(notice_id)
