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
    """Building the layout must never touch disk — even when a legacy
    nested v2/ folder happens to exist on disk at this data_home, root must
    still resolve the same pure way (#504: no disk stat inside this
    function)."""
    layout = storage_layout_v2(tmp_path / "data_home")
    assert not (tmp_path / "data_home").exists()
    assert layout.root == tmp_path / "data_home"


def test_storage_layout_v2_root_is_data_home_itself(tmp_path):
    """#504 item 6: the physical root is DATA_HOME directly — the pre-#504
    nested `v2/` folder is retired. Collision with V1's own top-level
    `projects/`/`runtime/`/`cache/` names is avoided on disk by
    `core.migration.promote_v1.ArchiveV1LegacyStep` moving V1's leftovers
    into `backups/v1-archive-<ts>/` BEFORE anything reuses those names, not
    by nesting V2's paths under a different directory name."""
    home = tmp_path / "data_home"
    layout = storage_layout_v2(home)
    assert layout.root == home
    assert layout.projects_root == home / "projects"
    assert layout.runtime == home / "runtime"
    assert layout.cache == home / "cache"


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
    assert layout.root == tmp_path


def test_layout_state_v1_when_nothing_migrated(tmp_path):
    home = tmp_path / "data_home"
    home.mkdir()
    assert layout_state(home) == "v1"


def test_layout_state_v1_when_only_real_v1_data_present(tmp_path):
    """A genuinely pre-#504 machine (real V1 files, no legacy v2/, no
    top-level V2 marker) is still plain `"v1"` — no ladder step has run at
    all yet."""
    home = tmp_path / "data_home"
    home.mkdir()
    (home / "projects.json").write_text("{}", encoding="utf-8")
    assert layout_state(home) == "v1"


def test_layout_state_mixed_when_legacy_v2_root_still_on_disk(tmp_path):
    """A pre-#504 nested v2/ folder existing at all means "promote hasn't
    run yet" — regardless of whether a top-level V2 marker also exists."""
    home = tmp_path / "data_home"
    (home / "v2").mkdir(parents=True)
    (home / "projects.json").write_text("{}", encoding="utf-8")
    assert layout_state(home) == "mixed"


def test_layout_state_v2_once_promoted_and_v1_leftovers_archived(tmp_path):
    """The steady end state (#504 item 6: no more `"mixed"` once the
    boot-time ladder has finished) — top-level V2 marker present, no legacy
    v2/ root, no V1 leftover files."""
    home = tmp_path / "data_home"
    (home / "system").mkdir(parents=True)
    (home / "system" / "version.json").write_text("{}", encoding="utf-8")
    assert layout_state(home) == "v2"


def test_layout_state_permanent_infra_never_counts_as_a_v1_marker(tmp_path):
    """`runtime/` (#504 item 8: never touched by the archive step) and
    `SETTINGS_HOME` (== DATA_HOME on every real, non-dev install) must
    never make `"v2"` unreachable the way the pre-#504 marker set did."""
    home = tmp_path / "data_home"
    (home / "system").mkdir(parents=True)
    (home / "system" / "version.json").write_text("{}", encoding="utf-8")
    (home / "runtime").mkdir()
    assert layout_state(home) == "v2"


def test_legacy_mapping_covers_every_ladder_step():
    steps_present = {e.ladder_step for e in LEGACY_MAPPING if e.ladder_step > 0}
    assert steps_present == {1, 2, 3, 4, 5, 6, 7, 8}


def test_legacy_mapping_entries_have_semantics_and_are_not_blindly_unknown():
    """Migration rule #1: 'Do not move files blindly' — every scheduled
    (ladder_step > 0) row must have a real semantics tag, not UNKNOWN."""
    for entry in LEGACY_MAPPING:
        if entry.ladder_step > 0:
            assert entry.semantics != LegacySemantics.UNKNOWN, entry.v1_path
        assert entry.v1_path
        assert entry.v2_path
