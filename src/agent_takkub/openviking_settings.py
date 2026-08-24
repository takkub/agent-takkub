"""Persisted UI-facing config for the Settings window's "Knowledge & Design
› OpenViking" view (final closeout pack 2, `docs/plans/final-closeout-
after-1.3.0/04_SETTINGS_UI_FINAL.md`).

`core.context_sources.openviking_adapter`'s own env vars (`TAKKUB_OPENVIKING_
MODE` etc.) ALWAYS win over what's saved here — this store only supplies the
mode `openviking_adapter.mode()` falls back to when no env override is set,
plus the strict-scope UI knobs (`strict_project`/`include_global`/
`result_limit`/`timeout`) nothing reads yet (`02_OPENVIKING_STRICT_SCOPE.md`
is a separate, concurrently-landing backend change) — persisting them now
gives that work a settled place to read from without a schema migration
later, and lets the operator stage the values ahead of time.

Same "leaf, stdlib + config only" shape as `core_v2_settings.py` (see that
module's own docstring for why): no Qt, no `agent_takkub.core` dependency.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from . import config
from .core.context_sources.openviking_adapter import MODES

_DEFAULT_MODE = "shadow"


@dataclass(frozen=True, slots=True)
class OpenVikingUiConfig:
    mode: str = _DEFAULT_MODE
    strict_project: bool = True
    include_global: bool = True
    result_limit: int = 8
    timeout: float = 4.0


def path():
    return config.SETTINGS_HOME / "openviking-settings.json"


def load() -> OpenVikingUiConfig:
    defaults = OpenVikingUiConfig()
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("openviking-settings.json root is not an object")
    except (OSError, ValueError, json.JSONDecodeError):
        return defaults

    mode = raw.get("mode")
    if mode not in MODES:
        mode = defaults.mode
    try:
        result_limit = max(1, int(raw.get("result_limit", defaults.result_limit)))
    except (TypeError, ValueError):
        result_limit = defaults.result_limit
    try:
        timeout = max(0.5, float(raw.get("timeout", defaults.timeout)))
    except (TypeError, ValueError):
        timeout = defaults.timeout

    return OpenVikingUiConfig(
        mode=mode,
        strict_project=bool(raw.get("strict_project", defaults.strict_project)),
        include_global=bool(raw.get("include_global", defaults.include_global)),
        result_limit=result_limit,
        timeout=timeout,
    )


def save(cfg: OpenVikingUiConfig) -> bool:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    return config._write_json_atomic(target, asdict(cfg))


__all__ = ["OpenVikingUiConfig", "load", "path", "save"]
