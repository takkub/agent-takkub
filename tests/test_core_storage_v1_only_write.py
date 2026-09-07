"""`core.storage.v1_only_write` (#502, Phase 10 "V2.1" prep for #504) — a
V1 source newer than its dual-write mirror means some writer skipped
`core.storage.dual_write`. Same "pass data_home explicitly" style
`test_core_storage_dual_write.py` already uses, independent of
`tests/conftest.py`'s isolation wiring."""

from __future__ import annotations

import os
import time

import pytest

from agent_takkub.core.migration.steps_v1 import build_readonly_registries_step
from agent_takkub.core.storage import dual_write
from agent_takkub.core.storage.v1_only_write import scan_v1_only_writes


@pytest.fixture(autouse=True)
def _isolated_settings_home(tmp_path, monkeypatch):
    """`scan_v1_only_writes` resolves every mapping's `settings_home` from
    `config.SETTINGS_HOME` (only `data_home` is threaded explicitly, same
    convention `dual_write.py`'s own callers already follow) — without this,
    a test writing to a mapping's `.source` path would land in this dev
    machine's real `~/.takkub`/settings dir."""
    settings_home = tmp_path / "settings_home"
    settings_home.mkdir()
    monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", settings_home)


def _migrated_home(tmp_path):
    home = tmp_path / "data_home"
    (home / "v2").mkdir(parents=True)
    return home


def _touch_future(path, seconds: float = 100.0) -> None:
    future = time.time() + seconds
    os.utime(path, (future, future))


def test_not_migrated_returns_empty(tmp_path):
    home = tmp_path / "data_home"  # no v2/ created
    assert scan_v1_only_writes(data_home=home) == []


def test_no_hits_when_v1_source_missing(tmp_path):
    home = _migrated_home(tmp_path)
    assert scan_v1_only_writes(data_home=home) == []


def test_no_hits_right_after_dual_write(tmp_path):
    """The normal, working path: a V1 writer calls its dual_write_* sibling
    in the same call, so the V2 mirror is never older than V1."""
    home = _migrated_home(tmp_path)
    mapping = build_readonly_registries_step(data_home=home).mappings
    source = next(m.source for m in mapping if m.name == "provider-models")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("{}", encoding="utf-8")
    dual_write.dual_write_provider_models({"claude": "sonnet"}, data_home=home)

    assert scan_v1_only_writes(data_home=home) == []


def test_hit_when_v1_source_written_without_dual_write(tmp_path):
    """A writer that skipped dual-write entirely: V1 source exists and is
    newer than the (stale, or never-updated) V2 mirror."""
    home = _migrated_home(tmp_path)
    mapping = build_readonly_registries_step(data_home=home).mappings
    source = next(m.source for m in mapping if m.name == "provider-models")
    target = next(m.target for m in mapping if m.name == "provider-models")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"schema": 1, "data": {}}', encoding="utf-8")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"claude": "sonnet"}', encoding="utf-8")
    _touch_future(source)

    hits = scan_v1_only_writes(data_home=home)
    assert [h.name for h in hits] == ["provider-models"]
    assert hits[0].lag_s > 0


def test_small_mtime_gap_is_not_a_hit(tmp_path):
    """Same-tick V1-then-V2 writes (coarse filesystem mtime resolution) must
    not false-positive — only a gap past the slop counts."""
    home = _migrated_home(tmp_path)
    mapping = build_readonly_registries_step(data_home=home).mappings
    source = next(m.source for m in mapping if m.name == "provider-models")
    target = next(m.target for m in mapping if m.name == "provider-models")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"schema": 1, "data": {}}', encoding="utf-8")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"claude": "sonnet"}', encoding="utf-8")
    now = time.time()
    os.utime(source, (now, now))
    os.utime(target, (now, now))

    assert scan_v1_only_writes(data_home=home) == []


def test_custom_roles_registry_covered(tmp_path):
    home = _migrated_home(tmp_path)
    from agent_takkub import config
    from agent_takkub.core.storage.layout import storage_layout_v2

    target = storage_layout_v2(home).agents / "custom" / "registry.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"schema": 1, "data": {}}', encoding="utf-8")
    source = config.SETTINGS_HOME / "custom-roles.json"
    source.write_text("{}", encoding="utf-8")
    _touch_future(source)

    hits = scan_v1_only_writes(data_home=home)
    assert any(h.name == "custom-roles" for h in hits)


def test_projects_registry_covered(tmp_path):
    home = _migrated_home(tmp_path)
    from agent_takkub.core.migration.steps_v1 import ProjectMigrationStep

    step = ProjectMigrationStep(data_home=home)
    target = step._registry_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"schema": 1, "data": {}}', encoding="utf-8")
    source = home / "projects.json"
    source.write_text('{"projects": {}}', encoding="utf-8")
    _touch_future(source)

    hits = scan_v1_only_writes(data_home=home)
    assert any(h.name == "projects-registry" for h in hits)
