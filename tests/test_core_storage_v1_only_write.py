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


# ── #502/#504 review 2026-09-07: missing mirror (no v2/ target at all) ─────


def test_source_with_no_mirror_at_all_is_a_hit(tmp_path):
    """A V1 source that exists with NO v2/ mirror ever created — the domain
    was never migrated, or (the case that matters) a fresh V1-only write
    created it for a domain dual-write never got wired up for. The old
    "both files must exist" rule silently read this as zero hits."""
    home = _migrated_home(tmp_path)
    mapping = build_readonly_registries_step(data_home=home).mappings
    source = next(m.source for m in mapping if m.name == "provider-models")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"claude": "sonnet"}', encoding="utf-8")
    # deliberately never create the target

    hits = scan_v1_only_writes(data_home=home)
    hit = next(h for h in hits if h.name == "provider-models")
    assert hit.reason == "missing_mirror"
    assert hit.target_mtime is None
    assert hit.lag_s is None


def test_missing_mirror_and_stale_mirror_are_distinguishable(tmp_path):
    home = _migrated_home(tmp_path)
    mapping = build_readonly_registries_step(data_home=home).mappings

    # provider-models: normal stale-mirror case (both exist, source newer)
    pm_source = next(m.source for m in mapping if m.name == "provider-models")
    pm_target = next(m.target for m in mapping if m.name == "provider-models")
    pm_target.parent.mkdir(parents=True, exist_ok=True)
    pm_target.write_text('{"schema": 1, "data": {}}', encoding="utf-8")
    pm_source.parent.mkdir(parents=True, exist_ok=True)
    pm_source.write_text('{"claude": "sonnet"}', encoding="utf-8")
    _touch_future(pm_source)

    # custom-roles: missing-mirror case (source exists, target never created)
    from agent_takkub import config

    cr_source = config.SETTINGS_HOME / "custom-roles.json"
    cr_source.write_text("{}", encoding="utf-8")

    hits = {h.name: h for h in scan_v1_only_writes(data_home=home)}
    assert hits["provider-models"].reason == "stale_mirror"
    assert hits["custom-roles"].reason == "missing_mirror"


# ── #502/#504 review 2026-09-07: role-providers fan-out (global + per-project
#    role-providers.json merged into one routing.json target) — #480 drifted
#    exactly here; the old scan skipped this domain entirely. ───────────────


def test_role_providers_fanout_missing_target_is_a_hit(tmp_path):
    home = _migrated_home(tmp_path)
    from agent_takkub import config

    # Global scope is sourced from `role-models.json` now (B-H2, 2026-09-07
    # round-2 review) — #515 archives the global `role-providers.json` on
    # first read, so it's no longer a live V1 source.
    global_source = config.SETTINGS_HOME / "role-models.json"
    global_source.write_text('{"backend": {"provider": "claude"}}', encoding="utf-8")
    # routing.json target deliberately never created

    hits = scan_v1_only_writes(data_home=home)
    hit = next(h for h in hits if h.name == "role-providers:global")
    assert hit.reason == "missing_mirror"


def test_role_providers_fanout_stale_global_source_is_a_hit(tmp_path):
    home = _migrated_home(tmp_path)
    from agent_takkub import config
    from agent_takkub.core.migration.steps_v1 import RoleAgentMigrationStep

    target = RoleAgentMigrationStep(data_home=home)._routing_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"schema": 1, "global": {}, "projects": {}}', encoding="utf-8")

    global_source = config.SETTINGS_HOME / "role-models.json"
    global_source.write_text('{"backend": {"provider": "claude"}}', encoding="utf-8")
    _touch_future(global_source)

    hits = scan_v1_only_writes(data_home=home)
    hit = next(h for h in hits if h.name == "role-providers:global")
    assert hit.reason == "stale_mirror"
    assert hit.lag_s > 0


def test_role_providers_fanout_stale_project_scope_is_a_hit(tmp_path):
    """The per-project scope must be checked too, not just global — #480's
    drift was project-scoped."""
    home = _migrated_home(tmp_path)
    from agent_takkub import config
    from agent_takkub.core.migration.steps_v1 import RoleAgentMigrationStep

    (home / "projects.json").write_text('{"projects": {"proj_a": {"paths": {}}}}', encoding="utf-8")
    target = RoleAgentMigrationStep(data_home=home)._routing_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"schema": 1, "global": {}, "projects": {}}', encoding="utf-8")

    (config.SETTINGS_HOME / "role-models.json").write_text("{}", encoding="utf-8")
    proj_source = config.SETTINGS_HOME / "projects" / "proj_a" / "role-providers.json"
    proj_source.parent.mkdir(parents=True, exist_ok=True)
    proj_source.write_text('{"backend": "codex"}', encoding="utf-8")
    _touch_future(proj_source)

    hits = scan_v1_only_writes(data_home=home)
    hit = next(h for h in hits if h.name == "role-providers:proj_a")
    assert hit.reason == "stale_mirror"


def test_role_providers_fanout_no_hit_right_after_dual_write(tmp_path):
    home = _migrated_home(tmp_path)
    from agent_takkub import config
    from agent_takkub.core.storage import dual_write

    global_source = config.SETTINGS_HOME / "role-models.json"
    global_source.write_text('{"backend": {"provider": "claude"}}', encoding="utf-8")
    dual_write.dual_write_routing({"backend": "claude"}, {}, data_home=home)

    hits = scan_v1_only_writes(data_home=home)
    assert not any(h.name.startswith("role-providers") for h in hits)


def test_role_providers_fanout_no_source_no_hit(tmp_path):
    """No V1 role-providers.json anywhere (global or per-project) — nothing
    has been written yet, so there's nothing to flag as missing/stale."""
    home = _migrated_home(tmp_path)
    hits = scan_v1_only_writes(data_home=home)
    assert not any(h.name.startswith("role-providers") for h in hits)
