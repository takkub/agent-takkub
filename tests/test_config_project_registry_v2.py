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
