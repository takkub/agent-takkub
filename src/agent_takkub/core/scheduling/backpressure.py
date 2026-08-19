"""Backpressure ladder (docs/v2/07_SCHEDULER_RESOURCE_RUNTIME.md §10):
Normal -> throttle new tasks -> pause background agents -> queue. Reuses the
SAME cpu/ram signal `resource_governor.sample()` already computes — no new
polling. Levels are graded, not switch-like: THROTTLE_NEW engages before the
existing `overloaded` latch trips, so low-priority admission slows down
ahead of the hard pause instead of at the same instant as it. Never kills
what's already running (§10: "ไม่ควร kill agent ทันทีถ้าไม่จำเป็น") — callers
gate admission of NEW work via `admits()`, and separately `pause()` already-
running BACKGROUND work through `runtime_control` once the rung reaches
PAUSE_BACKGROUND."""

from __future__ import annotations

from dataclasses import dataclass

from .models import BackpressureLevel, Priority

# Ratio of the way from cpu_resume_percent to cpu_pause_percent at which
# THROTTLE_NEW engages, e.g. resume=65, pause=85 -> throttle at 75.
_THROTTLE_RAMP = 0.5

# RAM headroom multiplier (relative to min_available_ram_percent) below
# which THROTTLE_NEW engages ahead of the hard `overloaded` latch.
_THROTTLE_RAM_MARGIN = 1.25

# queued/active-capacity ratio above which a sustained PAUSE_BACKGROUND
# escalates to QUEUE — §10's last rung, reached only once overloaded AND the
# backlog shows the pressure isn't clearing on its own.
_QUEUE_BACKLOG_RATIO = 2.0


@dataclass(frozen=True, slots=True)
class BackpressureSignal:
    cpu_percent: float
    available_ram_percent: float
    overloaded: bool
    cpu_pause_percent: float
    cpu_resume_percent: float
    min_available_ram_percent: float
    queued_count: int = 0
    active_capacity_hint: int = 1


def classify(signal: BackpressureSignal) -> BackpressureLevel:
    if signal.overloaded:
        backlog_threshold = max(1, signal.active_capacity_hint) * _QUEUE_BACKLOG_RATIO
        if signal.queued_count >= backlog_threshold:
            return BackpressureLevel.QUEUE
        return BackpressureLevel.PAUSE_BACKGROUND
    throttle_cpu = signal.cpu_resume_percent + _THROTTLE_RAMP * (
        signal.cpu_pause_percent - signal.cpu_resume_percent
    )
    throttle_ram = signal.min_available_ram_percent * _THROTTLE_RAM_MARGIN
    if signal.cpu_percent >= throttle_cpu or signal.available_ram_percent < throttle_ram:
        return BackpressureLevel.THROTTLE_NEW
    return BackpressureLevel.NORMAL


# Lowest-priority tier still admitted for NEW work at each rung.
_MIN_PRIORITY_ADMITTED: dict[BackpressureLevel, Priority] = {
    BackpressureLevel.NORMAL: Priority.BACKGROUND,
    BackpressureLevel.THROTTLE_NEW: Priority.NORMAL,
    BackpressureLevel.PAUSE_BACKGROUND: Priority.LOW,
    BackpressureLevel.QUEUE: Priority.CRITICAL,
}


def admits(level: BackpressureLevel, priority: Priority) -> bool:
    return priority <= _MIN_PRIORITY_ADMITTED[level]
