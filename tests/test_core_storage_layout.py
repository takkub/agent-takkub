"""`core.storage.layout` — StorageLayoutV2/ProjectLayoutV2 shape, purity
(no disk I/O), layout_state() detection, and the V1->V2 mapping table
(#309 Phase 8b, docs/v2/V2_IMPLEMENTATION_PLAN.md §5.3)."""

from __future__ import annotations

from agent_takkub.core.storage import layout as layout_mod
from agent_takkub.core.storage.layout import (
    LEGACY_MAPPING,
    LegacySemantics,
    layout_state,
    storage_layout_v2,
)


def test_storage_layout_v2_is_pure_path_arithmetic(tmp_path):
    """Building the layout must never touch disk."""
    layout = storage_layout_v2(tmp_path / "data_home")
    assert not (tmp_path / "data_home").exists()
    assert layout.root == tmp_path / "data_home" / "v2"


def test_storage_layout_v2_nests_under_v2_to_avoid_v1_name_collision(tmp_path):
    home = tmp_path / "data_home"
    layout = storage_layout_v2(home)
    # V1 already owns `projects/`, `runtime/`, `cache/` at DATA_HOME's top
    # level with a different shape — V2 must not reuse those exact paths.
    assert layout.projects_root != home / "projects"
    assert layout.runtime != home / "runtime"
    assert layout.cache != home / "cache"
    assert str(layout.projects_root).startswith(str(home / "v2"))


def test_storage_layout_v2_system_paths(tmp_path):
    layout = storage_layout_v2(tmp_path)
    assert layout.system_version_json == layout.system / "version.json"
    assert layout.system_migrations == layout.system / "migrations"
    assert layout.system_backups == layout.system / "backups"
    assert layout.system_schemas == layout.system / "schemas"


def test_storage_layout_v2_state_buckets(tmp_path):
    layout = storage_layout_v2(tmp_path)
    for attr in ("providers", "accounts", "sessions", "tasks", "issues", "registry"):
        assert getattr(layout, f"state_{attr}") == layout.root / "state" / attr


def test_project_layout_matches_task_spec(tmp_path):
    layout = storage_layout_v2(tmp_path)
    proj = layout.project("demo-project")
    assert proj.root == layout.projects_root / "demo-project"
    assert proj.project_json == proj.root / "project.json"
    for name in (
        "worktrees",
        "artifacts",
        "conversations",
        "brain",
        "checkpoints",
        "logs",
        "state",
    ):
        assert getattr(proj, name) == proj.root / name


def test_default_data_home_reads_config_data_home(monkeypatch, tmp_path):
    monkeypatch.setattr(layout_mod.config, "DATA_HOME", tmp_path)
    layout = storage_layout_v2()
    assert layout.root == tmp_path / "v2"


def test_layout_state_v1_when_no_v2_root(tmp_path):
    home = tmp_path / "data_home"
    home.mkdir()
    assert layout_state(home) == "v1"


def test_layout_state_mixed_when_both_present(tmp_path, monkeypatch):
    home = tmp_path / "data_home"
    (home / "v2").mkdir(parents=True)
    (home / "projects.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(layout_mod.config, "SETTINGS_HOME", home / "settings")
    assert layout_state(home) == "mixed"


def test_layout_state_v2_when_v1_markers_gone(tmp_path, monkeypatch):
    home = tmp_path / "data_home"
    (home / "v2").mkdir(parents=True)
    monkeypatch.setattr(layout_mod.config, "SETTINGS_HOME", tmp_path / "nonexistent-settings")
    assert layout_state(home) == "v2"


def test_legacy_mapping_covers_every_ladder_step():
    steps_present = {e.ladder_step for e in LEGACY_MAPPING if e.ladder_step > 0}
    assert steps_present == {1, 2, 3, 4, 5, 6, 7}


def test_legacy_mapping_entries_have_semantics_and_are_not_blindly_unknown():
    """Migration rule #1: 'Do not move files blindly' — every scheduled
    (ladder_step > 0) row must have a real semantics tag, not UNKNOWN."""
    for entry in LEGACY_MAPPING:
        if entry.ladder_step > 0:
            assert entry.semantics != LegacySemantics.UNKNOWN, entry.v1_path
        assert entry.v1_path
        assert entry.v2_path
