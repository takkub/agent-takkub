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


def test_round_trip_is_atomic_and_validated(tmp_path) -> None:
    target = tmp_path / "performance.json"
    desired = performance_settings.preset("maximum", logical_cpus=24, total_memory_gb=64)
    assert performance_settings.save(desired, target)
    assert performance_settings.load(target) == desired
    assert json.loads(target.read_text(encoding="utf-8"))["schema_version"] == 1


def test_invalid_or_partial_file_falls_back_to_balanced(tmp_path) -> None:
    target = tmp_path / "performance.json"
    target.write_text('{"mode":"maximum"}', encoding="utf-8")
    assert performance_settings.load(target).mode == "balanced"


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
