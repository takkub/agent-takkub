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

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config

SCHEMA_VERSION = 1

# Mirrors the 5 flags named in the epic #309 Phase 9 task spec. "context" has
# no `core.context` module / flag.py yet (Context Builder is 7c, still
# unbuilt per `core.brain.facade`'s own docstring) — kept here anyway so the
# Overview view has one place to persist it the day that module exists,
# rather than needing a schema migration later.
FLAG_NAMES: tuple[str, ...] = ("router", "conversation", "context", "brain", "scheduler")

_DEFAULT_FLAGS: dict[str, bool] = {name: False for name in FLAG_NAMES}


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
        "flags": dict(_DEFAULT_FLAGS),
        "scheduler_policy": asdict(SchedulerPolicyConfig()),
    }


def load() -> dict:
    """Missing/corrupt file reads as all-defaults — fail-open, same contract
    as `performance_settings.load()`."""
    target = path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return _default_payload()
    except (OSError, ValueError, json.JSONDecodeError):
        return _default_payload()
    merged = _default_payload()
    flags = payload.get("flags")
    if isinstance(flags, dict):
        merged["flags"].update({k: bool(v) for k, v in flags.items() if k in FLAG_NAMES})
    policy = payload.get("scheduler_policy")
    if isinstance(policy, dict):
        merged["scheduler_policy"].update(
            {k: v for k, v in policy.items() if k in merged["scheduler_policy"]}
        )
    return merged


def save(payload: dict) -> bool:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    return config._write_json_atomic(target, payload)


def flag_enabled(name: str) -> bool:
    return bool(load()["flags"].get(name, False))


def set_flag(name: str, value: bool) -> bool:
    if name not in FLAG_NAMES:
        raise ValueError(f"unknown Core V2 flag: {name!r}")
    payload = load()
    payload["flags"][name] = bool(value)
    return save(payload)


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
