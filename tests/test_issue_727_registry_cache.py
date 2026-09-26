"""#727: parser-specific cache entries and project-registry self-repair."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import auto_migrate_boot, cached_read, config, core_v2_settings
from agent_takkub.core.storage.layout import storage_layout_v2
from agent_takkub.core.storage.legacy_reader import read_json


@pytest.fixture(autouse=True)
def _clear_read_cache():
    cached_read.invalidate()
    yield
    cached_read.invalidate()


def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "agent-takkub-home"
    home.mkdir()
    monkeypatch.setattr(config, "DATA_HOME", home)
    monkeypatch.setattr(config, "PROJECTS_JSON", home / "projects.json")
    monkeypatch.setattr(
        config,
        "_v2_project_registry_path",
        lambda: storage_layout_v2(home).projects_root / "registry.json",
    )
    return config._v2_project_registry_path()


def test_legacy_read_then_load_projects_uses_distinct_parsers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = _isolate_registry(tmp_path, monkeypatch)
    registry_path.parent.mkdir(parents=True)
    document = {
        "schema": 1,
        "migrated_from": "projects.json",
        "data": {"active": "demo", "projects": {"demo": {"paths": {}}}},
    }
    registry_path.write_text(json.dumps(document), encoding="utf-8")

    # This is the collision from #727: the legacy reader first caches the
    # raw envelope, while the project reader must independently unwrap it.
    assert read_json(registry_path) == document
    assert config.load_projects() == document["data"]


def test_boot_migration_raw_probe_then_project_load_keeps_registry_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = _isolate_registry(tmp_path, monkeypatch)
    settings_home = tmp_path / "settings-home"
    settings_home.mkdir()
    monkeypatch.setattr(config, "SETTINGS_HOME", settings_home)
    monkeypatch.setattr(config, "RUNTIME_DIR", config.DATA_HOME / "runtime")
    monkeypatch.setattr(auto_migrate_boot, "is_dev_checkout", lambda: False)
    (config.DATA_HOME / "projects.json").write_text(
        json.dumps({"active": "demo", "projects": {"demo": {"paths": {}}}}),
        encoding="utf-8",
    )
    core_v2_settings._reset_cache()
    try:
        result = auto_migrate_boot.run_boot_stage()
    finally:
        core_v2_settings._reset_cache()
    assert result.action == "applied"

    # Boot's migration probes use the legacy raw-JSON reader on the target;
    # the application's project reader must still see its unwrapped domain.
    assert read_json(registry_path)["data"]["projects"].keys() == {"demo"}
    assert config.load_projects() == {"active": "demo", "projects": {"demo": {"paths": {}}}}


def test_invalidate_removes_every_parser_for_path(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text('{"value": 1}', encoding="utf-8")
    calls = {"first": 0, "second": 0}

    def parse_first(text: str) -> dict:
        calls["first"] += 1
        return json.loads(text)

    def parse_second(text: str) -> dict:
        calls["second"] += 1
        return json.loads(text)

    cached_read.read_cached(path, parse_first)
    cached_read.read_cached(path, parse_second)
    assert len([key for key in cached_read._cache if key[0] == str(path)]) == 2

    cached_read.invalidate(path)

    cached_read.read_cached(path, parse_first)
    cached_read.read_cached(path, parse_second)
    assert calls == {"first": 2, "second": 2}


def test_save_rejects_registry_envelope_data_without_touching_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = _isolate_registry(tmp_path, monkeypatch)
    registry_path.parent.mkdir(parents=True)
    original = {"schema": 1, "data": {"active": None, "projects": {}}}
    registry_path.write_text(json.dumps(original), encoding="utf-8")

    assert config.save_projects_json({"data": {"active": None, "projects": {}}}) is False
    assert json.loads(registry_path.read_text(encoding="utf-8")) == original


@pytest.mark.parametrize("depth", [2, 3, 4])
def test_nested_registry_unions_projects_and_saves_one_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int
) -> None:
    registry_path = _isolate_registry(tmp_path, monkeypatch)
    registry_path.parent.mkdir(parents=True)
    inner = {
        "active": "inner",
        "open_tabs": ["inner"],
        "projects": {"same": {"v": "inner"}, "inner": {}},
    }
    for level in range(2, depth + 1):
        inner = {
            "active": f"level-{level}",
            "open_tabs": [f"level-{level}"],
            "projects": {"same": {"v": f"level-{level}"}, f"level-{level}": {}},
            "data": inner,
            "schema": level,
        }
    registry_path.write_text(
        json.dumps({"schema": 1, "migrated_from": "legacy", "data": inner}),
        encoding="utf-8",
    )

    loaded = config.load_projects()
    assert loaded == {
        "active": f"level-{depth}",
        "open_tabs": [f"level-{depth}"],
        "projects": {
            "same": {"v": f"level-{depth}"},
            **{f"level-{level}": {} for level in range(2, depth + 1)},
            "inner": {},
        },
    }
    assert not {"data", "schema", "migrated_from", "migrated_at"}.intersection(loaded)

    assert config.save_projects_json(loaded) is True
    saved = json.loads(registry_path.read_text(encoding="utf-8"))
    assert saved["data"] == loaded
    assert "data" not in saved["data"]
    assert config.load_projects() == loaded
