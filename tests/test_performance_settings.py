from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_takkub import performance_settings


def test_balanced_preset_is_machine_aware() -> None:
    small = performance_settings.preset("balanced", logical_cpus=8, total_memory_gb=16)
    large = performance_settings.preset("balanced", logical_cpus=32, total_memory_gb=128)
    assert small.mode == "balanced"
    assert small.max_heavy_global == 2
    assert large.max_heavy_global == 6


def test_round_trip_persists_only_mode(tmp_path) -> None:
    """#515 Settings diet: `save()` only persists `mode` — every numeric
    field is a live `preset(mode)` derivation from THIS machine, never a
    stale number from whatever machine/OS produced the file."""
    target = tmp_path / "performance.json"
    desired = performance_settings.preset("maximum", logical_cpus=24, total_memory_gb=64)
    assert performance_settings.save(desired, target)
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk == {"schema_version": 1, "mode": "maximum"}
    assert performance_settings.load(target).mode == "maximum"


def test_get_mode_round_trips(tmp_path) -> None:
    target = tmp_path / "performance.json"
    assert performance_settings.set_mode("safe", target)
    assert performance_settings.get_mode(target) == "safe"
    assert performance_settings.load(target).mode == "safe"


def test_invalid_or_missing_file_falls_back_to_balanced(tmp_path) -> None:
    target = tmp_path / "performance.json"
    assert performance_settings.load(target).mode == "balanced"
    target.write_text("not json", encoding="utf-8")
    assert performance_settings.load(target).mode == "balanced"


def test_legacy_full_shape_file_migrates_to_mode_only(tmp_path) -> None:
    """A pre-#515 file (real prod shape: mode + every numeric field) reads
    its `mode` once, archives the old file under `backups/` instead of
    deleting it, and rewrites a slim mode-only file in its place."""
    target = tmp_path / "performance-settings.json"
    legacy = {
        "schema_version": 1,
        "mode": "balanced",
        "max_heavy_global": 4,
        "max_heavy_per_project": 2,
        "max_browser_global": 2,
        "max_build_global": 2,
        "max_test_global": 2,
        "max_package_install_global": 2,
        "cpu_pause_percent": 85,
        "cpu_resume_percent": 65,
        "min_available_ram_percent": 20,
        "resume_ram_percent": 25,
        "hidden_render_ms": 300,
    }
    target.write_text(json.dumps(legacy), encoding="utf-8")

    assert performance_settings.get_mode(target) == "balanced"

    backup = target.parent / "backups" / target.name
    assert backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8")) == legacy
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "mode": "balanced",
    }


def test_hysteresis_and_per_project_invariants() -> None:
    base = performance_settings.preset("balanced", logical_cpus=16, total_memory_gb=32)
    with pytest.raises(ValueError, match="per-project"):
        performance_settings.validate(replace(base, max_heavy_global=1, max_heavy_per_project=2))


def test_safe_and_maximum_bound_balanced() -> None:
    safe = performance_settings.preset("safe", logical_cpus=32, total_memory_gb=128)
    balanced = performance_settings.preset("balanced", logical_cpus=32, total_memory_gb=128)
    maximum = performance_settings.preset("maximum", logical_cpus=32, total_memory_gb=128)
    assert safe.max_heavy_global < balanced.max_heavy_global <= maximum.max_heavy_global
    assert safe.hidden_render_ms > balanced.hidden_render_ms > maximum.hidden_render_ms


def test_from_dict_still_defaults_missing_overload_deadband_field() -> None:
    """#305: `overload_deadband_timeout_s` was added after the schema
    shipped. `from_dict` (still used to validate a fully-specified
    `PerformanceSettings` payload, e.g. in tests) defaults it instead of
    discarding the whole payload the way a genuinely-missing field would."""
    desired = performance_settings.preset("maximum", logical_cpus=24, total_memory_gb=64)
    payload = desired.to_dict()
    del payload["overload_deadband_timeout_s"]

    loaded = performance_settings.from_dict(payload)
    assert loaded.mode == "maximum"
    assert loaded.max_heavy_global == desired.max_heavy_global
    assert loaded.overload_deadband_timeout_s == 120.0


def test_overload_deadband_timeout_bounds_are_validated() -> None:
    base = performance_settings.preset("balanced", logical_cpus=16, total_memory_gb=32)
    with pytest.raises(ValueError, match="dead-band"):
        performance_settings.validate(replace(base, overload_deadband_timeout_s=5.0))
    with pytest.raises(ValueError, match="dead-band"):
        performance_settings.validate(replace(base, overload_deadband_timeout_s=3_600.0))


def test_package_install_limit_not_one_in_balanced_or_maximum() -> None:
    """Issue #240 point 2: a global limit of 1 turned a single misclassified
    task into a full serializer for every other task sharing its resource
    class. "safe" mode keeps 1 by design (max conservatism); balanced and
    maximum must not."""
    safe = performance_settings.preset("safe", logical_cpus=32, total_memory_gb=128)
    balanced = performance_settings.preset("balanced", logical_cpus=32, total_memory_gb=128)
    maximum = performance_settings.preset("maximum", logical_cpus=32, total_memory_gb=128)
    assert safe.max_package_install_global == 1
    assert balanced.max_package_install_global >= 2
    assert maximum.max_package_install_global >= 2
