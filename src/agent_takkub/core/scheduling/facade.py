"""The ONE façade `resource_governor.py` calls into for the Phase 8a
dimensions (docs/v2/REUSE_VS_REWRITE_MATRIX.md §5: "V2 แทรกผ่าน façade เดียว
ต่อ boundary"). Flag OFF (`TAKKUB_V2_SCHEDULER=0`): every function is a no-op that returns
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

# (settings file mtime, converted SlotPolicy) — reload only when the Settings
# UI actually rewrote core-v2-settings.json, not on every call.
_policy_cache: tuple[float, SlotPolicy] | None = None


def _slot_policy_from_config(cfg) -> SlotPolicy:
    return SlotPolicy(
        max_agents_global=cfg.max_agents_global,
        max_panes_global=cfg.max_panes_global,
        provider_max_concurrent=dict(cfg.provider_max_concurrent),
        account_max_concurrent=dict(cfg.account_max_concurrent),
        project_max_agents=dict(cfg.project_max_agents),
        project_max_panes=dict(cfg.project_max_panes),
    )


def effective_slot_policy() -> SlotPolicy:
    """The `SlotPolicy` the Settings UI's persisted Scheduler Policy
    (`core_v2_settings.SchedulerPolicyConfig`) resolves to right now —
    `resource_governor.ResourceGovernor` falls back to this only when its
    caller didn't inject an explicit `slot_policy` of its own. Flag OFF: the
    settings file is never touched at all, same "zero disk I/O" contract as
    every other function in this module. Cached by the settings file's mtime
    so a caller building many governors doesn't reread/reparse it each time;
    any failure (missing/corrupt file, bad shape) fails open to `SlotPolicy()`
    — the same all-defaults, denies-nothing policy a caller gets today."""
    if not v2_scheduler_enabled():
        return SlotPolicy()
    global _policy_cache
    try:
        from agent_takkub import core_v2_settings

        mtime = core_v2_settings.path().stat().st_mtime
        if _policy_cache is not None and _policy_cache[0] == mtime:
            return _policy_cache[1]
        computed = _slot_policy_from_config(core_v2_settings.load_scheduler_policy())
        _policy_cache = (mtime, computed)
        return computed
    except Exception:
        _log.exception("core.scheduling.facade.effective_slot_policy failed (fail-open)")
        return SlotPolicy()


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
