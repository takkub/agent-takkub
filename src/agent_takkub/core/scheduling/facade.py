"""The ONE façade `resource_governor.py` calls into for the Phase 8a
dimensions (docs/v2/REUSE_VS_REWRITE_MATRIX.md §5: "V2 แทรกผ่าน façade เดียว
ต่อ boundary"). Flag OFF (`TAKKUB_V2_SCHEDULER=0`): every function is a no-op that returns
whatever leaves the caller's existing behavior untouched — `resource_governor`
stays byte-identical (`tests/test_resource_governor*.py`'s flag-off parity
tests). Flag ON: best-effort, fail-open — an exception here never turns into
a spurious denial of a slot the legacy checks already allowed."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Sequence

from . import backpressure, policy
from .backpressure import BackpressureSignal
from .flag import v2_scheduler_enabled
from .models import ActiveCounts, BackpressureLevel, Priority, SlotPolicy, SlotRequest
from .priority_queue import order_by_priority

_log = logging.getLogger(__name__)

# (settings file mtime, converted SlotPolicy) — reload only when the Settings
# UI actually rewrote core-v2-settings.json, not on every call. `mtime` is
# `None` when the file doesn't exist yet (the common case — most machines
# never opened Settings → Scheduler), a stable key so the parse result still
# caches instead of re-parsing `_default_payload()` every call.
_policy_cache: tuple[float | None, SlotPolicy] | None = None

# #364 lever 3: one active pane/subagent measured at ~540MB (claude/codex CLI)
# + ~99MB (QtWebEngineProcess for a pane's xterm.js) ≈ 650MB, the same figure
# `exec_mode.py`'s `_PANE_RAM_GB` already budgets per fan-out instance.
_BYTES_PER_PANE_ESTIMATE = 650 * 1024 * 1024


def _slot_policy_from_config(cfg) -> SlotPolicy:
    return SlotPolicy(
        max_agents_global=cfg.max_agents_global,
        max_panes_global=cfg.max_panes_global,
        provider_max_concurrent=dict(cfg.provider_max_concurrent),
        account_max_concurrent=dict(cfg.account_max_concurrent),
        project_max_agents=dict(cfg.project_max_agents),
        project_max_panes=dict(cfg.project_max_panes),
    )


def _ram_derived_max_panes_global() -> int | None:
    """Proactive default for `max_panes_global` when the Settings UI hasn't
    pinned one (#364 lever 3 — reject a spawn before the machine gets
    sluggish, instead of only braking once the REACTIVE cpu/RAM governor
    latch already trips): `floor((available − reserve) / 650MB)`, where
    `reserve` is the same `min_available_ram_percent` share of total RAM the
    reactive latch already keeps in hand (`performance_settings`'s persisted
    value — this module can't import `resource_governor` itself, which is
    what applies its `TAKKUB_MIN_AVAILABLE_RAM_PERCENT` env override on top;
    that emergency knob is rare enough that this default skipping it is an
    acceptable gap, not a correctness bug).

    Sampled fresh on every call (a `psutil.virtual_memory()` read is a few
    microseconds) rather than folded into `_policy_cache` above — that cache
    exists to skip re-parsing the settings JSON, not to skip a RAM sample
    this cheap, and this number needs to track current headroom, not the
    settings file's mtime. Floors at 1 (never a 0-or-negative cap that would
    block every spawn outright) and fails open to `None` (no cap) on any
    psutil error, matching every other function in this module."""
    try:
        import psutil

        from agent_takkub import performance_settings

        vm = psutil.virtual_memory()
        reserve_fraction = performance_settings.load().min_available_ram_percent / 100.0
        headroom_bytes = vm.available - (vm.total * reserve_fraction)
        return max(1, int(headroom_bytes // _BYTES_PER_PANE_ESTIMATE))
    except Exception:
        _log.exception("core.scheduling.facade._ram_derived_max_panes_global failed (fail-open)")
        return None


def effective_slot_policy() -> SlotPolicy:
    """The `SlotPolicy` the Settings UI's persisted Scheduler Policy
    (`core_v2_settings.SchedulerPolicyConfig`) resolves to right now —
    `resource_governor.ResourceGovernor` falls back to this only when its
    caller didn't inject an explicit `slot_policy` of its own. Flag OFF: the
    settings file is never touched at all, same "zero disk I/O" contract as
    every other function in this module. The parsed config is cached by the
    settings file's mtime so a caller building many governors doesn't
    reread/reparse it each time; any failure (missing/corrupt file, bad
    shape) fails open to `SlotPolicy()` — the same all-defaults, denies-
    nothing policy a caller gets today.

    `max_panes_global` gets one extra step layered on top of the cache
    (#364 lever 3): when the config leaves it unset (`None` — true both for
    an explicit `null` on disk and for no settings file at all), this
    substitutes `_ram_derived_max_panes_global()`'s live RAM-based figure
    instead of leaving the dimension uncapped."""
    if not v2_scheduler_enabled():
        return SlotPolicy()
    global _policy_cache
    try:
        from agent_takkub import core_v2_settings

        try:
            mtime = core_v2_settings.path().stat().st_mtime
        except OSError:
            mtime = None
        if _policy_cache is not None and _policy_cache[0] == mtime:
            policy = _policy_cache[1]
        else:
            policy = _slot_policy_from_config(core_v2_settings.load_scheduler_policy())
            _policy_cache = (mtime, policy)
        if policy.max_panes_global is None:
            ram_cap = _ram_derived_max_panes_global()
            if ram_cap is not None:
                policy = dataclasses.replace(policy, max_panes_global=ram_cap)
        return policy
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
