"""Application-wide resource admission and fair waiting queue."""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import psutil

from . import performance_settings
from .core.scheduling import facade as scheduling_facade
from .core.scheduling.backpressure import BackpressureSignal
from .core.scheduling.models import ActiveCounts, Priority, SlotPolicy, SlotRequest


class ResourceClass(StrEnum):
    LIGHT = "light"
    NORMAL = "normal"
    HEAVY = "heavy"
    BROWSER = "browser"
    BUILD = "build"
    TEST = "test"
    PACKAGE_INSTALL = "package_install"


@dataclass(frozen=True, slots=True)
class GovernorLimits:
    max_heavy_global: int = 4
    max_heavy_per_project: int = 2
    max_browser_global: int = 2
    max_build_global: int = 2
    max_test_global: int = 2
    max_package_install_global: int = 1
    cpu_pause_percent: float = 85.0
    cpu_resume_percent: float = 65.0
    min_available_ram_percent: float = 20.0
    resume_ram_percent: float = 25.0
    # overload_deadband_timeout_s (#305): a latch can sit forever in the gap
    # between cpu_resume_percent/min_available_ram_percent (either metric
    # already below ITS OWN pause line) and the stricter both-must-hold
    # resume_ram_percent/cpu_resume_percent condition below — e.g. available
    # RAM parked at 23% (above min_available=20 but below resume_ram=25)
    # with CPU idle. See `ResourceGovernor.sample()`'s dead-band handling.
    overload_deadband_timeout_s: float = 120.0

    @classmethod
    def from_environment(cls) -> GovernorLimits:
        persisted = performance_settings.load()

        def _int(name: str, default: int) -> int:
            try:
                return max(1, int(os.environ.get(name, default)))
            except (TypeError, ValueError):
                return default

        def _float(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default

        return cls(
            max_heavy_global=_int("TAKKUB_MAX_HEAVY_GLOBAL", persisted.max_heavy_global),
            max_heavy_per_project=_int(
                "TAKKUB_MAX_HEAVY_PER_PROJECT", persisted.max_heavy_per_project
            ),
            max_browser_global=_int("TAKKUB_MAX_BROWSER_GLOBAL", persisted.max_browser_global),
            max_build_global=_int("TAKKUB_MAX_BUILD_GLOBAL", persisted.max_build_global),
            max_test_global=_int("TAKKUB_MAX_TEST_GLOBAL", persisted.max_test_global),
            max_package_install_global=_int(
                "TAKKUB_MAX_PACKAGE_INSTALL_GLOBAL", persisted.max_package_install_global
            ),
            cpu_pause_percent=_float("TAKKUB_CPU_PAUSE_PERCENT", persisted.cpu_pause_percent),
            cpu_resume_percent=_float("TAKKUB_CPU_RESUME_PERCENT", persisted.cpu_resume_percent),
            min_available_ram_percent=_float(
                "TAKKUB_MIN_AVAILABLE_RAM_PERCENT", persisted.min_available_ram_percent
            ),
            resume_ram_percent=_float("TAKKUB_RESUME_RAM_PERCENT", persisted.resume_ram_percent),
            overload_deadband_timeout_s=_float(
                "TAKKUB_OVERLOAD_DEADBAND_TIMEOUT_S", persisted.overload_deadband_timeout_s
            ),
        )


@dataclass(frozen=True, slots=True)
class ResourceToken:
    token_id: str
    project_id: str
    pane_id: str
    task_id: str
    resource_class: ResourceClass
    acquired_at: float
    # provider_id/account_id (epic #309 Phase 8a): optional, additive — only
    # populated when a caller opts into `core.scheduling`'s provider/account
    # dimensions; every existing caller leaves these None.
    provider_id: str | None = None
    account_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    allowed: bool
    reason: str = ""
    retry_after_ms: int = 1000
    token: ResourceToken | None = None


@dataclass(slots=True)
class QueuedTask:
    queue_id: str
    project_id: str
    pane_id: str
    task_id: str
    resource_class: ResourceClass
    queued_at: float
    on_admitted: Callable[[ResourceToken], None] | None = None
    # attempts / next_retry_at (issue #195): backoff bookkeeping for the
    # `dispatch_waiting` retry loop. attempts counts denials seen INSIDE the
    # loop only (the initial denial that caused the enqueue is not counted
    # here — it's logged unconditionally by the `request_slot` call in
    # `assign()`). next_retry_at=0.0 means "eligible for the very next
    # dispatch_waiting pass" — a freshly freed slot must still be grabbed
    # promptly, not delayed by an up-front backoff.
    attempts: int = 0
    next_retry_at: float = 0.0
    # reason: most recent denial reason, kept for status/UI surfacing
    # (`ResourceGovernor.snapshot()`'s waiting_tasks) — issue #195 point 3.
    reason: str = ""
    # retry_epoch (issue #201): the governor's `_capacity_epoch` value at the
    # moment this item's backoff was set. If the epoch has since advanced
    # (a slot was released or limits changed) the time-based backoff below
    # is stale and must not gate the next dispatch attempt.
    retry_epoch: int = -1
    # last_gate_block_log_at (issue #240 point 4): wall-clock time of the
    # most recent `resource_gate_block` line actually emitted for this item.
    # Initialized to `queued_at` in `enqueue()` — `assign()` already logs one
    # unconditionally the moment the initial denial happens, before the item
    # is even enqueued, so the heartbeat window starts counting from there.
    last_gate_block_log_at: float = 0.0
    # provider_id/account_id/priority (epic #309 Phase 8a): additive —
    # default None/NORMAL leaves every existing caller's FIFO-within-project
    # ordering untouched; only used when TAKKUB_V2_SCHEDULER is on.
    provider_id: str | None = None
    account_id: str | None = None
    priority: Priority = Priority.NORMAL


# Issue #195: backoff schedule (seconds) for the queued-task retry loop in
# `dispatch_waiting`. A denied item used to be retried unconditionally on
# every 1s governor tick, re-emitting an identical resource_gate_block line
# every second for as long as it stayed blocked (proven: 15 lines in 15s for
# one task). Spacing retries out this way still notices freed capacity
# within 15s worst case, and the log line frequency naturally follows the
# same schedule since a line is only emitted when an attempt is actually made.
_GATE_RETRY_BACKOFF_S: tuple[float, ...] = (1.0, 2.0, 5.0, 15.0)

# Issue #240 point 4: the #195 backoff above still floods events.log for a
# task blocked a *long* time — once the backoff settles at its 15s floor, a
# multi-hour wait still emits one resource_gate_block line every 15s forever
# (proven in the field: 1154 lines for one pane in a single wave). The ramp-up
# attempts (covered by `_GATE_RETRY_BACKOFF_S` itself) still log every time so
# Lead sees the block happen promptly; once an item has cycled through the
# whole ramp, further lines are throttled to at most one per this interval —
# a heartbeat instead of a per-retry flood. The final unblock is always
# summarized separately by `resource_gate_unblocked` regardless of how many
# attempts were suppressed here.
_GATE_BLOCK_LOG_HEARTBEAT_S: float = 60.0

# Issue #240 point 1: substrings that flag a task's resource class from raw
# task text. A prohibition sentence ("ห้ามรัน `pip install -e .` เด็ดขาด") that
# *mentions* one of these markers in order to forbid it was being counted as
# a signal that the task WOULD do it — see `_marker_signals` below.
_NEGATION_CUES: tuple[str, ...] = (
    "ห้าม",
    "อย่า",
    "ไม่ควร",
    "ไม่ต้อง",
    "don't",
    "do not",
    "never",
    "avoid",
    "must not",
    "mustn't",
    "shouldn't",
    "should not",
)


def _line_has_negation_cue(line: str) -> bool:
    return any(cue in line for cue in _NEGATION_CUES)


def _marker_signals(text: str, markers: tuple[str, ...]) -> bool:
    """True if any of `markers` appears in `text` on a line that isn't a
    prohibition/negation about it. `text` is expected already-lowercased
    (the negation cues below are matched against it as-is, and Thai text is
    unaffected by ``.lower()``), scanned line by line so a forbidden-command
    sentence elsewhere in a multi-line task spec never taints an unrelated
    instruction line."""
    for line in text.splitlines():
        if any(marker in line for marker in markers) and not _line_has_negation_cue(line):
            return True
    return False


_HEAVY_CLASSES = {
    ResourceClass.HEAVY,
    ResourceClass.BROWSER,
    ResourceClass.BUILD,
    ResourceClass.TEST,
    ResourceClass.PACKAGE_INSTALL,
}


class ResourceGovernor:
    """Non-blocking admission controller with hysteresis and project fairness.

    ``sample`` uses ``psutil.cpu_percent(interval=None)`` exclusively, so it is
    safe to call from a Qt timer. Waiting callbacks are invoked by ``dispatch_waiting``
    in the caller's thread; the cockpit calls it from the GUI thread.
    """

    def __init__(
        self,
        limits: GovernorLimits | None = None,
        *,
        sampler: Callable[[], tuple] | None = None,
        clock: Callable[[], float] = time.time,
        event_sink: Callable[[str, dict], None] | None = None,
        slot_policy: SlotPolicy | None = None,
    ) -> None:
        self.limits = limits or GovernorLimits.from_environment()
        # slot_policy (epic #309 Phase 8a): all-defaults `SlotPolicy()` caps
        # nothing (see core.scheduling.models), and `_denial_reason` only
        # ever consults it via the flag-gated `scheduling_facade` — so a
        # governor built without one is byte-identical to pre-Phase-8a.
        # A caller-supplied policy always wins; otherwise fall back to the
        # Settings UI's persisted Scheduler Policy (flag-gated, fail-open to
        # the same `SlotPolicy()` default — see `effective_slot_policy`'s
        # own docstring).
        self.slot_policy = slot_policy or scheduling_facade.effective_slot_policy()
        self._sampler = sampler or self._sample_psutil
        self._clock = clock
        self._event_sink = event_sink
        self._cpu_percent = 0.0
        self._available_ram_percent = 100.0
        self._process_count = 0
        self._available_memory_bytes = 0
        self._total_memory_bytes = 0
        self._overloaded = False
        # deadband_since (#305): wall-clock time this latch entered the
        # dead-band (neither metric over its own pause line, yet the
        # both-must-hold resume condition still unmet) — None whenever not
        # currently in that state. See `sample()`.
        self._deadband_since: float | None = None
        self._tokens: dict[str, ResourceToken] = {}
        self._waiting: OrderedDict[str, deque[QueuedTask]] = OrderedDict()
        self._project_cursor = 0
        self._lock = threading.RLock()
        # capacity_epoch (issue #201): bumped whenever a slot is actually
        # freed or limits change — i.e. whenever "there might be room now"
        # becomes true. dispatch_waiting uses it to tell a queue head that
        # is merely time-backed-off apart from one that is stale-backed-off
        # (its next_retry_at was computed against capacity that no longer
        # reflects reality), so a freed slot is never left idle for up to
        # 15s waiting on a clock instead of the event that actually matters.
        self._capacity_epoch = 0
        # Warm psutil's non-blocking CPU sampler; the first value is undefined.
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

    @staticmethod
    def _sample_psutil() -> tuple[float, float, int, int, int]:
        cpu = float(psutil.cpu_percent(interval=None))
        vm = psutil.virtual_memory()
        available_pct = 100.0 * float(vm.available) / max(1.0, float(vm.total))
        try:
            processes = len(psutil.pids())
        except Exception:
            processes = 0
        return cpu, available_pct, processes, int(vm.available), int(vm.total)

    def _emit(self, event: str, **details: object) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(event, details)
        except Exception:
            pass

    def update_limits(self, limits: GovernorLimits) -> None:
        """Apply new limits without dropping active tokens or queued work."""
        with self._lock:
            self.limits = limits
            self._capacity_epoch += 1
        self._emit(
            "resource_limits_updated",
            limits={field: getattr(limits, field) for field in limits.__dataclass_fields__},
        )

    def sample(self) -> dict:
        try:
            sampled = self._sampler()
            cpu, ram, processes = sampled[:3]
            available_bytes = int(sampled[3]) if len(sampled) >= 5 else self._available_memory_bytes
            total_bytes = int(sampled[4]) if len(sampled) >= 5 else self._total_memory_bytes
        except Exception:
            cpu, ram, processes = (
                self._cpu_percent,
                self._available_ram_percent,
                self._process_count,
            )
            available_bytes = self._available_memory_bytes
            total_bytes = self._total_memory_bytes
        with self._lock:
            self._cpu_percent = max(0.0, min(100.0, float(cpu)))
            self._available_ram_percent = max(0.0, min(100.0, float(ram)))
            self._process_count = max(0, int(processes))
            self._available_memory_bytes = max(0, available_bytes)
            self._total_memory_bytes = max(0, total_bytes)
            if self._overloaded:
                if (
                    self._cpu_percent <= self.limits.cpu_resume_percent
                    and self._available_ram_percent >= self.limits.resume_ram_percent
                ):
                    self._overloaded = False
                    self._deadband_since = None
                elif (
                    self._cpu_percent < self.limits.cpu_pause_percent
                    and self._available_ram_percent >= self.limits.min_available_ram_percent
                ):
                    # Dead-band (#305): neither metric is over ITS OWN pause
                    # line anymore (this is exactly `_overload_state_reason`'s
                    # "waiting_resume" case), but the stricter both-must-hold
                    # resume condition above isn't satisfied either — e.g.
                    # available RAM parked between min_available_ram_percent
                    # and resume_ram_percent. Left alone this never clears on
                    # its own. Escape hatch: once the machine has sat in this
                    # zone continuously for overload_deadband_timeout_s,
                    # release the latch — the zone's own definition already
                    # proves neither metric is actually breaching its limit.
                    now = self._clock()
                    if self._deadband_since is None:
                        self._deadband_since = now
                    elif now - self._deadband_since >= self.limits.overload_deadband_timeout_s:
                        waited_s = now - self._deadband_since
                        self._overloaded = False
                        self._deadband_since = None
                        self._emit(
                            "overload_deadband_released",
                            cpu_percent=self._cpu_percent,
                            available_ram_percent=self._available_ram_percent,
                            waited_s=round(waited_s, 1),
                        )
                else:
                    # Actively over one metric's own pause line — not a
                    # dead-band wait, so any in-progress dead-band timer is
                    # stale and must not carry over once that metric clears.
                    self._deadband_since = None
            elif (
                self._cpu_percent >= self.limits.cpu_pause_percent
                or self._available_ram_percent < self.limits.min_available_ram_percent
            ):
                self._overloaded = True
                self._deadband_since = None
            return self.snapshot()

    def overloaded(self) -> bool:
        with self._lock:
            return self._overloaded

    def current_metrics(self) -> tuple[float, float]:
        """Latest sampled ``(cpu_percent, available_ram_percent)`` — a cheap
        accessor for callers (#305's status-header wording) that only need
        the numbers behind an overload reason, without `snapshot()`'s full
        token/queue bookkeeping."""
        with self._lock:
            return self._cpu_percent, self._available_ram_percent

    def _counts(
        self,
    ) -> tuple[int, dict[str, int], dict[ResourceClass, int], dict[ResourceClass, list[str]]]:
        heavy = 0
        by_project: dict[str, int] = {}
        by_class: dict[ResourceClass, int] = {}
        holders_by_class: dict[ResourceClass, list[str]] = {}
        for token in self._tokens.values():
            by_class[token.resource_class] = by_class.get(token.resource_class, 0) + 1
            holders_by_class.setdefault(token.resource_class, []).append(token.pane_id)
            if token.resource_class in _HEAVY_CLASSES:
                heavy += 1
                by_project[token.project_id] = by_project.get(token.project_id, 0) + 1
        return heavy, by_project, by_class, holders_by_class

    def _active_counts(self) -> ActiveCounts:
        """Snapshot for `core.scheduling.policy.evaluate` (epic #309 Phase
        8a) — one active token is treated as both one agent and one pane
        (see `ActiveCounts`'s `ponytail:` note)."""
        total = len(self._tokens)
        by_provider: dict[str, int] = {}
        by_account: dict[str, int] = {}
        by_project: dict[str, int] = {}
        for token in self._tokens.values():
            if token.provider_id is not None:
                by_provider[token.provider_id] = by_provider.get(token.provider_id, 0) + 1
            if token.account_id is not None:
                by_account[token.account_id] = by_account.get(token.account_id, 0) + 1
            by_project[token.project_id] = by_project.get(token.project_id, 0) + 1
        return ActiveCounts(
            agents_global=total,
            panes_global=total,
            by_provider=by_provider,
            by_account=by_account,
            by_project_agents=by_project,
            by_project_panes=by_project,
        )

    def holders_for_class(self, resource_class: ResourceClass) -> list[tuple[str, str]]:
        """(project_id, pane_id) pairs currently holding a token of
        `resource_class` (#240 point 3, extended #303). Global classes
        (BROWSER/BUILD/TEST/...) are capped machine-wide, so a holder is
        routinely a DIFFERENT project than the caller's own — the bare
        pane-id-only version of this used to render as e.g. "blocked by
        qa#1, qa", which reads exactly like a stale lock from the caller's
        own project and cost a live incident's worth of confused debugging
        before anyone thought to check `runtime/events.log` by hand. The
        project id travels with each holder now so a caller can tell the
        two cases apart."""
        with self._lock:
            return [
                (t.project_id, t.pane_id)
                for t in self._tokens.values()
                if t.resource_class == resource_class
            ]

    def queue_length_for_class(self, resource_class: ResourceClass) -> int:
        """Total items waiting on `resource_class` across every project
        (#303) — a queued role's own status line otherwise gives no sense of
        how deep the machine-wide queue actually is, just that it's blocked."""
        with self._lock:
            return sum(
                1
                for queue in self._waiting.values()
                for item in queue
                if item.resource_class == resource_class
            )

    def _overload_state_reason(self) -> str:
        """Why the overload latch is currently held (issue #274): the old
        code returned "cpu_high" as an unconditional fallback whenever RAM
        wasn't below its pause line, even when CPU was nowhere near its own
        pause line either — e.g. CPU 29%/RAM 21.5% reported "cpu_high" for a
        latch that RAM itself had triggered minutes earlier. `sample()`'s
        hysteresis enters overload when CPU >= cpu_pause_percent OR RAM <
        min_available_ram_percent, but only exits when CPU <= cpu_resume_percent
        AND RAM >= resume_ram_percent (both, not either) — so a latch commonly
        outlives the metric that originally tripped it while the *other*
        metric is still catching up to its (stricter) resume line. Report the
        metric that is actually over ITS OWN pause threshold right now; if
        neither is, the latch is just waiting on the resume side of the
        hysteresis gap."""
        if self._cpu_percent >= self.limits.cpu_pause_percent:
            return "cpu_high"
        if self._available_ram_percent < self.limits.min_available_ram_percent:
            return "memory_low"
        return "waiting_resume"

    def _denial_reason(
        self,
        project_id: str,
        resource_class: ResourceClass,
        *,
        pane_id: str = "",
        task_id: str = "",
        provider_id: str | None = None,
        account_id: str | None = None,
        priority: Priority = Priority.NORMAL,
    ) -> str:
        if self._overloaded:
            # Overload latch still gates HEAVY-class work unconditionally
            # (unchanged legacy behavior); Phase 8a's backpressure ladder
            # additionally lets LIGHT/NORMAL-class low-priority work be
            # throttled once overloaded, via the facade check below.
            if resource_class not in {ResourceClass.LIGHT, ResourceClass.NORMAL}:
                return self._overload_state_reason()
        elif resource_class not in {ResourceClass.LIGHT, ResourceClass.NORMAL}:
            heavy, by_project, by_class, _holders = self._counts()
            if heavy >= self.limits.max_heavy_global:
                return "heavy_global_limit"
            if by_project.get(project_id, 0) >= self.limits.max_heavy_per_project:
                return "heavy_project_limit"
            class_limits = {
                ResourceClass.BROWSER: self.limits.max_browser_global,
                ResourceClass.BUILD: self.limits.max_build_global,
                ResourceClass.TEST: self.limits.max_test_global,
                ResourceClass.PACKAGE_INSTALL: self.limits.max_package_install_global,
            }
            limit = class_limits.get(resource_class)
            if limit is not None and by_class.get(resource_class, 0) >= limit:
                return f"{resource_class.value}_global_limit"

        signal = BackpressureSignal(
            cpu_percent=self._cpu_percent,
            available_ram_percent=self._available_ram_percent,
            overloaded=self._overloaded,
            cpu_pause_percent=self.limits.cpu_pause_percent,
            cpu_resume_percent=self.limits.cpu_resume_percent,
            min_available_ram_percent=self.limits.min_available_ram_percent,
            queued_count=sum(len(queue) for queue in self._waiting.values()),
        )
        level = scheduling_facade.backpressure_level(signal)
        if not scheduling_facade.backpressure_admits(level, priority):
            return f"backpressure_{level.value}"

        extended = scheduling_facade.extended_denial_reason(
            SlotRequest(
                project_id=project_id,
                pane_id=pane_id,
                task_id=task_id,
                provider_id=provider_id,
                account_id=account_id,
                priority=priority,
            ),
            self._active_counts(),
            self.slot_policy,
        )
        if extended:
            return extended
        return ""

    def request_slot(
        self,
        *,
        project_id: str,
        pane_id: str,
        resource_class: ResourceClass,
        task_id: str,
        emit_on_deny: bool = True,
        provider_id: str | None = None,
        account_id: str | None = None,
        priority: Priority = Priority.NORMAL,
    ) -> AdmissionDecision:
        with self._lock:
            reason = self._denial_reason(
                project_id,
                resource_class,
                pane_id=pane_id,
                task_id=task_id,
                provider_id=provider_id,
                account_id=account_id,
                priority=priority,
            )
            if reason:
                if emit_on_deny:
                    self._emit(
                        "resource_gate_block",
                        project_id=project_id,
                        pane_id=pane_id,
                        task_id=task_id,
                        resource_class=resource_class.value,
                        reason=reason,
                    )
                return AdmissionDecision(False, reason)
            token = ResourceToken(
                uuid.uuid4().hex,
                project_id,
                pane_id,
                task_id,
                resource_class,
                self._clock(),
                provider_id=provider_id,
                account_id=account_id,
            )
            self._tokens[token.token_id] = token
        self._emit(
            "resource_slot_acquired",
            token_id=token.token_id,
            project_id=project_id,
            pane_id=pane_id,
            task_id=task_id,
            resource_class=resource_class.value,
        )
        return AdmissionDecision(True, token=token)

    def release_slot(self, token: ResourceToken | str | None) -> bool:
        if token is None:
            return False
        token_id = token if isinstance(token, str) else token.token_id
        with self._lock:
            released = self._tokens.pop(token_id, None)
            if released is not None:
                self._capacity_epoch += 1
        if released is not None:
            self._emit(
                "resource_slot_released",
                token_id=released.token_id,
                project_id=released.project_id,
            )
        return released is not None

    def enqueue(
        self,
        *,
        project_id: str,
        pane_id: str,
        task_id: str,
        resource_class: ResourceClass,
        reason: str = "",
        on_admitted: Callable[[ResourceToken], None] | None = None,
        provider_id: str | None = None,
        account_id: str | None = None,
        priority: Priority = Priority.NORMAL,
    ) -> str:
        queued_at = self._clock()
        item = QueuedTask(
            uuid.uuid4().hex,
            project_id,
            pane_id,
            task_id,
            resource_class,
            queued_at,
            on_admitted,
            provider_id=provider_id,
            account_id=account_id,
            priority=priority,
        )
        item.reason = reason
        item.last_gate_block_log_at = queued_at
        with self._lock:
            self._waiting.setdefault(project_id, deque()).append(item)
        self._emit(
            "resource_task_queued",
            queue_id=item.queue_id,
            project_id=project_id,
            pane_id=pane_id,
            task_id=task_id,
            resource_class=resource_class.value,
            reason=reason,
        )
        return item.queue_id

    def cancel_waiting(
        self,
        *,
        project_id: str,
        pane_id: str | None = None,
        task_id: str | None = None,
    ) -> int:
        removed = 0
        with self._lock:
            queue = self._waiting.get(project_id)
            if queue is None:
                return 0
            kept: deque[QueuedTask] = deque()
            for item in queue:
                if (pane_id is None or item.pane_id == pane_id) and (
                    task_id is None or item.task_id == task_id
                ):
                    removed += 1
                else:
                    kept.append(item)
            if kept:
                self._waiting[project_id] = kept
            else:
                self._waiting.pop(project_id, None)
        if removed:
            self._emit(
                "resource_tasks_cancelled",
                project_id=project_id,
                pane_id=pane_id,
                task_id=task_id,
                count=removed,
            )
        return removed

    def dispatch_waiting(
        self, max_dispatch: int | None = None, *, now: float | None = None
    ) -> list[ResourceToken]:
        """Admit waiting work round-robin by project, FIFO within a project.

        Issue #195: a queue head whose `next_retry_at` hasn't elapsed yet is
        skipped without calling `request_slot` (so no `resource_gate_block`
        line is emitted either) — this is what turns the retry cadence from
        an unconditional 1s poll into the 1s/2s/5s/15s backoff schedule, since
        a line only ever gets logged when an attempt is actually made.

        Issue #201: that time-based skip alone left freed slots idle for up
        to 15s — a queue head's backoff is only honored while it was set
        under the SAME `_capacity_epoch` as now. A slot release (or a limits
        change) bumps the epoch, which makes every backed-off item stale and
        eligible for an immediate retry on the very next call regardless of
        `next_retry_at`, without touching the 1s/2s/5s/15s cadence that
        applies while capacity is genuinely unchanged (`ponytail:` this is a
        single global epoch rather than per-project/per-class — coarser than
        strictly necessary, so a release can trigger a futile retry+log for
        an unrelated queue, but bounded by actual release events rather than
        wall-clock ticks; upgrade to a per-resource-class epoch if that proves
        noisy in practice).
        """
        now = self._clock() if now is None else now
        admitted: list[tuple[QueuedTask, ResourceToken]] = []
        with self._lock:
            epoch = self._capacity_epoch
            projects = list(self._waiting)
            if projects:
                start = self._project_cursor % len(projects)
                projects = projects[start:] + projects[:start]
                # Phase 8a priority ordering (§8): a no-op stable re-sort
                # while TAKKUB_V2_SCHEDULER is off (facade returns the list
                # unchanged) — see core.scheduling.priority_queue.
                projects = scheduling_facade.order_projects(
                    projects,
                    lambda pid: self._waiting[pid][0].priority,
                )
            progress = True
            while projects and progress and (max_dispatch is None or len(admitted) < max_dispatch):
                progress = False
                for project_id in list(projects):
                    if max_dispatch is not None and len(admitted) >= max_dispatch:
                        break
                    queue = self._waiting.get(project_id)
                    if not queue:
                        continue
                    item = queue[0]
                    if item.next_retry_at > now and item.retry_epoch == epoch:
                        continue
                    # Issue #240 point 4: once an item has cycled through the
                    # whole ramp-up schedule, further resource_gate_block
                    # lines are throttled to a heartbeat instead of logging
                    # every single retry — the ramp-up itself (attempts within
                    # len(_GATE_RETRY_BACKOFF_S)) always logs so a fresh block
                    # is still immediately visible.
                    should_log = (
                        item.attempts + 1 <= len(_GATE_RETRY_BACKOFF_S)
                        or (now - item.last_gate_block_log_at) >= _GATE_BLOCK_LOG_HEARTBEAT_S
                    )
                    decision = self.request_slot(
                        project_id=item.project_id,
                        pane_id=item.pane_id,
                        task_id=item.task_id,
                        resource_class=item.resource_class,
                        emit_on_deny=should_log,
                        provider_id=item.provider_id,
                        account_id=item.account_id,
                        priority=item.priority,
                    )
                    if decision.allowed and decision.token is not None:
                        queue.popleft()
                        if not queue:
                            self._waiting.pop(project_id, None)
                        admitted.append((item, decision.token))
                        progress = True
                    else:
                        item.attempts += 1
                        delay = _GATE_RETRY_BACKOFF_S[
                            min(item.attempts, len(_GATE_RETRY_BACKOFF_S)) - 1
                        ]
                        item.next_retry_at = now + delay
                        item.retry_epoch = epoch
                        item.reason = decision.reason
                        if should_log:
                            item.last_gate_block_log_at = now
                self._project_cursor += 1
        for item, token in admitted:
            # Summary line replacing the per-retry flood (issue #195 point 2):
            # one entry per unblock, however many attempts it took.
            self._emit(
                "resource_gate_unblocked",
                project_id=item.project_id,
                pane_id=item.pane_id,
                task_id=item.task_id,
                resource_class=item.resource_class.value,
                blocked_for_s=round(now - item.queued_at, 1),
                attempts=item.attempts,
            )
            if item.on_admitted is not None:
                try:
                    item.on_admitted(token)
                except Exception:
                    self.release_slot(token)
        return [token for _, token in admitted]

    def snapshot(self) -> dict:
        with self._lock:
            heavy, by_project, by_class, holders_by_class = self._counts()
            return {
                "cpu_percent": self._cpu_percent,
                "available_memory_percent": self._available_ram_percent,
                "available_memory_bytes": self._available_memory_bytes,
                "total_memory_bytes": self._total_memory_bytes,
                "process_count": self._process_count,
                "overloaded": self._overloaded,
                # overload_reason (#274): the system-wide reason the latch is
                # held, independent of any one queued task's resource class —
                # lets the Performance Health dialog explain the OVERLOAD
                # banner instead of leaving the user to infer it.
                "overload_reason": self._overload_state_reason() if self._overloaded else "",
                "active_heavy_tasks": heavy,
                "active_tasks_by_project": by_project,
                "active_tasks_by_class": {key.value: value for key, value in by_class.items()},
                # #240 point 3: lets a caller tell a queued Lead/CLI message
                # *who* currently holds the slot a task is waiting behind.
                "resource_holders": {
                    key.value: list(panes) for key, panes in holders_by_class.items()
                },
                "queued_resource_tasks": sum(len(queue) for queue in self._waiting.values()),
                "waiting_tasks": [
                    {
                        "project_id": item.project_id,
                        "pane_id": item.pane_id,
                        "task_id": item.task_id,
                        "resource_class": item.resource_class.value,
                        "queued_at": item.queued_at,
                        # reason/attempts (issue #195 point 3): lets the status
                        # bar/pane surface *why* a task is waiting instead of
                        # just a bare queue count.
                        "reason": item.reason,
                        "attempts": item.attempts,
                    }
                    for queue in self._waiting.values()
                    for item in queue
                ],
                "resource_limits": {
                    field: getattr(self.limits, field) for field in self.limits.__dataclass_fields__
                },
            }


def classify_resource(role_name: str, task: str = "") -> ResourceClass:
    role = role_name.split("#", 1)[0].lower()
    text = task.lower()
    if role in {"qa", "critic", "designer"} or _marker_signals(
        text, ("playwright", "chromium", "browser qa", "lighthouse")
    ):
        return ResourceClass.BROWSER
    if _marker_signals(text, ("npm install", "npm ci", "pip install", "uv sync")):
        return ResourceClass.PACKAGE_INSTALL
    if _marker_signals(text, ("next build", "npm run build", "webpack", "vite build")):
        return ResourceClass.BUILD
    if _marker_signals(text, ("pytest", "npm test", "jest", "test suite")):
        return ResourceClass.TEST
    if role in {"lead", "shell"}:
        return ResourceClass.LIGHT
    return ResourceClass.NORMAL
