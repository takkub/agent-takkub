"""`core.storage.dual_write` (#362 Phase 10 wave 1) — every V1 config writer
mirrors into `v2/` best-effort, without ever touching V1's own success/
failure. Each test passes `data_home` explicitly (never relies on the
no-arg default) so this file is independent of `tests/conftest.py`'s
storage_layout_v2 isolation wiring — the same style `test_core_storage_
layout.py` already uses."""

from __future__ import annotations

from agent_takkub.core.migration.steps_v1 import (
    ProjectMigrationStep,
    RoleAgentMigrationStep,
    build_capability_step,
    build_readonly_registries_step,
)
from agent_takkub.core.storage import dual_write
from agent_takkub.core.storage.layout import storage_layout_v2
from agent_takkub.core.storage.legacy_reader import read_json


def _migrated_home(tmp_path):
    home = tmp_path / "data_home"
    (home / "v2").mkdir(parents=True)
    return home


# ── not migrated: every dual_write_* is a silent no-op ─────────────────────


def test_role_models_skips_when_not_migrated(tmp_path):
    home = tmp_path / "data_home"  # no v2/ created
    dual_write.dual_write_role_models(
        {"backend": {"provider": "codex", "model": "gpt"}}, data_home=home
    )
    assert not (home / "v2").exists()


def test_projects_skips_when_not_migrated(tmp_path):
    home = tmp_path / "data_home"
    dual_write.dual_write_projects({"active": None, "projects": {}}, data_home=home)
    assert not (home / "v2").exists()


# ── flat registries: role_models / provider_models ─────────────────────────


def test_role_models_mirrors_into_aliases_target(tmp_path):
    home = _migrated_home(tmp_path)
    entries = {"backend": {"provider": "codex", "model": "gpt-5.6"}}
    dual_write.dual_write_role_models(entries, data_home=home)

    target = build_readonly_registries_step(data_home=home).mappings
    aliases_target = next(m.target for m in target if m.name == "role-models")
    written = read_json(aliases_target)
    assert written["data"] == entries
    assert written["schema"] == 1


def test_provider_models_mirrors_into_registry_target(tmp_path):
    home = _migrated_home(tmp_path)
    models = {"claude": "claude-sonnet-5"}
    dual_write.dual_write_provider_models(models, data_home=home)

    mapping = build_readonly_registries_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "provider-models")
    assert read_json(target)["data"] == models


# ── capability: pane_tools_policy / skill_policy ────────────────────────────


def test_pane_tools_policy_mirrors_payload(tmp_path):
    home = _migrated_home(tmp_path)
    payload = {"version": 1, "roles": {"backend": {"mcps": ["x"], "plugins": []}}}
    dual_write.dual_write_pane_tools_policy(payload, data_home=home)

    mapping = build_capability_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "pane-tools")
    assert read_json(target)["data"] == payload


def test_pane_tools_policy_empty_delete_mirrors_empty_data(tmp_path):
    home = _migrated_home(tmp_path)
    dual_write.dual_write_pane_tools_policy({}, data_home=home)

    mapping = build_capability_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "pane-tools")
    assert read_json(target)["data"] == {}


def test_skill_policy_mirrors_payload(tmp_path):
    home = _migrated_home(tmp_path)
    payload = {"version": 1, "roles": {"backend": ["skill-a"]}}
    dual_write.dual_write_skill_policy(payload, data_home=home)

    mapping = build_capability_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "skill-policy")
    assert read_json(target)["data"] == payload


# ── routing (role-agent fan-out) ────────────────────────────────────────────


def test_routing_writes_the_merged_payload_it_is_given(tmp_path):
    """`dual_write_routing` takes the already-merged {global, projects} data
    as arguments — it never re-derives V1 paths itself (that's
    `provider_config.save_providers()`'s job, see `test_provider_config.py`
    for the integration proof), specifically so `config.py` (a leaf module)
    never has to transitively import `provider_config` to reach this."""
    home = _migrated_home(tmp_path)
    global_data = {"backend": "codex"}
    projects = {"proj-a": {"qa": "gemini"}}

    dual_write.dual_write_routing(global_data, projects, data_home=home)

    target = RoleAgentMigrationStep(data_home=home)._routing_target()
    written = read_json(target)
    assert written["global"] == global_data
    assert written["projects"] == projects


def test_routing_skips_when_not_migrated(tmp_path):
    home = tmp_path / "data_home"
    dual_write.dual_write_routing({"backend": "codex"}, {}, data_home=home)
    assert not (home / "v2").exists()


# ── custom roles: registry + per-role .md ───────────────────────────────────


def test_custom_roles_registry_mirrors_payload(tmp_path):
    home = _migrated_home(tmp_path)
    payload = {"version": 1, "roles": {"data-eng": {"label": "Data Eng", "color": "#94a3b8"}}}
    dual_write.dual_write_custom_roles_registry(payload, data_home=home)

    target = RoleAgentMigrationStep(data_home=home)._custom_roles_target()
    assert read_json(target)["data"] == payload


def test_custom_role_file_mirrors_and_removes(tmp_path):
    home = _migrated_home(tmp_path)
    dual_write.dual_write_custom_role_file("data-eng", "# Data Eng\n\ncontent", data_home=home)

    layout = storage_layout_v2(home)
    target = layout.agents / "custom" / "data-eng.md"
    assert target.read_text(encoding="utf-8") == "# Data Eng\n\ncontent"

    dual_write.dual_write_custom_role_file("data-eng", None, data_home=home)
    assert not target.exists()


# ── projects: registry + per-project fan-out ────────────────────────────────


def test_projects_mirrors_registry_and_per_project_files(tmp_path):
    home = _migrated_home(tmp_path)
    data = {
        "active": "proj-a",
        "projects": {
            "proj-a": {"description": "d", "paths": {"web": "/tmp/w"}, "presets": []},
        },
    }
    (home / "worktrees" / "proj-a").mkdir(parents=True)
    (home / "worktrees" / "proj-a" / "wt1").mkdir()

    dual_write.dual_write_projects(data, data_home=home)

    step = ProjectMigrationStep(data_home=home)
    registry = read_json(step._registry_target())
    assert registry["data"] == data

    project_doc = read_json(step._project_target("proj-a"))
    assert project_doc["data"] == data["projects"]["proj-a"]
    assert project_doc["worktrees_owned"] == ["wt1"]


# ── V2 write failure never raises ───────────────────────────────────────────


def test_v2_write_failure_is_swallowed_not_raised(tmp_path, caplog):
    home = _migrated_home(tmp_path)
    mapping = build_readonly_registries_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "role-models")
    # Make the target's parent directory impossible to create: a FILE
    # already occupies that path.
    target.parent.parent.mkdir(parents=True, exist_ok=True)
    target.parent.write_text("blocking file", encoding="utf-8")

    with caplog.at_level("WARNING", logger="agent_takkub.core.storage.dual_write"):
        dual_write.dual_write_role_models(
            {"backend": {"provider": "codex", "model": "x"}}, data_home=home
        )

    assert any("v2_write_failed" in r.message for r in caplog.records)
