"""#566: `config.load_projects()`/`save_projects_json()` must keep working
through the #504 boot-time migration ladder — `ArchiveV1LegacyStep` moves
`projects.json` into `backups/v1-archive-<ts>/` once
`core.migration.steps_v1.ProjectMigrationStep` has promoted it into the V2
project registry, and the read/write side must follow that move instead of
silently seeing zero projects.

Deliberately its own file, separate from `test_auto_migrate_boot.py`: it
only ever *calls* `auto_migrate_boot.run_boot_stage()` / `is_dev_checkout()`
as a black box (never edits that module), so it stays out of the way of
whoever owns the migration ladder implementation itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import auto_migrate_boot, config, core_v2_settings
from agent_takkub.core.storage.layout import layout_state


@pytest.fixture
def isolated_data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_home = tmp_path / "data_home"
    settings_home = tmp_path / "settings_home"
    data_home.mkdir()
    settings_home.mkdir()
    monkeypatch.setattr(config, "DATA_HOME", data_home)
    monkeypatch.setattr(config, "SETTINGS_HOME", settings_home)
    monkeypatch.setattr(config, "RUNTIME_DIR", data_home / "runtime")
    monkeypatch.setattr(config, "PROJECTS_JSON", data_home / "projects.json")
    monkeypatch.setattr(auto_migrate_boot, "is_dev_checkout", lambda: False)
    core_v2_settings._reset_cache()
    yield data_home
    core_v2_settings._reset_cache()


def test_project_registry_survives_migration_edit_and_reboot(
    isolated_data_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """repro `mixed_boot_projects`: a real machine with one project boots
    into 2.1.0 (full ladder apply, archiving `projects.json`), a project is
    then loaded/edited/saved through the same application API
    `project_wizard.py` uses, and the cockpit reboots again — every project
    must still be there, and `projects.json` must never reappear at the top
    of `DATA_HOME` (that would flip `layout_state()` back to `"mixed"`)."""
    data_home = isolated_data_home
    (data_home / "projects.json").write_text(
        json.dumps({"active": "demo", "projects": {"demo": {"paths": {"web": "/tmp/demo"}}}}),
        encoding="utf-8",
    )

    # Boot 1 — fresh "v1" machine, full ladder apply() + validate().
    result1 = auto_migrate_boot.run_boot_stage()
    assert result1.action == "applied"
    assert not (data_home / "projects.json").exists()
    assert layout_state(data_home) == "v2"

    assert config.load_projects() == {
        "active": "demo",
        "projects": {"demo": {"paths": {"web": "/tmp/demo"}}},
    }
    assert config.active_project()[0] == "demo"

    # Load + edit + save through the same API project_wizard.py uses.
    data = config.load_projects()
    data["projects"]["new-one"] = {"paths": {"web": "/tmp/new-one"}}
    data["active"] = "new-one"
    assert config.save_projects_json(data) is True

    # Never resurrected at the top level — would flip layout_state() back
    # to "mixed" (layout.py's own v1_markers check).
    assert not (data_home / "projects.json").exists()
    assert layout_state(data_home) == "v2"
    assert config.load_projects() == {
        "active": "new-one",
        "projects": {
            "demo": {"paths": {"web": "/tmp/demo"}},
            "new-one": {"paths": {"web": "/tmp/new-one"}},
        },
    }

    # Boot 2 (reboot) — apply_pending() path this time; nothing lost.
    result2 = auto_migrate_boot.run_boot_stage()
    assert result2.action == "pending_applied"
    assert not (data_home / "projects.json").exists()
    assert layout_state(data_home) == "v2"
    assert config.load_projects() == {
        "active": "new-one",
        "projects": {
            "demo": {"paths": {"web": "/tmp/demo"}},
            "new-one": {"paths": {"web": "/tmp/new-one"}},
        },
    }


def test_fresh_boot_with_no_projects_reads_as_empty(
    isolated_data_home: Path,
) -> None:
    """#504 item 4 case — a genuinely fresh machine with no `projects.json`
    at all boots straight to the promoted layout with no projects, and
    `load_projects()`/`active_project()` read that as empty, not an error."""
    data_home = isolated_data_home
    result = auto_migrate_boot.run_boot_stage()
    assert result.action == "applied"
    assert not (data_home / "projects.json").exists()
    assert config.list_project_names() == []
    assert config.active_project() == (None, {})


def test_dev_checkout_still_reads_and_writes_projects_json_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dev checkout never runs the boot migration ladder
    (`is_dev_checkout()` gates it off entirely, `config.DATA_HOME ==
    config.REPO_ROOT`) — `load_projects()`/`save_projects_json()` must keep
    reading/writing the plain V1 file exactly as before #566; the V2
    registry lookup is a harmless `.exists()` check under the (nested,
    dev-only) `v2/` root that never exists there."""
    pj = tmp_path / "projects.json"
    pj.write_text(json.dumps({"active": "demo", "projects": {"demo": {}}}), encoding="utf-8")
    monkeypatch.setattr(config, "DATA_HOME", config.REPO_ROOT)
    monkeypatch.setattr(config, "PROJECTS_JSON", pj)
    assert auto_migrate_boot.is_dev_checkout() is True

    assert config.load_projects() == {"active": "demo", "projects": {"demo": {}}}
    assert config.save_projects_json({"active": None, "projects": {}}) is True
    assert json.loads(pj.read_text(encoding="utf-8")) == {"active": None, "projects": {}}


# --- review 2026-09-23: a failed registry read must never be persisted as
# "no projects" (config.py load_projects fail-open → set_open_tabs /
# clear_active_project wrote {"projects": {}} over the only copy). ----------

_REAL = {
    "schema": 1,
    "migrated_from": "x",
    "data": {
        "active": "demo",
        "projects": {"demo": {"paths": {"web": "/tmp/demo"}}, "other": {"paths": {}}},
        "open_tabs": ["demo", "other"],
    },
}


@pytest.fixture
def seeded_registry(isolated_data_home: Path) -> Path:
    """A migrated machine: V2 registry with two projects, V1 file archived."""
    registry_path = config._v2_project_registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(_REAL), encoding="utf-8")
    assert not (isolated_data_home / "projects.json").exists()
    assert type(config.load_projects()) is dict  # a good read is a plain dict
    return registry_path


def _fail_next_read_of(monkeypatch: pytest.MonkeyPatch, target: Path, exc: Exception) -> dict:
    """Make exactly ONE `Path.read_text` of *target* raise *exc* (an AV/backup
    tool holding the file for a moment); every later read succeeds."""
    orig = Path.read_text
    state = {"fired": 0}

    def flaky(self: Path, *a, **kw):
        if self == target and not state["fired"]:
            state["fired"] += 1
            raise exc
        return orig(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", flaky)
    return state


def test_transient_read_error_in_set_open_tabs_does_not_wipe_registry(
    seeded_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifier repro: one PermissionError on the read inside
    `set_open_tabs()` used to persist `{"active": None, "projects": {},
    "open_tabs": []}` over the real registry (WIPED True)."""
    state = _fail_next_read_of(
        monkeypatch, seeded_registry, PermissionError(13, "sharing violation")
    )
    config.set_open_tabs(["demo", "other"])
    assert state["fired"] == 1
    assert json.loads(seeded_registry.read_text(encoding="utf-8")) == _REAL
    # and the next (successful) write still goes through normally
    config.set_open_tabs(["other"])
    assert json.loads(seeded_registry.read_text(encoding="utf-8"))["data"]["open_tabs"] == ["other"]


def test_transient_read_error_in_clear_active_project_does_not_wipe_registry(
    seeded_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_next_read_of(monkeypatch, seeded_registry, OSError(5, "I/O error"))
    config.clear_active_project()
    assert json.loads(seeded_registry.read_text(encoding="utf-8")) == _REAL


def test_corrupt_registry_reads_as_unreadable_and_writers_keep_it(
    seeded_registry: Path,
) -> None:
    """Truncated registry after a power loss: readers fail open to an empty
    list (marked `UnreadableProjects`), and every writer refuses to replace
    the corrupt-but-repairable file with a valid empty one."""
    truncated = json.dumps(_REAL)[:40]
    seeded_registry.write_text(truncated, encoding="utf-8")

    data = config.load_projects()
    assert isinstance(data, config.UnreadableProjects)
    assert data == {"active": None, "projects": {}}
    assert config.list_project_names() == []
    assert config.active_project() == (None, {})
    assert config.get_open_tabs() == []

    config.set_open_tabs(["demo"])
    config.clear_active_project()
    assert config.set_active_project("demo") is False
    # project_wizard shape: mutate the loaded doc in place, then save
    data["projects"]["new-one"] = {"paths": {}}
    data["active"] = "new-one"
    assert config.save_projects_json(data) is False
    # a fresh doc built by hand is refused too while the on-disk file is unreadable
    assert config.save_projects_json({"active": None, "projects": {"z": {}}}) is False
    assert seeded_registry.read_text(encoding="utf-8") == truncated


def test_registry_without_data_object_is_not_an_empty_project_list(
    seeded_registry: Path,
) -> None:
    seeded_registry.write_text(json.dumps({"schema": 1, "data": None}), encoding="utf-8")
    data = config.load_projects()
    assert isinstance(data, config.UnreadableProjects)
    assert config.save_projects_json(data) is False
    assert json.loads(seeded_registry.read_text(encoding="utf-8")) == {"schema": 1, "data": None}


def test_unreadable_v1_projects_json_is_refused_by_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dev checkout / pre-migration: same rule for the plain V1 file."""
    pj = tmp_path / "projects.json"
    pj.write_text('{"active": "demo", "projects": {"demo": {}', encoding="utf-8")
    monkeypatch.setattr(config, "DATA_HOME", config.REPO_ROOT)
    monkeypatch.setattr(config, "PROJECTS_JSON", pj)
    assert auto_migrate_boot.is_dev_checkout() is True

    data = config.load_projects()
    assert isinstance(data, config.UnreadableProjects)
    data["active"] = None
    assert config.save_projects_json(data) is False
    assert pj.read_text(encoding="utf-8") == '{"active": "demo", "projects": {"demo": {}'


def test_missing_store_is_a_plain_empty_dict_that_writers_accept(
    isolated_data_home: Path,
) -> None:
    """Fresh machine (#504 item 4): no registry, no V1 file → a genuinely
    empty list, NOT the unreadable marker — the first project must save."""
    data = config.load_projects()
    assert type(data) is dict
    assert data == {"active": None, "projects": {}}
    data["projects"]["first"] = {"paths": {}}
    assert config.save_projects_json(data) is True
    assert config.list_project_names() == ["first"]
