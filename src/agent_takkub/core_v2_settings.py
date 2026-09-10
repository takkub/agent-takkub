"""Persisted UI-facing config for Core V2 (epic #309 Phase 9) — the durable
fallback underneath `TAKKUB_V2_ROUTER`/`_CONVERSATION`/`_BRAIN`/`_SCHEDULER`
(env always wins, same precedence shape as `performance_settings.py`'s
env-override-then-config pattern) plus the Scheduler view's `SlotPolicy`
editor, which has no persistence layer anywhere else yet (`core.scheduling`
only ever accepts a `SlotPolicy` built in memory by whoever constructs
`resource_governor.ResourceGovernor` — see that module's own docstring).

No Qt, no `agent_takkub.core` dependency — same "leaf, stdlib + config only"
shape as `performance_settings.py` so `core/*/flag.py` can import this module
without pulling anything UI-shaped into the Core V2 bottom layer (pyproject's
`core-is-bottom-layer` import-linter contract only restricts what
`agent_takkub.core` may import, not what imports it — precedent:
`core.storage.paths` already imports `agent_takkub.config` directly).
"""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config

SCHEMA_VERSION = 1

# Context Strategy (v2-hardening C, `13_SIMPLE_UX.md`) — Fast/Automatic/Deep
# UX switch for the Context Gate/Classifier v2 stack. A plain string rather
# than another `FLAG_NAMES` boolean since it's a 3-way choice, not on/off;
# read back via `core.brain.flag.context_strategy` (env `TAKKUB_CONTEXT_
# STRATEGY` wins over this, same precedence shape the boolean flags use).
_CONTEXT_STRATEGIES: tuple[str, ...] = ("fast", "automatic", "deep")
_DEFAULT_CONTEXT_STRATEGY = "automatic"


@dataclass(frozen=True, slots=True)
class SchedulerPolicyConfig:
    """Same 6 dimensions as `core.scheduling.models.SlotPolicy`, duplicated
    here (rather than imported) so this module stays free of any
    `agent_takkub.core` dependency — the Scheduler view converts this into a
    real `SlotPolicy` only at display/preview time."""

    max_agents_global: int | None = None
    max_panes_global: int | None = None
    provider_max_concurrent: dict[str, int] = field(default_factory=dict)
    account_max_concurrent: dict[str, int] = field(default_factory=dict)
    project_max_agents: dict[str, int] = field(default_factory=dict)
    project_max_panes: dict[str, int] = field(default_factory=dict)
    default_priority: str = "normal"


def path() -> Path:
    return config.SETTINGS_HOME / "core-v2-settings.json"


def _default_payload() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "scheduler_policy": asdict(SchedulerPolicyConfig()),
        "context_strategy": _DEFAULT_CONTEXT_STRATEGY,
    }


# (stat key, parsed payload) — key is `(st_mtime_ns, st_size)` when the file
# exists, or `None` when it's missing/unreadable, so a still-missing file
# hits the cache too instead of rebuilding `_default_payload()` every call.
# Same "reload only when the file actually changed" shape as
# `core.scheduling.facade._policy_cache`. `load()` still `stat()`s on every
# call (one cheap syscall) to detect that change — same tradeoff that
# module's own `effective_slot_policy()` already accepts.
_cache_lock = threading.Lock()
_cache: tuple[tuple[int, int] | None, dict] | None = None


def _reset_cache() -> None:
    """Test-only escape hatch: forces the next `load()` to re-stat/re-parse.
    Needed whenever a test redirects `path()` to a different file — the
    stat-key namespace isn't unique across roots, so a stale cache entry
    from a previous test could otherwise leak in as a coincidental hit."""
    global _cache
    with _cache_lock:
        _cache = None


def load() -> dict:
    """Missing/corrupt file reads as all-defaults — fail-open, same contract
    as `performance_settings.load()`. Cached by the file's `(mtime_ns, size)`
    so repeated calls (e.g. once per `flag_enabled()` check per Qt tick) only
    re-`read_text()`/`json.loads()` when the Settings UI actually rewrote the
    file — see `_cache` above."""
    global _cache
    target = path()
    try:
        st = target.stat()
        cache_key: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = None

    with _cache_lock:
        if _cache is not None and _cache[0] == cache_key:
            return copy.deepcopy(_cache[1])

    if cache_key is None:
        merged = _default_payload()
    else:
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("core-v2-settings.json root is not an object")
            merged = _default_payload()
            # "flags" (router/conversation/context/brain/scheduler/
            # auto_migrate — v2_authority itself retired by #504) is intentionally NOT read back here
            # any more (#515 Settings diet) — every one of them is default-ON
            # since 1.0.84/2.0.0 and `flag_enabled()` below now always
            # returns True, so a legacy file's persisted values are dead
            # weight; the next `save()` (scheduler policy / context strategy)
            # drops the key from disk for good. `TAKKUB_V2_*=0` env vars
            # remain the only escape hatch (see each `core/*/flag.py`).
            policy = payload.get("scheduler_policy")
            if isinstance(policy, dict):
                merged["scheduler_policy"].update(
                    {k: v for k, v in policy.items() if k in merged["scheduler_policy"]}
                )
            strategy = payload.get("context_strategy")
            if isinstance(strategy, str) and strategy in _CONTEXT_STRATEGIES:
                merged["context_strategy"] = strategy
        except (OSError, ValueError, json.JSONDecodeError):
            merged = _default_payload()

    with _cache_lock:
        _cache = (cache_key, merged)
    return copy.deepcopy(merged)


def save(payload: dict) -> bool:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    ok = config._write_json_atomic(target, payload)
    _reset_cache()
    return ok


def flag_enabled(name: str) -> bool:
    """Always True (#515 Settings diet) — every Core V2 flag (router/
    conversation/context/brain/scheduler/auto_migrate) has been
    default-ON since 1.0.84/2.0.0 with no real-world reason left to flip one
    off from the UI (v2_authority itself was retired outright by #504 —
    see `core.storage.v2_target`). `name` is accepted (unused) so every
    `core/*/flag.py`
    module's `env-wins-else-this` call shape needs no change — an operator's
    `TAKKUB_V2_*=0`/`TAKKUB_AUTO_MIGRATE=0` env override is checked BEFORE
    this function is ever reached and still fully works."""
    return True


def load_scheduler_policy() -> SchedulerPolicyConfig:
    raw = load()["scheduler_policy"]
    return SchedulerPolicyConfig(
        max_agents_global=raw.get("max_agents_global"),
        max_panes_global=raw.get("max_panes_global"),
        provider_max_concurrent=dict(raw.get("provider_max_concurrent") or {}),
        account_max_concurrent=dict(raw.get("account_max_concurrent") or {}),
        project_max_agents=dict(raw.get("project_max_agents") or {}),
        project_max_panes=dict(raw.get("project_max_panes") or {}),
        default_priority=raw.get("default_priority") or "normal",
    )


def save_scheduler_policy(policy: SchedulerPolicyConfig) -> bool:
    payload = load()
    payload["scheduler_policy"] = asdict(policy)
    return save(payload)


def load_context_strategy() -> str:
    return load().get("context_strategy", _DEFAULT_CONTEXT_STRATEGY)


def save_context_strategy(value: str) -> bool:
    if value not in _CONTEXT_STRATEGIES:
        raise ValueError(f"unknown context strategy: {value!r}")
    payload = load()
    payload["context_strategy"] = value
    return save(payload)
