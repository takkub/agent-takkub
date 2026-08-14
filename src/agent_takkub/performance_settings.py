"""Persisted, validated cockpit performance policy.

The environment variables supported by :mod:`resource_governor` remain the
highest-priority emergency override.  This module supplies the durable UI
defaults underneath them and deliberately has no Qt dependency so settings,
CLI, and tests all use one schema.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
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
        1,
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
    return settings


def from_dict(payload: dict) -> PerformanceSettings:
    values = {
        name: payload[name] for name in PerformanceSettings.__dataclass_fields__ if name in payload
    }
    missing = set(PerformanceSettings.__dataclass_fields__) - set(values)
    if missing:
        raise ValueError(f"missing performance settings: {', '.join(sorted(missing))}")
    return validate(PerformanceSettings(**values))


def load(settings_path: Path | None = None) -> PerformanceSettings:
    target = settings_path or path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("performance settings root must be an object")
        return from_dict(payload)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return preset("balanced")


def save(settings: PerformanceSettings, settings_path: Path | None = None) -> bool:
    target = settings_path or path()
    validate(settings)
    target.parent.mkdir(parents=True, exist_ok=True)
    return config._write_json_atomic(target, settings.to_dict())
