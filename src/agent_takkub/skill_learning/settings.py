"""Persisted Skill Learning policy — `SETTINGS_HOME/skill_learning.json`.

One schema for the CLI, the Settings dialog and the pipeline. Unknown or
out-of-range values fall back to the defaults instead of discarding the whole
file. `TAKKUB_SKILL_LEARNING=auto|propose|off` overrides the persisted mode
(an emergency switch that needs no UI).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from .. import config

MODES = ("auto", "propose", "off")
# Reflector candidates in preference order. gemini is deliberately absent:
# `agy -p` needs a real TTY and returns nothing when captured (see
# gemini_helper.gemini_exec) — gemini panes are still LEARNED FROM, they just
# never act as the reflector. Documented gap, not a silent one.
REFLECTOR_PROVIDERS = ("claude", "codex", "opencode", "cursor")
ENV_MODE = "TAKKUB_SKILL_LEARNING"

# (min, max) per numeric knob — a hand-edited file can't set a zero cap or a
# one-second reflection cadence.
_BOUNDS: dict[str, tuple[int, int]] = {
    "min_interval_s": (0, 86_400),
    "daily_cap": (0, 500),
    "max_intents": (1, 10),
    "maturity_project": (5, 10_000),
    "maturity_global": (5, 50_000),
    "capacity_project": (1, 500),
    "capacity_global": (1, 200),
    "curate_every": (2, 1_000),
    "timeout_s": (30, 1_800),
    "min_worth": (0, 100),
}


@dataclass(frozen=True, slots=True)
class LearningSettings:
    mode: str = "auto"
    provider: str = "auto"  # "auto" or one of REFLECTOR_PROVIDERS
    model: str = ""  # "" = the provider's own default
    min_interval_s: int = 600  # per project, between two reflections
    daily_cap: int = 24  # reflections per project per day
    max_intents: int = 3  # skill changes accepted from one reflection
    maturity_project: int = 60  # requests before a project skill faces graduation
    maturity_global: int = 200
    capacity_project: int = 40  # mature skills kept per layer
    capacity_global: int = 15
    curate_every: int = 12  # landed changes between two curator passes
    timeout_s: int = 300
    min_worth: int = 3  # episode worth score needed to reflect


def path() -> Path:
    return Path(config.SETTINGS_HOME) / "skill_learning.json"


def _coerce(raw: dict) -> LearningSettings:
    base = LearningSettings()
    out: dict = {}
    for f in fields(LearningSettings):
        if f.name not in raw:
            continue
        val = raw[f.name]
        default = getattr(base, f.name)
        if isinstance(default, int):
            if isinstance(val, bool) or not isinstance(val, int | float):
                continue
            lo, hi = _BOUNDS.get(f.name, (0, 1 << 30))
            out[f.name] = max(lo, min(hi, int(val)))
        elif isinstance(val, str):
            out[f.name] = val.strip()
    s = replace(base, **out)
    if s.mode not in MODES:
        s = replace(s, mode=base.mode)
    if s.provider != "auto" and s.provider not in REFLECTOR_PROVIDERS:
        s = replace(s, provider="auto")
    return s


def _load_persisted() -> LearningSettings:
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return _coerce(raw if isinstance(raw, dict) else {})


def load() -> LearningSettings:
    """Persisted policy with the env emergency override applied."""
    s = _load_persisted()
    env_mode = os.environ.get(ENV_MODE, "").strip().lower()
    if env_mode in MODES:
        s = replace(s, mode=env_mode)
    return s


def save(s: LearningSettings) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def update(**changes) -> LearningSettings:
    """Validate *changes* through the same coercion as `load` and persist.
    Starts from the persisted file, never from `load()` — the env override
    is a runtime switch and must not get written back to disk."""
    current = asdict(_load_persisted())
    current.update(changes)
    s = _coerce(current)
    save(s)
    return s
