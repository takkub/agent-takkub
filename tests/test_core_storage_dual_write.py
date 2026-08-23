"""`core.storage.dual_write` (#362 Phase 10 wave 1) — every V1 config writer
mirrors into `v2/` best-effort, without ever touching V1's own success/
failure. Each test passes `data_home` explicitly (never relies on the
no-arg default) so this file is independent of `tests/conftest.py`'s
storage_layout_v2 isolation wiring — the same style `test_core_storage_
layout.py` already uses."""

from __future__ import annotations

import json

from agent_takkub.core.migration.steps_v1 import (
    ProjectMigrationStep,
    RoleAgentMigrationStep,
    build_capability_step,
    build_readonly_registries_step,
    build_state_step,
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


def test_disabled_providers_mirrors_into_registry_target(tmp_path):
    """#362 wave 1c — `provider_state.save()` was the first known gap in
    ladder step 1: a mapping existed in `build_readonly_registries_step`
    with no `dual_write_*` hook wired to it."""
    home = _migrated_home(tmp_path)
    state = {"codex": True}
    dual_write.dual_write_disabled_providers(state, data_home=home)

    mapping = build_readonly_registries_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "disabled-providers")
    assert read_json(target)["data"] == state


def test_exec_mode_mirrors_into_execution_target(tmp_path):
    """#362 wave 1c — `exec_mode.set_current()` was the second known gap."""
    home = _migrated_home(tmp_path)
    payload = {"mode": "parallel"}
    dual_write.dual_write_exec_mode(payload, data_home=home)

    mapping = build_readonly_registries_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "exec-mode")
    assert read_json(target)["data"] == payload


def test_rtk_enabled_mirrors_into_features_target(tmp_path):
    """#362 wave 1c — `rtk_helper.set_rtk_enabled()` was found by the
    mechanical audit below (same shape as the two known gaps above, not
    previously flagged)."""
    home = _migrated_home(tmp_path)
    payload = {"enabled": True}
    dual_write.dual_write_rtk_enabled(payload, data_home=home)

    mapping = build_readonly_registries_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "rtk-enabled")
    assert read_json(target)["data"] == payload


def test_readonly_registries_skip_when_not_migrated(tmp_path):
    home = tmp_path / "data_home"
    dual_write.dual_write_disabled_providers({"codex": True}, data_home=home)
    dual_write.dual_write_exec_mode({"mode": "parallel"}, data_home=home)
    dual_write.dual_write_rtk_enabled({"enabled": True}, data_home=home)
    assert not (home / "v2").exists()


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


# ── state: local-issues / issue-dedup / autoresume / remote-sessions ───────


def test_local_issues_mirrors_when_path_matches_mapping_source(tmp_path):
    home = _migrated_home(tmp_path)
    issues = [{"number": 1, "title": "x"}]
    source = home / ".takkub_issues.json"

    dual_write.dual_write_local_issues(issues, source, data_home=home)

    mapping = build_state_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "local-issues")
    assert read_json(target)["data"] == issues


def test_local_issues_skips_for_a_project_scoped_path(tmp_path):
    """Only the cockpit-bug instance (DATA_HOME/.takkub_issues.json) has a V2
    target — a per-project `.takkub_issues.json` at an unrelated cwd must not
    write anything into V2 at all."""
    home = _migrated_home(tmp_path)
    other_project = tmp_path / "some_project" / ".takkub_issues.json"

    dual_write.dual_write_local_issues([{"number": 1}], other_project, data_home=home)

    mapping = build_state_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "local-issues")
    assert not target.exists()


def test_local_issues_skips_when_not_migrated(tmp_path):
    home = tmp_path / "data_home"
    dual_write.dual_write_local_issues(
        [{"number": 1}], home / ".takkub_issues.json", data_home=home
    )
    assert not (home / "v2").exists()


def test_issue_dedup_mirrors_payload(tmp_path):
    home = _migrated_home(tmp_path)
    state = {"ValueError:app.py:57": 1234.5}
    dual_write.dual_write_issue_dedup(state, data_home=home)

    mapping = build_state_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "issue-dedup")
    assert read_json(target)["data"] == state


def test_autoresume_mirrors_payload(tmp_path):
    home = _migrated_home(tmp_path)
    dual_write.dual_write_autoresume({"enabled": True}, data_home=home)

    mapping = build_state_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "autoresume")
    assert read_json(target)["data"] == {"enabled": True}


def test_remote_sessions_mirrors_payload(tmp_path):
    home = _migrated_home(tmp_path)
    doc = {"fingerprint": "abc", "sessions": {"hash1": 123.0}}
    dual_write.dual_write_remote_sessions(doc, data_home=home)

    mapping = build_state_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "remote-sessions")
    assert read_json(target)["data"] == doc


def test_remote_sessions_none_removes_v2_copy(tmp_path):
    home = _migrated_home(tmp_path)
    dual_write.dual_write_remote_sessions({"fingerprint": "abc", "sessions": {}}, data_home=home)

    mapping = build_state_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "remote-sessions")
    assert target.exists()

    dual_write.dual_write_remote_sessions(None, data_home=home)
    assert not target.exists()


def test_state_step_validates_after_dual_write_mirrors_match_v1(tmp_path):
    """After every step-5 writer's dual-write call, the ladder's own
    `RegistryCopyStep.validate()` (which `migrate validate` calls) must see
    the V2 mirrors as matching their V1 sources."""
    home = _migrated_home(tmp_path)
    settings = tmp_path / "settings_home"
    settings.mkdir()

    issues_doc = [{"number": 1, "title": "x"}]
    (home / ".takkub_issues.json").write_text(json.dumps(issues_doc), encoding="utf-8")
    dedup_doc = {"ValueError:app.py:57": 1234.5}
    (home / "auto_issue_dedup.json").write_text(json.dumps(dedup_doc), encoding="utf-8")
    autoresume_doc = {"enabled": True}
    (settings / "autoresume.json").write_text(json.dumps(autoresume_doc), encoding="utf-8")
    remote_doc = {"fingerprint": "abc", "sessions": {}}
    (settings / "takkub-remote-sessions.json").write_text(json.dumps(remote_doc), encoding="utf-8")

    dual_write.dual_write_local_issues(issues_doc, home / ".takkub_issues.json", data_home=home)
    dual_write.dual_write_issue_dedup(dedup_doc, data_home=home)
    dual_write.dual_write_autoresume(autoresume_doc, data_home=home)
    dual_write.dual_write_remote_sessions(remote_doc, data_home=home)

    step = build_state_step(data_home=home, settings_home=settings)
    report = step.validate()
    assert report.ok, report.detail


def test_state_writers_skip_when_not_migrated(tmp_path):
    home = tmp_path / "data_home"
    dual_write.dual_write_issue_dedup({"x": 1.0}, data_home=home)
    dual_write.dual_write_autoresume({"enabled": True}, data_home=home)
    dual_write.dual_write_remote_sessions({"fingerprint": "a", "sessions": {}}, data_home=home)
    assert not (home / "v2").exists()


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


def test_state_writer_v2_write_failure_is_swallowed_not_raised(tmp_path, caplog):
    home = _migrated_home(tmp_path)
    mapping = build_state_step(data_home=home).mappings
    target = next(m.target for m in mapping if m.name == "autoresume")
    target.parent.parent.mkdir(parents=True, exist_ok=True)
    target.parent.write_text("blocking file", encoding="utf-8")

    with caplog.at_level("WARNING", logger="agent_takkub.core.storage.dual_write"):
        dual_write.dual_write_autoresume({"enabled": True}, data_home=home)

    assert any("v2_write_failed" in r.message for r in caplog.records)


# ── mechanical audit (#362 wave 1c): every ladder mapping → dual_write hook ─
#
# V1 file                              -> V2 target                          -> writer module        -> dual-write hooked?
# settings/provider-models.json        -> models/registry.json               -> provider_models.py    -> yes (dual_write_provider_models)
# settings/role-models.json            -> models/aliases.json                -> role_models.py        -> yes (dual_write_role_models)
# settings/disabled-providers.json     -> providers/registry.json            -> provider_state.py      -> yes (dual_write_disabled_providers) [wave 1c: was missing]
# settings/exec-mode.json              -> config/execution.json              -> exec_mode.py           -> yes (dual_write_exec_mode)           [wave 1c: was missing]
# settings/rtk-enabled.json            -> config/features/rtk.json           -> rtk_helper.py          -> yes (dual_write_rtk_enabled)         [wave 1c: was missing]
# settings/pane-tools.json             -> capabilities/mcp/permissions.json  -> pane_tools_policy.py   -> yes (dual_write_pane_tools_policy)
# settings/skill-policy.json           -> capabilities/skills/registry.json  -> skill_policy.py        -> yes (dual_write_skill_policy)
# DATA_HOME/.takkub_issues.json        -> state/issues/local.json            -> issues.py              -> yes (dual_write_local_issues)
# DATA_HOME/auto_issue_dedup.json      -> state/issues/dedup.json            -> auto_issue_capture.py  -> yes (dual_write_issue_dedup)
# settings/autoresume.json             -> state/sessions/autoresume.json     -> auto_resume.py         -> yes (dual_write_autoresume)
# settings/takkub-remote-sessions.json -> state/sessions/remote.json         -> remote/session_store.py -> yes (dual_write_remote_sessions)
# settings/custom-roles.json           -> agents/custom/registry.json        -> custom_roles.py        -> yes (dual_write_custom_roles_registry) [step 2, not RegistryMapping]
# custom_agents_dir/<role>.md          -> agents/custom/<role>.md            -> custom_roles.py        -> yes (dual_write_custom_role_file)      [step 2, not RegistryMapping]
# role-providers.json (global+project) -> config/routing.json                -> provider_config.py     -> yes (dual_write_routing)               [step 2, not RegistryMapping]
# DATA_HOME/projects.json              -> projects/registry.json + per-id    -> config.py              -> yes (dual_write_projects)              [step 4, not RegistryMapping]
# (none — computed, not a V1 writer)   -> providers/<p>/provider.json etc.   -> (n/a)                  -> exception: step 6 has no V1 payload to mirror (CredentialReferenceStep computes fresh from config-dir presence every apply())
# RUNTIME_DIR/{tasks,sessions,...}     -> state_* trees                     -> (many, unbounded)      -> exception: step 7, documented in dual_write.py's module docstring (per-file fan-out, too costly to hook live; synced by apply_pending() at boot)
# RUNTIME_DIR/core                     -> system/                           -> (n/a — already V2-shaped) -> exception: step 8, documented in dual_write.py's module docstring (not a V1 config mirror)

# Ladder step 1/3/5 mapping name -> the dual_write_* function wired to it.
# A name intentionally does NOT collapse to `dual_write_<name.replace("-", "_")}`
# in every case (e.g. "pane-tools" -> `dual_write_pane_tools_policy`), so this
# table is spelled out explicitly rather than derived — the whole point is to
# catch a NEW mapping added to one of the three step-1/3/5 factories below
# without anyone adding a line here (or to `dual_write.py`) at the same time.
_FLAT_MAPPING_DUAL_WRITE_FNS: dict[str, str] = {
    # ladder step 1 — readonly-registries
    "provider-models": "dual_write_provider_models",
    "role-models": "dual_write_role_models",
    "disabled-providers": "dual_write_disabled_providers",
    "exec-mode": "dual_write_exec_mode",
    "rtk-enabled": "dual_write_rtk_enabled",
    # ladder step 3 — capability
    "pane-tools": "dual_write_pane_tools_policy",
    "skill-policy": "dual_write_skill_policy",
    # ladder step 5 — state
    "local-issues": "dual_write_local_issues",
    "issue-dedup": "dual_write_issue_dedup",
    "autoresume": "dual_write_autoresume",
    "remote-sessions": "dual_write_remote_sessions",
}


def test_every_ladder_mapping_has_dual_write_or_documented_exception(tmp_path):
    """Mechanical drift guard (#362 wave 1c). Ladder steps 1/3/5 are built
    from flat `RegistryMapping` tuples — this walks every mapping those
    three step factories actually report today and cross-checks it against
    `_FLAT_MAPPING_DUAL_WRITE_FNS` above. Fails if:

    - a mapping exists in the ladder with no entry here (the exact gap
      `disabled-providers`/`exec-mode`/`rtk-enabled` shipped with before
      this test existed — a future new mapping would silently ship the
      same way without this), or
    - an entry here no longer matches any live mapping (stale audit row),
      or
    - the named `dual_write_*` function was renamed/removed from
      `dual_write.py` out from under the table.

    Steps 2/4/6/7/8 aren't `RegistryMapping`-based and are covered by the
    table comment above plus the two tests below instead."""
    home = tmp_path / "data_home"
    live_mapping_names = {
        m.name
        for step_factory in (
            build_readonly_registries_step,
            build_capability_step,
            build_state_step,
        )
        for m in step_factory(data_home=home).mappings
    }
    assert live_mapping_names, "sanity: the ladder steps must report at least one mapping"

    documented_names = set(_FLAT_MAPPING_DUAL_WRITE_FNS)
    missing = live_mapping_names - documented_names
    assert not missing, (
        f"ladder mapping(s) {sorted(missing)} have no dual_write_* hook and no "
        "entry in _FLAT_MAPPING_DUAL_WRITE_FNS (test_core_storage_dual_write.py) — "
        "add a dual_write_* function in dual_write.py, wire it into the V1 writer, "
        "and add it to this table"
    )
    stale = documented_names - live_mapping_names
    assert not stale, (
        f"_FLAT_MAPPING_DUAL_WRITE_FNS names {sorted(stale)} no longer exist in the ladder"
    )

    for mapping_name, fn_name in _FLAT_MAPPING_DUAL_WRITE_FNS.items():
        assert hasattr(dual_write, fn_name), (
            f"{mapping_name!r} names {fn_name}, which no longer exists on dual_write.py"
        )


def test_step_2_and_4_fanout_targets_have_dual_write_hooks():
    """Steps 2 (`RoleAgentMigrationStep`) and 4 (`ProjectMigrationStep`)
    build their V2 targets from step-instance methods, not a flat
    `RegistryMapping` tuple, so they fall outside the mechanical audit
    above — asserted directly here instead."""
    for fn_name in (
        "dual_write_custom_roles_registry",
        "dual_write_custom_role_file",
        "dual_write_routing",
        "dual_write_projects",
    ):
        assert hasattr(dual_write, fn_name), f"{fn_name} missing from dual_write.py"


def test_step_6_7_8_remain_the_documented_no_hook_exceptions():
    """Steps 6/7/8 deliberately have no `dual_write_*` hook — pinning each
    step's id here so a rename doesn't silently detach it from its written
    rationale (step 6: below; steps 7/8: `dual_write.py`'s module
    docstring). Step 6 (`CredentialReferenceStep`) computes its records
    fresh from provider config-dir presence on every `apply()` — there is
    no in-memory V1 payload a caller persists for dual-write to mirror,
    unlike every other step."""
    from agent_takkub.core.migration.steps_v1 import (
        CoreInternalStoreStep,
        CredentialReferenceStep,
        RuntimeTriageStep,
    )

    assert CredentialReferenceStep.step_id == "credential-reference"
    assert RuntimeTriageStep.step_id == "runtime-triage"
    assert CoreInternalStoreStep.step_id == "core-internal-store"
