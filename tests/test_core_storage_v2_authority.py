"""`core.storage.v2_authority` (#362 Phase 10 wave 2 — "readers") + the V1
modules wired to consult it. Every scenario below proves the same three-way
contract: flag OFF (default) never touches v2 at all; flag ON + a good v2
mirror answers from v2; flag ON + a missing/corrupt v2 mirror falls back to
V1 exactly as if the flag were off, never raising."""

from __future__ import annotations

import json

import pytest

from agent_takkub.core.migration.steps_v1 import (
    ProjectMigrationStep,
    RoleAgentMigrationStep,
    build_capability_step,
    build_readonly_registries_step,
    build_state_step,
)
from agent_takkub.core.storage import v2_authority
from agent_takkub.core.storage.legacy_reader import read_json

# ── flag ──────────────────────────────────────────────────────────────────


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("TAKKUB_V2_AUTHORITY", raising=False)
    assert v2_authority.v2_authority_enabled() is False


def test_flag_on_when_env_is_1(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "1")
    assert v2_authority.v2_authority_enabled() is True


def test_flag_off_for_any_other_env_value(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "true")
    assert v2_authority.v2_authority_enabled() is False


def test_flag_env_wins_over_settings_toggle(monkeypatch):
    from agent_takkub import core_v2_settings

    monkeypatch.setattr(core_v2_settings, "flag_enabled", lambda name: True, raising=False)
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "0")
    assert v2_authority.v2_authority_enabled() is False


def test_flag_falls_back_to_settings_toggle_when_env_unset(monkeypatch):
    from agent_takkub import core_v2_settings

    monkeypatch.delenv("TAKKUB_V2_AUTHORITY", raising=False)
    monkeypatch.setattr(
        core_v2_settings, "flag_enabled", lambda name: name == "v2_authority", raising=False
    )
    assert v2_authority.v2_authority_enabled() is True


def test_core_v2_settings_defaults_v2_authority_off(monkeypatch, tmp_path):
    """The other 5 `TAKKUB_V2_*` flags default ON (1.0.84) — this one must
    NOT inherit that sweep (see `core_v2_settings._DEFAULT_FLAGS`'s own
    comment for why)."""
    from agent_takkub import core_v2_settings

    monkeypatch.setattr(core_v2_settings, "path", lambda: tmp_path / "core-v2-settings.json")
    core_v2_settings._reset_cache()
    flags = core_v2_settings.load()["flags"]
    assert flags["v2_authority"] is False
    assert flags["router"] is True  # sanity: the sweep itself still applies to the others


# ── reader helpers ──────────────────────────────────────────────────────────


def _migrated_home(tmp_path):
    home = tmp_path / "data_home"
    (home / "v2").mkdir(parents=True)
    return home


# ── flat registries: role-models / provider-models / disabled-providers ────


@pytest.mark.parametrize(
    "dual_write_fn,read_fn,step_builder,mapping_name",
    [
        (
            "dual_write_role_models",
            "read_role_models",
            build_readonly_registries_step,
            "role-models",
        ),
        (
            "dual_write_provider_models",
            "read_provider_models",
            build_readonly_registries_step,
            "provider-models",
        ),
        (
            "dual_write_disabled_providers",
            "read_disabled_providers",
            build_readonly_registries_step,
            "disabled-providers",
        ),
        (
            "dual_write_pane_tools_policy",
            "read_pane_tools_policy",
            build_capability_step,
            "pane-tools",
        ),
        ("dual_write_skill_policy", "read_skill_policy", build_capability_step, "skill-policy"),
    ],
)
def test_flat_registry_reader_returns_none_when_not_migrated(
    tmp_path, dual_write_fn, read_fn, step_builder, mapping_name
):
    home = tmp_path / "data_home"  # no v2/
    read = getattr(v2_authority, read_fn)
    assert read(data_home=home) is None


@pytest.mark.parametrize(
    "dual_write_fn,read_fn,step_builder,mapping_name,payload",
    [
        (
            "dual_write_role_models",
            "read_role_models",
            build_readonly_registries_step,
            "role-models",
            {"backend": {"provider": "codex", "model": "gpt-5.6"}},
        ),
        (
            "dual_write_provider_models",
            "read_provider_models",
            build_readonly_registries_step,
            "provider-models",
            {"claude": "claude-sonnet-5"},
        ),
        (
            "dual_write_disabled_providers",
            "read_disabled_providers",
            build_readonly_registries_step,
            "disabled-providers",
            {"codex": True},
        ),
        (
            "dual_write_pane_tools_policy",
            "read_pane_tools_policy",
            build_capability_step,
            "pane-tools",
            {"version": 1, "roles": {"backend": {"mcps": ["x"], "plugins": []}}},
        ),
        (
            "dual_write_skill_policy",
            "read_skill_policy",
            build_capability_step,
            "skill-policy",
            {"version": 1, "roles": {"backend": ["skill-a"]}},
        ),
    ],
)
def test_flat_registry_reader_returns_written_payload(
    tmp_path, dual_write_fn, read_fn, step_builder, mapping_name, payload
):
    from agent_takkub.core.storage import dual_write

    home = _migrated_home(tmp_path)
    getattr(dual_write, dual_write_fn)(payload, data_home=home)

    read = getattr(v2_authority, read_fn)
    assert read(data_home=home) == payload


@pytest.mark.parametrize(
    "read_fn,step_builder,mapping_name",
    [
        ("read_role_models", build_readonly_registries_step, "role-models"),
        ("read_pane_tools_policy", build_capability_step, "pane-tools"),
    ],
)
def test_flat_registry_reader_returns_none_and_logs_on_corrupt_mirror(
    tmp_path, caplog, read_fn, step_builder, mapping_name
):
    home = _migrated_home(tmp_path)
    mapping = step_builder(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == mapping_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not valid json", encoding="utf-8")

    read = getattr(v2_authority, read_fn)
    with caplog.at_level("WARNING", logger="agent_takkub.core.storage.v2_authority"):
        result = read(data_home=home)
    assert result is None
    assert any("v2_read_failed" in r.message for r in caplog.records)


# ── routing (role-agent fan-out) ─────────────────────────────────────────────


def test_read_routing_returns_none_when_not_migrated(tmp_path):
    home = tmp_path / "data_home"
    assert v2_authority.read_routing(data_home=home) is None


def test_read_routing_returns_global_and_projects(tmp_path):
    from agent_takkub.core.storage import dual_write

    home = _migrated_home(tmp_path)
    dual_write.dual_write_routing(
        {"backend": "codex"}, {"proj-a": {"qa": "gemini"}}, data_home=home
    )

    result = v2_authority.read_routing(data_home=home)
    assert result == {"global": {"backend": "codex"}, "projects": {"proj-a": {"qa": "gemini"}}}


def test_read_routing_returns_none_on_corrupt_mirror(tmp_path, caplog):
    home = _migrated_home(tmp_path)
    target = RoleAgentMigrationStep(data_home=home)._routing_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level("WARNING", logger="agent_takkub.core.storage.v2_authority"):
        result = v2_authority.read_routing(data_home=home)
    assert result is None
    assert any("v2_read_failed" in r.message for r in caplog.records)


# ── custom roles registry ───────────────────────────────────────────────────


def test_read_custom_roles_registry_returns_written_payload(tmp_path):
    from agent_takkub.core.storage import dual_write

    home = _migrated_home(tmp_path)
    payload = {"version": 1, "roles": {"data-eng": {"label": "Data Eng", "color": "#94a3b8"}}}
    dual_write.dual_write_custom_roles_registry(payload, data_home=home)

    assert v2_authority.read_custom_roles_registry(data_home=home) == payload


# ── projects registry ────────────────────────────────────────────────────────


def test_read_projects_registry_returns_written_document(tmp_path):
    from agent_takkub.core.storage import dual_write

    home = _migrated_home(tmp_path)
    data = {"active": "proj-a", "projects": {"proj-a": {"description": "d"}}}
    dual_write.dual_write_projects(data, data_home=home)

    assert v2_authority.read_projects_registry(data_home=home) == data


# ── local-issues (source-path gated) ────────────────────────────────────────


def test_read_local_issues_matches_only_the_cockpit_bug_source(tmp_path):
    from agent_takkub.core.storage import dual_write

    home = _migrated_home(tmp_path)
    issues = [{"number": 1, "title": "x"}]
    source = home / ".takkub_issues.json"
    dual_write.dual_write_local_issues(issues, source, data_home=home)

    assert v2_authority.read_local_issues(source, data_home=home) == issues
    other = tmp_path / "some_project" / ".takkub_issues.json"
    assert v2_authority.read_local_issues(other, data_home=home) is None


# ── issue-dedup / autoresume-shaped / remote-sessions ───────────────────────


def test_read_issue_dedup_returns_written_state(tmp_path):
    from agent_takkub.core.storage import dual_write

    home = _migrated_home(tmp_path)
    state = {"ValueError:app.py:57": 1234.5}
    dual_write.dual_write_issue_dedup(state, data_home=home)

    assert v2_authority.read_issue_dedup(data_home=home) == state


def test_read_remote_sessions_returns_written_doc(tmp_path):
    from agent_takkub.core.storage import dual_write

    home = _migrated_home(tmp_path)
    doc = {"fingerprint": "abc", "sessions": {"hash1": 123.0}}
    dual_write.dual_write_remote_sessions(doc, data_home=home)

    assert v2_authority.read_remote_sessions(data_home=home) == doc


def test_read_remote_sessions_returns_none_after_clear(tmp_path):
    from agent_takkub.core.storage import dual_write

    home = _migrated_home(tmp_path)
    dual_write.dual_write_remote_sessions({"fingerprint": "abc", "sessions": {}}, data_home=home)
    dual_write.dual_write_remote_sessions(None, data_home=home)

    assert v2_authority.read_remote_sessions(data_home=home) is None


# ── authority_state() (doctor --storage-layout, plan §3 Wave E item 3) ─────


def test_authority_state_v1_when_not_migrated(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "1")
    assert v2_authority.authority_state(tmp_path / "data_home") == "v1"


def test_authority_state_mixed_when_migrated_but_flag_off(tmp_path, monkeypatch):
    monkeypatch.delenv("TAKKUB_V2_AUTHORITY", raising=False)
    home = _migrated_home(tmp_path)
    (home / "runtime").mkdir()  # a V1 marker so layout_state() reports "mixed"
    assert v2_authority.authority_state(home) == "mixed"


def test_authority_state_v2_when_migrated_and_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "1")
    home = _migrated_home(tmp_path)
    (home / "runtime").mkdir()
    assert v2_authority.authority_state(home) == "v2"


# ── V1 module integration: real writer -> dual-write -> flag-gated reader ──


def _wire_authority_home(monkeypatch, home):
    """Point BOTH `dual_write` (the write side) and `v2_authority` (the read
    side) at the same isolated data_home — they each bind their own copy of
    `_effective_data_home` (import-time reference, not a live link), so both
    need patching independently for a real V1 writer's dual-write call and
    this test's own `v2_authority_enabled()` reads to agree on where `v2/`
    is."""
    monkeypatch.setattr(
        "agent_takkub.core.storage.dual_write._effective_data_home", lambda dh=None: home
    )
    monkeypatch.setattr(
        "agent_takkub.core.storage.v2_authority._effective_data_home", lambda dh=None: home
    )


def test_role_models_reads_from_v2_when_flag_on(monkeypatch, tmp_path):
    from agent_takkub import role_models

    home = _migrated_home(tmp_path)
    _wire_authority_home(monkeypatch, home)
    monkeypatch.setattr(role_models, "_PATH", tmp_path / "role-models.json")
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "1")

    role_models.set_model("backend", "codex", "gpt-5.6")
    assert role_models.model_for("backend", "codex") == "gpt-5.6"

    # Prove it's actually reading v2, not just getting lucky because both
    # sides agree: hand-edit the v2 mirror directly (bypassing dual-write)
    # to a DIFFERENT value and confirm the getter follows it.
    mapping = build_readonly_registries_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "role-models")
    doc = read_json(target)
    doc["data"] = {"backend": {"provider": "codex", "model": "gpt-5.6-terra"}}
    target.write_text(json.dumps(doc), encoding="utf-8")
    assert role_models.model_for("backend", "codex") == "gpt-5.6-terra"

    # Flag off: back to V1's own (unchanged) file, ignoring the v2 edit above.
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "0")
    assert role_models.model_for("backend", "codex") == "gpt-5.6"


def test_role_models_falls_back_to_v1_when_v2_mirror_missing(monkeypatch, tmp_path):
    from agent_takkub import role_models

    home = tmp_path / "data_home"  # never migrated
    _wire_authority_home(monkeypatch, home)
    monkeypatch.setattr(role_models, "_PATH", tmp_path / "role-models.json")
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "1")

    role_models.set_model("backend", "codex", "gpt-5.6")
    assert role_models.model_for("backend", "codex") == "gpt-5.6"


def test_provider_models_reads_from_v2_when_flag_on(monkeypatch, tmp_path):
    from agent_takkub import provider_models

    home = _migrated_home(tmp_path)
    _wire_authority_home(monkeypatch, home)
    monkeypatch.setattr(provider_models, "_PATH", tmp_path / "provider-models.json")
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "1")

    provider_models.set_model("claude", "claude-sonnet-5")
    mapping = build_readonly_registries_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "provider-models")
    doc = read_json(target)
    doc["data"] = {"claude": "claude-opus-5"}
    target.write_text(json.dumps(doc), encoding="utf-8")

    assert provider_models.model_for("claude") == "claude-opus-5"


def test_provider_state_reads_from_v2_when_flag_on(monkeypatch, tmp_path):
    from agent_takkub import provider_state

    home = _migrated_home(tmp_path)
    _wire_authority_home(monkeypatch, home)
    monkeypatch.setattr(provider_state, "_PATH", tmp_path / "disabled-providers.json")
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "1")

    provider_state.set_disabled("codex", True)
    mapping = build_readonly_registries_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "disabled-providers")
    doc = read_json(target)
    doc["data"] = {"codex": False}
    target.write_text(json.dumps(doc), encoding="utf-8")

    assert provider_state.is_disabled("codex") is False


def test_pane_tools_policy_reads_from_v2_when_flag_on(monkeypatch, tmp_path):
    from agent_takkub import pane_tools_policy

    home = _migrated_home(tmp_path)
    _wire_authority_home(monkeypatch, home)
    monkeypatch.setattr(pane_tools_policy, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "1")

    pane_tools_policy.set_role_items("backend", "mcps", ["alpha"])
    mapping = build_capability_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "pane-tools")
    doc = read_json(target)
    doc["data"]["roles"]["backend"]["mcps"] = ["beta"]
    target.write_text(json.dumps(doc), encoding="utf-8")

    assert pane_tools_policy.effective_mcps("backend") == frozenset({"beta"})


def test_skill_policy_reads_from_v2_when_flag_on(monkeypatch, tmp_path):
    from agent_takkub import skill_policy

    home = _migrated_home(tmp_path)
    _wire_authority_home(monkeypatch, home)
    monkeypatch.setattr(skill_policy, "SKILL_POLICY_FILE", tmp_path / "skill-policy.json")
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "1")

    skill_policy.set_role_skills("backend", ["skill-a"])
    mapping = build_capability_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "skill-policy")
    doc = read_json(target)
    doc["data"]["roles"]["backend"] = ["skill-b"]
    target.write_text(json.dumps(doc), encoding="utf-8")

    assert skill_policy.effective_skills("backend") == ["skill-b"]


def test_custom_roles_reads_from_v2_when_flag_on(monkeypatch, tmp_path):
    from agent_takkub import custom_roles

    home = _migrated_home(tmp_path)
    _wire_authority_home(monkeypatch, home)
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "1")

    ok, err = custom_roles.create_role("data-eng", "Data Eng", "#94a3b8", 2, 5)
    assert ok, err
    target = RoleAgentMigrationStep(data_home=home)._custom_roles_target()
    doc = read_json(target)
    doc["data"]["roles"]["data-eng"]["label"] = "Data Engineer"
    target.write_text(json.dumps(doc), encoding="utf-8")

    assert custom_roles.load_custom_roles()["data-eng"].label == "Data Engineer"


def test_provider_config_routing_reads_v2_global_when_flag_on(monkeypatch, tmp_path):
    from agent_takkub import config, provider_config

    home = _migrated_home(tmp_path)
    _wire_authority_home(monkeypatch, home)
    monkeypatch.setattr(provider_config, "_CONFIG_PATH", tmp_path / "role-providers.json")
    monkeypatch.setattr(provider_config, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "list_project_names", lambda: [])
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "1")

    provider_config.save_providers({"backend": "codex"})
    target = RoleAgentMigrationStep(data_home=home)._routing_target()
    doc = read_json(target)
    doc["global"] = {"backend": "gemini"}
    target.write_text(json.dumps(doc), encoding="utf-8")

    assert provider_config.load_providers() == {"backend": "gemini"}


def test_provider_config_routing_reads_v2_project_scope_when_flag_on(monkeypatch, tmp_path):
    from agent_takkub import config, provider_config

    home = _migrated_home(tmp_path)
    _wire_authority_home(monkeypatch, home)
    monkeypatch.setattr(provider_config, "_CONFIG_PATH", tmp_path / "role-providers.json")
    monkeypatch.setattr(provider_config, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "list_project_names", lambda: ["proj-a"])
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "1")

    provider_config.save_providers({"qa": "gemini"}, project="proj-a")
    target = RoleAgentMigrationStep(data_home=home)._routing_target()
    doc = read_json(target)
    doc["projects"]["proj-a"] = {"qa": "opencode"}
    target.write_text(json.dumps(doc), encoding="utf-8")

    assert provider_config.load_providers("proj-a") == {"qa": "opencode"}


def test_config_load_projects_reads_from_v2_when_flag_on(monkeypatch, tmp_path):
    from agent_takkub import config

    home = _migrated_home(tmp_path)
    _wire_authority_home(monkeypatch, home)
    monkeypatch.setattr(config, "PROJECTS_JSON", tmp_path / "projects.json")
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "1")

    config.save_projects_json({"active": "proj-a", "projects": {"proj-a": {"description": "d"}}})
    target = ProjectMigrationStep(data_home=home)._registry_target()
    doc = read_json(target)
    doc["data"] = {"active": "proj-b", "projects": {"proj-b": {"description": "e"}}}
    target.write_text(json.dumps(doc), encoding="utf-8")

    assert config.load_projects() == {
        "active": "proj-b",
        "projects": {"proj-b": {"description": "e"}},
    }


def test_remote_session_store_reads_from_v2_when_flag_on(monkeypatch, tmp_path):
    from agent_takkub.remote import session_store

    home = _migrated_home(tmp_path)
    _wire_authority_home(monkeypatch, home)
    monkeypatch.setattr(session_store, "_PATH", tmp_path / "takkub-remote-sessions.json")
    monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "1")

    session_store.save("fp-1", {"hash1": 999.0})
    mapping = build_state_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "remote-sessions")
    doc = read_json(target)
    doc["data"] = {"fingerprint": "fp-1", "sessions": {"hash1": 111.0}}
    target.write_text(json.dumps(doc), encoding="utf-8")

    assert session_store.load("fp-1") == {"hash1": 111.0}


def test_v1_writers_stay_correct_when_flag_off_despite_stale_v2(monkeypatch, tmp_path):
    """Parity's other half: flag OFF must ignore v2 entirely, even a v2
    mirror that's been hand-edited to something different — #362's own
    'ปิด flag = พฤติกรรมเดิมเป๊ะ' rule."""
    from agent_takkub import role_models

    home = _migrated_home(tmp_path)
    _wire_authority_home(monkeypatch, home)
    monkeypatch.setattr(role_models, "_PATH", tmp_path / "role-models.json")
    monkeypatch.delenv("TAKKUB_V2_AUTHORITY", raising=False)

    role_models.set_model("backend", "codex", "gpt-5.6")
    mapping = build_readonly_registries_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "role-models")
    doc = read_json(target)
    doc["data"] = {"backend": {"provider": "codex", "model": "gpt-5.6-terra"}}
    target.write_text(json.dumps(doc), encoding="utf-8")

    assert role_models.model_for("backend", "codex") == "gpt-5.6"
