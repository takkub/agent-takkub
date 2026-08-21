"""Idempotent task-delivery primitives.

The terminal UI is an observation surface, not an acknowledgement protocol.
This module keeps delivery identity, expiry and pane-session ownership outside
the TUI heuristics so retries can be conservative and stale work can be rejected
before it reaches a PTY.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path


class DeliveryState(StrEnum):
    QUEUED = "queued"
    WAITING_RESOURCE = "waiting_resource"
    SPAWNED_IDLE = "spawned_idle"
    WRITING = "writing"
    WRITTEN = "written"
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    UNCERTAIN = "uncertain"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


_SINGLE_FLIGHT_STATES = {
    DeliveryState.WRITING,
    DeliveryState.SUBMITTING,
    DeliveryState.UNCERTAIN,
}
_TERMINAL_STATES = {
    DeliveryState.DONE,
    DeliveryState.FAILED,
    DeliveryState.EXPIRED,
    DeliveryState.CANCELLED,
}
# States that represent an in-progress DELIVERY ATTEMPT — the paste/submit
# has not yet been confirmed to have landed. expire_stale() (issue #255) only
# reaps deliveries stuck in one of these: ACCEPTED/RUNNING/SPAWNED_IDLE mean
# the task WAS delivered and the teammate may now be working it for hours,
# so sweeping those by a TTL measured from creation would incorrectly cancel
# a delivery whose only remaining job is idempotent duplicate-paste
# prevention, not "being on time".
_IN_FLIGHT_STATES = {
    DeliveryState.QUEUED,
    DeliveryState.WAITING_RESOURCE,
    DeliveryState.WRITING,
    DeliveryState.WRITTEN,
    DeliveryState.SUBMITTING,
    DeliveryState.UNCERTAIN,
}

# States in which the task text has NOT yet been put in front of the pane —
# nothing has been written to the PTY at all (#295). The distinction matters
# because "cancel the pending delivery" means two opposite things: for a
# delivery that already pasted once, cancelling only suppresses an idempotent
# re-paste (harmless); for one of these, cancelling means the pane never
# receives the task and simply sits with no work.
_UNDELIVERED_STATES = {
    DeliveryState.QUEUED,
    DeliveryState.WAITING_RESOURCE,
}

# States in which we cannot PROVE the task reached the pane (#336). The two
# above never wrote anything; `UNCERTAIN` is the one that bites, because it
# means the paste went out but the confirmation signal came back ambiguous —
# "ready prompt + empty input box" reads the same whether the submit landed or
# is still sitting in the box (the same ambiguity that keeps auto-repaste
# switched off, #134/#328). Treating it as delivered let a `takkub send`
# supersede destroy the pane's only copy of its task and report the result as
# "ปลอดภัย ไม่ต้องทำอะไร": measured on a real session, gemini was superseded at
# 15:49:14 after going UNCERTAIN at 15:48:33 and its transcript never contained
# the task at all, while `takkub status` still said `working` for 4 more
# minutes.
_UNCONFIRMED_STATES = _UNDELIVERED_STATES | {DeliveryState.UNCERTAIN}


@dataclass(slots=True)
class TaskDelivery:
    delivery_id: str
    task_id: str
    project_id: str
    pane_id: str
    session_generation: int
    payload: str
    created_at: float
    expires_at: float
    state: DeliveryState = DeliveryState.QUEUED
    submit_attempts: int = 0
    enter_retries: int = 0

    @property
    def pane_session_key(self) -> tuple[str, str, int]:
        return self.project_id, self.pane_id, self.session_generation

    def expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at


def has_reached_pane(delivery: TaskDelivery) -> bool:
    """True once *delivery*'s task text is CONFIRMED in front of its pane.

    `WRITING` counts: the write is already in progress, so a second delivery
    of the same task would duplicate rather than deliver. `UNCERTAIN` does
    not (#336) — see `_UNCONFIRMED_STATES`. The asymmetry is deliberate: this
    predicate gates "is it safe to throw this delivery away", so an ambiguous
    signal has to answer no.
    """
    return DeliveryState(delivery.state) not in _UNCONFIRMED_STATES


class DeliveryManager:
    """Thread-safe delivery registry with a pane/session single-flight gate."""

    def __init__(
        self,
        *,
        default_ttl_sec: float | None = None,
        event_sink: Callable[[str, dict], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.default_ttl_sec = float(
            default_ttl_sec
            if default_ttl_sec is not None
            # #255: must outlast lead_inbox._delayed_enter_verified's own
            # busy-resend budget (_SUBMIT_BUSY_MAX_RESENDS x
            # _SUBMIT_VERIFY_GRACE_MS ≈ 90s — the self-heal window for a
            # slow-but-eventually-successful MCP boot), or every resend past
            # 30s both (a) gets its DeliveryManager state wrongly flipped to
            # EXPIRED by _reject_stale even though the CR write it's part of
            # still lands, and (b) risks the PTY writer thread's own
            # expires_at staleness check (pty_session.py PtyWriter.run)
            # silently dropping that same write outright if it happens to
            # sit in a congested queue. 120s gives ~30s of margin above the
            # ~90s worst case, well short of BUSY_WAIT_CEILING_SEC (1800s)
            # so a delivery genuinely stuck this long is still worth
            # reaping (see expire_stale below).
            else os.environ.get("TAKKUB_TASK_DELIVERY_TTL_SEC", "120")
        )
        self._clock = clock
        self._event_sink = event_sink
        self._deliveries: dict[str, TaskDelivery] = {}
        self._active: dict[tuple[str, str, int], str] = {}
        self._lock = threading.RLock()

    def _emit(self, event: str, delivery: TaskDelivery, **extra: object) -> None:
        if self._event_sink is None:
            return
        details = {
            "project_id": delivery.project_id,
            "pane_id": delivery.pane_id,
            "task_id": delivery.task_id,
            "delivery_id": delivery.delivery_id,
            "session_generation": delivery.session_generation,
            **extra,
        }
        try:
            self._event_sink(event, details)
        except Exception:
            pass

    def create(
        self,
        *,
        task_id: str,
        project_id: str,
        pane_id: str,
        session_generation: int,
        payload: str,
        ttl_sec: float | None = None,
        waiting_resource: bool = False,
    ) -> TaskDelivery:
        now = self._clock()
        ttl = self.default_ttl_sec if ttl_sec is None else float(ttl_sec)
        delivery = TaskDelivery(
            delivery_id=uuid.uuid4().hex,
            task_id=task_id or uuid.uuid4().hex,
            project_id=project_id,
            pane_id=pane_id,
            session_generation=int(session_generation),
            payload=payload,
            created_at=now,
            expires_at=now + max(0.0, ttl),
            state=(DeliveryState.WAITING_RESOURCE if waiting_resource else DeliveryState.QUEUED),
        )
        with self._lock:
            self._deliveries[delivery.delivery_id] = delivery
        self._emit("task_delivery_created", delivery)
        return delivery

    def get(self, delivery_id: str) -> TaskDelivery | None:
        with self._lock:
            return self._deliveries.get(delivery_id)

    def _release_active(self, delivery: TaskDelivery) -> None:
        if self._active.get(delivery.pane_session_key) == delivery.delivery_id:
            self._active.pop(delivery.pane_session_key, None)

    def _reject_stale(self, delivery: TaskDelivery, current_generation: int) -> bool:
        if delivery.state in _TERMINAL_STATES:
            return True
        if delivery.expired(self._clock()):
            delivery.state = DeliveryState.EXPIRED
            self._release_active(delivery)
            self._emit("task_delivery_expired", delivery)
            return True
        if delivery.session_generation != int(current_generation):
            delivery.state = DeliveryState.EXPIRED
            self._release_active(delivery)
            self._emit("task_delivery_expired", delivery, reason="stale_generation")
            return True
        return False

    def validate_for_write(self, delivery_id: str, current_generation: int) -> bool:
        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            return bool(
                delivery is not None and not self._reject_stale(delivery, current_generation)
            )

    def begin_write(self, delivery_id: str, current_generation: int) -> bool:
        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            if delivery is None or self._reject_stale(delivery, current_generation):
                return False
            owner = self._active.get(delivery.pane_session_key)
            if owner not in (None, delivery.delivery_id):
                prior = self._deliveries.get(owner)
                if prior is not None:
                    self._reject_stale(prior, current_generation)
                owner = self._active.get(delivery.pane_session_key)
            if owner not in (None, delivery.delivery_id):
                return False
            self._active[delivery.pane_session_key] = delivery.delivery_id
            delivery.state = DeliveryState.WRITING
        self._emit("task_delivery_write", delivery)
        return True

    def mark_written(self, delivery_id: str) -> bool:
        return self._set_state(delivery_id, DeliveryState.WRITTEN)

    def begin_submit(self, delivery_id: str, current_generation: int) -> bool:
        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            if delivery is None or self._reject_stale(delivery, current_generation):
                return False
            owner = self._active.get(delivery.pane_session_key)
            if owner not in (None, delivery.delivery_id):
                prior = self._deliveries.get(owner)
                if prior is not None:
                    self._reject_stale(prior, current_generation)
                owner = self._active.get(delivery.pane_session_key)
            if owner not in (None, delivery.delivery_id):
                return False
            self._active[delivery.pane_session_key] = delivery.delivery_id
            delivery.submit_attempts += 1
            delivery.state = DeliveryState.SUBMITTING
        self._emit("task_delivery_submit", delivery)
        return True

    def retry_enter(self, delivery_id: str, current_generation: int) -> bool:
        """Record a submit retry without ever re-emitting the task payload."""
        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            if delivery is None or self._reject_stale(delivery, current_generation):
                return False
            if self._active.get(delivery.pane_session_key) != delivery.delivery_id:
                return False
            delivery.enter_retries += 1
            delivery.submit_attempts += 1
            delivery.state = DeliveryState.SUBMITTING
        self._emit("task_delivery_submit", delivery, retry="enter")
        return True

    def mark_accepted(self, delivery_id: str) -> bool:
        return self._set_state(delivery_id, DeliveryState.ACCEPTED, release=True)

    def mark_uncertain(self, delivery_id: str) -> bool:
        return self._set_state(delivery_id, DeliveryState.UNCERTAIN)

    def mark_running(self, delivery_id: str) -> bool:
        return self._set_state(delivery_id, DeliveryState.RUNNING, release=True)

    def mark_spawned_idle(self, delivery_id: str) -> bool:
        return self._set_state(delivery_id, DeliveryState.SPAWNED_IDLE, release=True)

    def mark_done(self, delivery_id: str) -> bool:
        return self._set_state(delivery_id, DeliveryState.DONE, release=True)

    def mark_failed(self, delivery_id: str, reason: str = "") -> bool:
        """*reason* rides along on the emitted event, and callers must give
        one (#331).

        FAILED is reached from outcomes that are nothing alike: the task text
        never reached the pane (a real delivery failure), the pane was closed
        with a delivery still registered (routine), or the teammate ran the
        task and reported `takkub done --failed` (also routine — the delivery
        worked perfectly). Without a reason on the event they are one
        undifferentiated count, which is how the auto-issue reporter opened a
        bug over four ordinary FAILED task reports.
        """
        return self._set_state(delivery_id, DeliveryState.FAILED, release=True, reason=reason)

    def _set_state(
        self, delivery_id: str, state: DeliveryState, *, release: bool = False, **extra: object
    ) -> bool:
        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            if delivery is None:
                return False
            delivery.state = state
            if release or state not in _SINGLE_FLIGHT_STATES:
                self._release_active(delivery)
        # Drop empty extras so a caller that passes nothing produces exactly
        # the event payload it always did.
        self._emit(
            f"task_delivery_{state.value}", delivery, **{k: v for k, v in extra.items() if v}
        )
        return True

    def cancel_for_session(self, project_id: str, pane_id: str, session_generation: int) -> int:
        """Cancel EVERY pending delivery for this pane-session; returns how many.

        Unconditional on purpose — this backs the explicit `takkub cancel`
        verb and pane teardown, where "cancel it" is the caller's stated
        intent regardless of how far the delivery got. The selective variant
        used when Lead merely *sends* into a busy pane is
        :meth:`supersede_for_session` (#295).
        """
        cancelled, _kept = self._cancel_for_session(
            project_id, pane_id, session_generation, only_delivered=False
        )
        return len(cancelled)

    def supersede_for_session(
        self, project_id: str, pane_id: str, session_generation: int
    ) -> tuple[list[TaskDelivery], list[TaskDelivery]]:
        """Cancel only the deliveries whose task text already reached the pane.

        Returns ``(cancelled, kept_undelivered)``.

        #255 cancels a pane's pending deliveries when Lead sends into it by
        hand, because the old delivery's self-heal resend would otherwise
        re-paste the ORIGINAL task on top of whatever Lead just sent. That
        reasoning only holds for a delivery that has already pasted once —
        there is nothing to duplicate otherwise.

        #295/#336: a delivery still in QUEUED/WAITING_RESOURCE has never put
        the task in front of the pane, and an UNCERTAIN one cannot be shown
        to have done so, so cancelling it doesn't suppress a
        duplicate, it may destroy the only copy — the pane ends up with no work
        at all, and from Lead's side the "superseded" notice looked identical
        to the harmless case. Those are left armed so the task still lands;
        the caller is handed them so it can say so instead of staying quiet.
        """
        return self._cancel_for_session(
            project_id, pane_id, session_generation, only_delivered=True
        )

    def _cancel_for_session(
        self,
        project_id: str,
        pane_id: str,
        session_generation: int,
        *,
        only_delivered: bool,
    ) -> tuple[list[TaskDelivery], list[TaskDelivery]]:
        cancelled: list[TaskDelivery] = []
        kept: list[TaskDelivery] = []
        with self._lock:
            for delivery in self._deliveries.values():
                if (
                    delivery.project_id == project_id
                    and delivery.pane_id == pane_id
                    and delivery.session_generation == int(session_generation)
                    and delivery.state not in _TERMINAL_STATES
                ):
                    if only_delivered and not has_reached_pane(delivery):
                        kept.append(delivery)
                        continue
                    delivery.state = DeliveryState.CANCELLED
                    self._release_active(delivery)
                    cancelled.append(delivery)
        for delivery in cancelled:
            self._emit("task_delivery_cancelled", delivery)
        for delivery in kept:
            self._emit("task_delivery_kept_undelivered", delivery, state=str(delivery.state))
        return cancelled, kept

    def expire_stale(self) -> list[TaskDelivery]:
        """Reap deliveries stuck in an in-flight state (see
        ``_IN_FLIGHT_STATES``) past their TTL and return the ones just
        expired (issue #255's reaper — see ``Orchestrator._reap_stale_deliveries``,
        which wires this into the idle watchdog tick and tells Lead about
        each one instead of it going quiet until a duplicate paste surfaces
        it). Deliberately narrower than "any non-terminal state" (the old
        behaviour) — ACCEPTED/RUNNING/SPAWNED_IDLE mean the task already
        landed and may legitimately run for hours, so those must never be
        swept by a creation-time TTL."""
        expired: list[TaskDelivery] = []
        with self._lock:
            now = self._clock()
            for delivery in self._deliveries.values():
                if delivery.state in _IN_FLIGHT_STATES and delivery.expired(now):
                    delivery.state = DeliveryState.EXPIRED
                    self._release_active(delivery)
                    expired.append(delivery)
        for delivery in expired:
            self._emit("task_delivery_expired", delivery, reason="stale_reap")
        return expired

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [asdict(item) for item in self._deliveries.values()]


def make_notice_id(
    project_id: str,
    role: str,
    task_id: str,
    completion_generation: str | int,
) -> str:
    raw = "\0".join((project_id, role, task_id, str(completion_generation)))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


class NoticeDeduper:
    """Durable, TTL-bounded at-most-once registry for completion notices."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        ttl_sec: float = 7 * 24 * 60 * 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.ttl_sec = float(ttl_sec)
        self._clock = clock
        self._seen: dict[str, float] = {}
        self.duplicate_count = 0
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._seen = {str(k): float(v) for k, v in raw.items()}
        except (OSError, ValueError, TypeError):
            self._seen = {}
        self.prune(persist=False)

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._seen, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def prune(self, *, persist: bool = True) -> int:
        cutoff = self._clock() - self.ttl_sec
        with self._lock:
            stale = [key for key, ts in self._seen.items() if ts < cutoff]
            for key in stale:
                self._seen.pop(key, None)
            if stale and persist:
                self._persist()
        return len(stale)

    def seen(self, notice_id: str) -> bool:
        with self._lock:
            self.prune(persist=False)
            return notice_id in self._seen

    def mark_once(self, notice_id: str) -> bool:
        with self._lock:
            self.prune(persist=False)
            if notice_id in self._seen:
                self.duplicate_count += 1
                return False
            self._seen[notice_id] = self._clock()
            self._persist()
            return True
