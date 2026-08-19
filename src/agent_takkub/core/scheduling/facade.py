"""The ONE façade `resource_governor.py` calls into for the Phase 8a
dimensions (docs/v2/REUSE_VS_REWRITE_MATRIX.md §5: "V2 แทรกผ่าน façade เดียว
ต่อ boundary"). Flag OFF (default): every function is a no-op that returns
whatever leaves the caller's existing behavior untouched — `resource_governor`
stays byte-identical (`tests/test_resource_governor*.py`'s flag-off parity
tests). Flag ON: best-effort, fail-open — an exception here never turns into
a spurious denial of a slot the legacy checks already allowed."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from . import backpressure, policy
from .backpressure import BackpressureSignal
from .flag import v2_scheduler_enabled
from .models import ActiveCounts, BackpressureLevel, Priority, SlotPolicy, SlotRequest
from .priority_queue import order_by_priority

_log = logging.getLogger(__name__)


def extended_denial_reason(
    request: SlotRequest, counts: ActiveCounts, slot_policy: SlotPolicy
) -> str:
    if not v2_scheduler_enabled():
        return ""
    try:
        return policy.evaluate(request, counts, slot_policy)
    except Exception:
        _log.exception("core.scheduling.facade.extended_denial_reason failed (fail-open)")
        return ""


def backpressure_level(signal: BackpressureSignal) -> BackpressureLevel:
    if not v2_scheduler_enabled():
        return BackpressureLevel.NORMAL
    try:
        return backpressure.classify(signal)
    except Exception:
        _log.exception("core.scheduling.facade.backpressure_level failed (fail-open)")
        return BackpressureLevel.NORMAL


def backpressure_admits(level: BackpressureLevel, priority: Priority) -> bool:
    if not v2_scheduler_enabled():
        return True
    try:
        return backpressure.admits(level, priority)
    except Exception:
        _log.exception("core.scheduling.facade.backpressure_admits failed (fail-open)")
        return True


def order_projects(project_ids: Sequence[str], priority_of: Callable[[str], Priority]) -> list[str]:
    if not v2_scheduler_enabled():
        return list(project_ids)
    try:
        return order_by_priority(project_ids, priority_of)
    except Exception:
        _log.exception("core.scheduling.facade.order_projects failed (fail-open)")
        return list(project_ids)
