"""Persisted, validated cockpit performance policy.

The environment variables supported by :mod:`resource_governor` remain the
highest-priority emergency override.  This module supplies the durable UI
defaults underneath them and deliberately has no Qt dependency so settings,
CLI, and tests all use one schema.

#515 Settings diet — "โหมดเครื่อง" (machine mode): the only thing worth
persisting is `mode` (safe/balanced/maximum); every concurrency/threshold
number in `PerformanceSettings` is a live `preset(mode)` derivation from
this machine's current CPU/RAM (psutil), never a user-picked number again.
`get_mode()`/`set_mode()` are the new persistence API — `load()`/`save()`
still exist and still return/accept a full `PerformanceSettings` (every
existing caller reads its fields unchanged), but `save()` now only persists
the `mode` field, and `load()` recomputes the rest fresh from `preset(mode)`
every call rather than reading back stale numbers a prior machine/OS
produced. A legacy file that still carries the old per-field numbers is
migrated once on first read: its `mode` is extracted, the old file is moved
to `SETTINGS_HOME/backups/` (never deleted), and a slim mode-only file is
written in its place.
"""

from __future__ import annotations

import json
from dataclasses import MISSING, asdict, dataclass
from pathlib import Path

import psutil

from . import config

SCHEMA_VERSION = 1
MODES = ("safe", "balanced", "maximum")


@dataclass(frozen=True, slots=True)
class PerformanceSettings:
    mode: str
    max_heavy_global: int
    max_heavy_per_project: int
    max_browser_global: int
    max_build_global: int
    max_test_global: int
    max_package_install_global: int
    cpu_pause_percent: float
    cpu_resume_percent: float
    min_available_ram_percent: float
    resume_ram_percent: float
    hidden_render_ms: int
    # overload_deadband_timeout_s (#305): seconds the overload latch may sit
    # in the CPU<pause/RAM<resume_ram "dead-band" (see resource_governor's
    # `sample()`) before it auto-releases. Added after the schema shipped, so
    # `from_dict` below defaults it for settings files saved before this
    # field existed rather than discarding the whole persisted file.
    overload_deadband_timeout_s: float = 120.0
    # #364 lever 1: discard the Chromium renderer of a hidden pane after it
    # sits inactive past the debounce window (TerminalWidget's own timer —
    # this is just the on/off switch). Same "added after schema shipped"
    # story as overload_deadband_timeout_s above — defaulted in from_dict
    # for settings files saved before this field existed.
    pane_discard_enabled: bool = True

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}


def path() -> Path:
    return config.SETTINGS_HOME / "performance-settings.json"


def preset(
    mode: str,
    *,
    logical_cpus: int | None = None,
    total_memory_gb: float | None = None,
) -> PerformanceSettings:
    """Return a machine-aware preset without reading or writing disk."""
    mode = str(mode).strip().lower()
    if mode not in MODES:
        raise ValueError(f"unknown performance mode: {mode!r}")
    cpu = max(1, int(logical_cpus or psutil.cpu_count(logical=True) or 4))
    total_gb = float(
        total_memory_gb
        if total_memory_gb is not None
        else psutil.virtual_memory().total / (1024**3)
    )
    if mode == "safe":
        return PerformanceSettings(mode, 2, 1, 1, 1, 1, 1, 80, 60, 25, 30, 400)
    if mode == "maximum":
        heavy = max(4, min(8, cpu // 3))
        per_project = max(2, min(4, heavy // 2))
        browser = max(2, min(4, cpu // 8))
        return PerformanceSettings(
            mode,
            heavy,
            per_project,
            browser,
            min(4, heavy),
            min(4, heavy),
            2,
            92,
            75,
            12,
            18,
            200,
        )
    if cpu <= 8 or total_gb <= 16:
        heavy, per_project, browser = 2, 1, 1
    elif cpu >= 24 and total_gb >= 64:
        heavy, per_project, browser = 6, 3, 3
    else:
        heavy, per_project, browser = 4, 2, 2
    return PerformanceSettings(
        mode,
        heavy,
        per_project,
        browser,
        min(2, heavy),
        min(2, heavy),
        # #240 point 2: limit=1 turned every misclassified task into a full
        # global serializer (one wrong classification == every other task
        # queues behind it, even ones that never install anything). The
        # classifier fix (`resource_governor._marker_signals`) addresses the
        # ROOT CAUSE, but a single global slot is still too fragile for a
        # signal derived by scanning free-form task text — 2 halves the
        # blast radius of any future misclassification without meaningfully
        # weakening the guardrail (still a hard global cap, just not a
        # complete-serialization one). "safe" mode below keeps 1 — its whole
        # point is maximum conservatism.
        2,
        85,
        65,
        20,
        25,
        300,
    )


def validate(settings: PerformanceSettings) -> PerformanceSettings:
    if settings.mode not in MODES:
        raise ValueError("mode must be safe, balanced, or maximum")
    integer_limits = (
        settings.max_heavy_global,
        settings.max_heavy_per_project,
        settings.max_browser_global,
        settings.max_build_global,
        settings.max_test_global,
        settings.max_package_install_global,
    )
    if any(value < 1 or value > 64 for value in integer_limits):
        raise ValueError("concurrency limits must be between 1 and 64")
    if settings.max_heavy_per_project > settings.max_heavy_global:
        raise ValueError("per-project heavy limit cannot exceed the global limit")
    if not 1 <= settings.cpu_resume_percent < settings.cpu_pause_percent <= 100:
        raise ValueError("CPU resume threshold must be lower than pause threshold")
    if not 1 <= settings.min_available_ram_percent < settings.resume_ram_percent <= 100:
        raise ValueError("RAM resume threshold must be higher than pause threshold")
    if not 50 <= settings.hidden_render_ms <= 2_000:
        raise ValueError("background render cadence must be between 50 and 2000 ms")
    if not 10 <= settings.overload_deadband_timeout_s <= 1_800:
        raise ValueError("overload dead-band timeout must be between 10 and 1800 seconds")
    return settings


def from_dict(payload: dict) -> PerformanceSettings:
    values = {
        name: payload[name] for name in PerformanceSettings.__dataclass_fields__ if name in payload
    }
    # Fields with a dataclass default (currently just overload_deadband_timeout_s,
    # #305) may be absent from a settings file saved before that field existed —
    # fall back to its default instead of discarding the whole persisted file.
    missing = set(PerformanceSettings.__dataclass_fields__) - set(values)
    missing = {
        name
        for name in missing
        if PerformanceSettings.__dataclass_fields__[name].default is MISSING
    }
    if missing:
        raise ValueError(f"missing performance settings: {', '.join(sorted(missing))}")
    return validate(PerformanceSettings(**values))


_DEFAULT_MODE = "balanced"


def _read_raw(target: Path) -> dict | None:
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def get_mode(settings_path: Path | None = None) -> str:
    """The persisted machine mode ("safe"/"balanced"/"maximum") — the only
    field this store keeps any more. A legacy file still shaped like the
    pre-#515 full `PerformanceSettings` dump (numeric fields alongside
    `mode`) is migrated once: the old file moves to `SETTINGS_HOME/backups/`
    (never deleted) and a slim `{"mode": ...}` file is written in its place.
    Missing/corrupt/unknown-mode -> `_DEFAULT_MODE`."""
    target = settings_path or path()
    payload = _read_raw(target)
    mode = payload.get("mode") if payload else None
    if mode not in MODES:
        return _DEFAULT_MODE
    # Legacy shape carries the old numeric fields too — slim it down once.
    if payload is not None and len(payload.keys() - {"schema_version", "mode"}) > 0:
        from . import config

        config.archive_settings_file(target)
        set_mode(mode, settings_path)
    return mode


def set_mode(mode: str, settings_path: Path | None = None) -> bool:
    mode = str(mode).strip().lower()
    if mode not in MODES:
        raise ValueError(f"unknown performance mode: {mode!r}")
    target = settings_path or path()
    target.parent.mkdir(parents=True, exist_ok=True)
    return config._write_json_atomic(target, {"schema_version": SCHEMA_VERSION, "mode": mode})


def load(settings_path: Path | None = None) -> PerformanceSettings:
    """A full, machine-aware `PerformanceSettings` — every numeric field is a
    fresh `preset(mode)` derivation from this machine's current CPU/RAM, not
    a persisted number (see module docstring)."""
    return preset(get_mode(settings_path))


def save(settings: PerformanceSettings, settings_path: Path | None = None) -> bool:
    """Persists only `settings.mode` — any numeric overrides on `settings`
    are derived, not user-settable any more (see module docstring), so they
    are accepted (for call-site backward compatibility) but ignored."""
    return set_mode(settings.mode, settings_path)
