"""V1->V2 ladder steps 1-7 (#309 Phase 8b, plan §5.3): dry-run/apply/
validate/rollback round-trip on synthetic V1 fixtures, run against BOTH an
installed-build-shaped layout (SETTINGS_HOME == DATA_HOME, mirrors
``~/.agent-takkub``) and a dev-checkout-shaped layout (SETTINGS_HOME
separate from DATA_HOME, mirrors ``~/.takkub``) via the `v1_homes` fixture,
per the task's own "ทั้ง ~/.agent-takkub layout และ ~/.takkub dev layout"
requirement.

Every test operates strictly under `tmp_path` — never the real
DATA_HOME/SETTINGS_HOME (mirrors `test_disk_usage.py`'s own rule).
"""

from __future__ import annotations

import json

import pytest

from agent_takkub.core.migration.backup import BackupManager
from agent_takkub.core.migration.journal import MigrationJournal
from agent_takkub.core.migration.steps_v1 import (
    CredentialReferenceStep,
    ProjectMigrationStep,
    RoleAgentMigrationStep,
    RuntimeTriageStep,
    build_capability_step,
    build_readonly_registries_step,
    build_state_step,
)
from agent_takkub.core.storage.jsonl_store import JsonlStore
from agent_takkub.core.storage.layout import storage_layout_v2
from agent_takkub.core.storage.legacy_reader import read_json


@pytest.fixture(params=["installed_merged", "dev_split"])
def v1_homes(request, tmp_path):
    """(data_home, settings_home) — 'installed_merged' mirrors an installed
    build (~/.agent-takkub layout, SETTINGS_HOME == DATA_HOME per
    `config._resolve_settings_home`); 'dev_split' mirrors a dev checkout
    (~/.takkub layout, SETTINGS_HOME separate from DATA_HOME)."""
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    if request.param == "installed_merged":
        settings_home = data_home
    else:
        settings_home = tmp_path / "settings_home"
        settings_home.mkdir()
    return data_home, settings_home


@pytest.fixture
def journal_backups(tmp_path):
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    backups = BackupManager(tmp_path / "backups")
    return journal, backups


def _apply_only(step):
    """inspect -> plan -> dry_run -> apply -> validate, WITHOUT rollback —
    for tests that need to inspect the written V2 content afterward."""
    inspect = step.inspect()
    assert inspect.ok, inspect.summary
    plan = step.plan()
    assert plan.ok, plan.summary
    dry = step.dry_run()
    assert dry.ok, dry.summary
    apply_report = step.apply()
    assert apply_report.ok, apply_report.summary
    validate_report = step.validate()
    assert validate_report.ok, validate_report.detail
    return apply_report


def _round_trip(step):
    """Full cycle including rollback — for tests that only care the whole
    ladder step is well-behaved, not the post-apply file content."""
    apply_report = _apply_only(step)
    rollback_report = step.rollback()
    assert rollback_report.ok, rollback_report.summary
    return apply_report


# ---------------------------------------------------------------------------
# Step 1 — readonly registries
# ---------------------------------------------------------------------------


def test_readonly_registries_round_trip_empty(v1_homes, journal_backups):
    data_home, settings_home = v1_homes
    journal, backups = journal_backups
    step = build_readonly_registries_step(
        journal, backups, data_home=data_home, settings_home=settings_home
    )
    _round_trip(step)


def test_readonly_registries_preserves_unknown_fields(v1_homes, journal_backups):
    data_home, settings_home = v1_homes
    journal, backups = journal_backups
    v1_content = {"backend": "claude", "future_field": {"nested": 1}}
    (settings_home / "provider-models.json").write_text(json.dumps(v1_content), encoding="utf-8")
    step = build_readonly_registries_step(
        journal, backups, data_home=data_home, settings_home=settings_home
    )
    apply_report = step.apply()
    assert apply_report.ok

    layout = storage_layout_v2(data_home)
    written = read_json(layout.models / "registry.json")
    assert written["data"] == v1_content  # nothing reshaped or dropped

    # V1 source itself is never touched (copy-never-move).
    assert (
        json.loads((settings_home / "provider-models.json").read_text(encoding="utf-8"))
        == v1_content
    )

    validate_report = step.validate()
    assert validate_report.ok
    rollback_report = step.rollback()
    assert rollback_report.ok
    assert not layout.models.joinpath("registry.json").exists()


def test_readonly_registries_rollback_restores_prior_v2_value(v1_homes, journal_backups):
    data_home, settings_home = v1_homes
    journal, backups = journal_backups
    layout = storage_layout_v2(data_home)
    layout.models.mkdir(parents=True)
    (layout.models / "registry.json").write_text(
        json.dumps({"schema": 1, "data": {"prior": True}}), encoding="utf-8"
    )
    (settings_home / "provider-models.json").write_text(json.dumps({"new": True}), encoding="utf-8")

    step = build_readonly_registries_step(
        journal, backups, data_home=data_home, settings_home=settings_home
    )
    step.apply()
    assert read_json(layout.models / "registry.json")["data"] == {"new": True}

    step.rollback()
    assert read_json(layout.models / "registry.json")["data"] == {"prior": True}


# ---------------------------------------------------------------------------
# Step 3 — capability
# ---------------------------------------------------------------------------


def test_capability_round_trip(v1_homes, journal_backups):
    data_home, settings_home = v1_homes
    journal, backups = journal_backups
    (settings_home / "skill-policy.json").write_text(
        json.dumps({"backend": ["some-skill"]}), encoding="utf-8"
    )
    step = build_capability_step(journal, backups, data_home=data_home, settings_home=settings_home)
    apply_report = _round_trip(step)
    assert "pane-tools" in apply_report.detail.get("written", [])


# ---------------------------------------------------------------------------
# Step 5 — state
# ---------------------------------------------------------------------------


def test_state_round_trip(v1_homes, journal_backups):
    data_home, settings_home = v1_homes
    journal, backups = journal_backups
    (data_home / ".takkub_issues.json").write_text(json.dumps([{"number": 1}]), encoding="utf-8")
    (settings_home / "autoresume.json").write_text(json.dumps({"on": True}), encoding="utf-8")
    step = build_state_step(journal, backups, data_home=data_home, settings_home=settings_home)
    _apply_only(step)
    layout = storage_layout_v2(data_home)
    assert read_json(layout.state_issues / "local.json")["data"] == [{"number": 1}]
    assert read_json(layout.state_sessions / "autoresume.json")["data"] == {"on": True}


# ---------------------------------------------------------------------------
# Step 2 — role / agent
# ---------------------------------------------------------------------------


def test_role_agent_round_trip_with_custom_role_and_project_routing(
    v1_homes, journal_backups, tmp_path
):
    data_home, settings_home = v1_homes
    journal, backups = journal_backups
    custom_agents_dir = tmp_path / "custom-agents"
    custom_agents_dir.mkdir()

    (settings_home / "custom-roles.json").write_text(
        json.dumps({"researcher": {"label": "Researcher", "color": "#123456"}}), encoding="utf-8"
    )
    (custom_agents_dir / "researcher.md").write_text("# Researcher role\n", encoding="utf-8")
    # #515: global routing lives in role-models.json now, not the (archived)
    # standalone role-providers.json — see steps_v1.py's
    # `_global_routing_source`.
    (settings_home / "role-models.json").write_text(
        json.dumps({"backend": {"provider": "codex"}}), encoding="utf-8"
    )

    (data_home / "projects.json").write_text(
        json.dumps({"active": "demo", "projects": {"demo": {"paths": {"web": "/tmp/web"}}}}),
        encoding="utf-8",
    )
    proj_routing_dir = settings_home / "projects" / "demo"
    proj_routing_dir.mkdir(parents=True)
    (proj_routing_dir / "role-providers.json").write_text(
        json.dumps({"qa": "gemini"}), encoding="utf-8"
    )

    step = RoleAgentMigrationStep(
        journal=journal,
        backups=backups,
        data_home=data_home,
        settings_home=settings_home,
        custom_agents_dir=custom_agents_dir,
    )
    apply_report = _apply_only(step)
    assert "researcher" in apply_report.detail["role_files"]

    layout = storage_layout_v2(data_home)
    registry = read_json(layout.agents / "custom" / "registry.json")
    assert registry["data"]["researcher"]["label"] == "Researcher"
    md = (layout.agents / "custom" / "researcher.md").read_text(encoding="utf-8")
    assert md == "# Researcher role\n"
    routing = read_json(layout.config_dir / "routing.json")
    assert routing["global"] == {"backend": "codex"}
    assert routing["projects"]["demo"] == {"qa": "gemini"}


def test_role_agent_apply_omits_project_with_no_v1_routing_file(v1_homes, journal_backups):
    """#480: `projects.json` can list a project that has never saved its own
    `role-providers.json` (never opened Providers & Roles for that tab).
    That project must get NO key in `routing.json["projects"]` — not a
    `{}` entry — so the V2 reader's "no entry -> inherit global" fallback
    fires for it exactly like V1's own `config_path(project).exists()`
    check does."""
    data_home, settings_home = v1_homes
    journal, backups = journal_backups
    # #515: global routing lives in role-models.json now — see
    # `test_role_agent_round_trip_with_custom_role_and_project_routing`.
    (settings_home / "role-models.json").write_text(
        json.dumps({"backend": {"provider": "codex"}}), encoding="utf-8"
    )
    (data_home / "projects.json").write_text(
        json.dumps({"active": "demo", "projects": {"demo": {"paths": {"web": "/tmp/web"}}}}),
        encoding="utf-8",
    )
    # No `settings_home/projects/demo/role-providers.json` written — "demo"
    # is a known project with no per-project override file.

    step = RoleAgentMigrationStep(
        journal=journal, backups=backups, data_home=data_home, settings_home=settings_home
    )
    _apply_only(step)

    layout = storage_layout_v2(data_home)
    routing = read_json(layout.config_dir / "routing.json")
    assert routing["global"] == {"backend": "codex"}
    assert "demo" not in routing["projects"]


def test_role_agent_reapply_clears_stale_empty_project_entry(v1_homes, journal_backups):
    """A prior (pre-#480) apply/dual-write could have left a bogus `{}`
    entry in `routing.json["projects"]` for a project with no V1 file.
    Re-running `apply()` (the migration ladder is copy-never-move, always a
    full overwrite of its target) must clear that stale entry, not merge
    with it."""
    data_home, settings_home = v1_homes
    journal, backups = journal_backups
    (settings_home / "role-models.json").write_text(
        json.dumps({"backend": {"provider": "codex"}}), encoding="utf-8"
    )
    (data_home / "projects.json").write_text(
        json.dumps({"active": "demo", "projects": {"demo": {"paths": {"web": "/tmp/web"}}}}),
        encoding="utf-8",
    )
    step = RoleAgentMigrationStep(
        journal=journal, backups=backups, data_home=data_home, settings_home=settings_home
    )
    layout = storage_layout_v2(data_home)
    target = layout.config_dir / "routing.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema": 1,
                "migrated_at": 0,
                "global": {"backend": "codex"},
                "projects": {"demo": {}},  # stale pre-fix entry
            }
        ),
        encoding="utf-8",
    )

    _apply_only(step)

    routing = read_json(target)
    assert "demo" not in routing["projects"]


def test_role_agent_apply_then_rollback_removes_written_md_file(
    v1_homes, journal_backups, tmp_path
):
    data_home, settings_home = v1_homes
    journal, backups = journal_backups
    custom_agents_dir = tmp_path / "custom-agents"
    custom_agents_dir.mkdir()
    (settings_home / "custom-roles.json").write_text(json.dumps({"analyst": {}}), encoding="utf-8")
    (custom_agents_dir / "analyst.md").write_text("analyst role", encoding="utf-8")

    step = RoleAgentMigrationStep(
        journal=journal,
        backups=backups,
        data_home=data_home,
        settings_home=settings_home,
        custom_agents_dir=custom_agents_dir,
    )
    step.apply()
    layout = storage_layout_v2(data_home)
    assert (layout.agents / "custom" / "analyst.md").exists()
    step.rollback()
    assert not (layout.agents / "custom" / "analyst.md").exists()

    # V1 source untouched throughout.
    assert (custom_agents_dir / "analyst.md").read_text(encoding="utf-8") == "analyst role"


# ---------------------------------------------------------------------------
# Step 4 — project
# ---------------------------------------------------------------------------


def test_project_round_trip_fans_out_and_records_worktree_ownership(v1_homes, journal_backups):
    data_home, _settings_home = v1_homes
    journal, backups = journal_backups
    (data_home / "projects.json").write_text(
        json.dumps(
            {
                "active": "demo",
                "projects": {
                    "demo": {"paths": {"web": "/tmp/web"}},
                    "other": {"paths": {"api": "/tmp/api"}},
                },
            }
        ),
        encoding="utf-8",
    )
    wt_dir = data_home / "worktrees" / "demo"
    (wt_dir / "backend-1").mkdir(parents=True)

    step = ProjectMigrationStep(journal=journal, backups=backups, data_home=data_home)
    apply_report = _apply_only(step)
    assert sorted(apply_report.detail["written"]) == ["demo", "other"]

    layout = storage_layout_v2(data_home)
    demo = read_json(layout.project("demo").project_json)
    assert demo["data"] == {"paths": {"web": "/tmp/web"}}
    assert demo["worktrees_owned"] == ["backend-1"]
    registry = read_json(layout.projects_root / "registry.json")
    assert "demo" in registry["data"]["projects"]


def test_project_step_never_copies_worktree_checkouts(v1_homes, journal_backups):
    data_home, _settings_home = v1_homes
    journal, backups = journal_backups
    (data_home / "projects.json").write_text(
        json.dumps({"active": "demo", "projects": {"demo": {"paths": {}}}}), encoding="utf-8"
    )
    wt_dir = data_home / "worktrees" / "demo" / "backend-1"
    wt_dir.mkdir(parents=True)
    (wt_dir / "some-real-file.py").write_text("code", encoding="utf-8")

    step = ProjectMigrationStep(journal=journal, backups=backups, data_home=data_home)
    step.apply()

    layout = storage_layout_v2(data_home)
    assert not layout.project("demo").worktrees.exists()  # no checkout bytes copied


# ---------------------------------------------------------------------------
# #605 — a step whose V1 source has already been archived away must never
# re-derive its V2 target from the now-missing source and overwrite
# already-migrated, real data with an empty payload.
# ---------------------------------------------------------------------------


def test_project_apply_keeps_registry_when_v1_source_gone_and_target_populated(
    v1_homes, journal_backups
):
    data_home, _settings_home = v1_homes
    journal, backups = journal_backups
    (data_home / "projects.json").write_text(
        json.dumps({"projects": {"demo": {"paths": {"web": "/tmp/web"}}}}), encoding="utf-8"
    )
    step = ProjectMigrationStep(journal=journal, backups=backups, data_home=data_home)
    _apply_only(step)

    layout = storage_layout_v2(data_home)
    registry_path = layout.projects_root / "registry.json"
    registry_before = read_json(registry_path)
    assert registry_before["data"]["projects"]["demo"]["paths"] == {"web": "/tmp/web"}

    (data_home / "projects.json").unlink()  # stands in for ArchiveV1LegacyStep archiving it
    assert step.source_retired() is True

    apply_report = step.apply()
    assert apply_report.ok
    assert apply_report.detail.get("kept") is True
    assert read_json(registry_path) == registry_before  # not wiped to {"projects": {}}


def test_project_rollback_after_kept_apply_preserves_registry_byte_identical(
    v1_homes, journal_backups
):
    """#605 M1: a "kept" target (V1 source retired) must survive a
    rollback byte-identical, not get deleted because the kept branch
    never took a backup for it to restore from."""
    data_home, _settings_home = v1_homes
    journal, backups = journal_backups
    (data_home / "projects.json").write_text(
        json.dumps({"projects": {"demo": {"paths": {"web": "/tmp/web"}}}}), encoding="utf-8"
    )
    step = ProjectMigrationStep(journal=journal, backups=backups, data_home=data_home)
    _apply_only(step)

    layout = storage_layout_v2(data_home)
    registry_path = layout.projects_root / "registry.json"
    registry_bytes_before = registry_path.read_bytes()

    (data_home / "projects.json").unlink()
    assert step.source_retired() is True
    apply_report = step.apply()
    assert apply_report.detail.get("kept") is True

    rollback_report = step.rollback()
    assert rollback_report.ok, rollback_report.summary
    assert registry_path.read_bytes() == registry_bytes_before


def test_project_apply_still_writes_empty_registry_when_nothing_was_ever_migrated(
    v1_homes, journal_backups
):
    """The #605 guard must only protect an already-populated target — a
    from-scratch install with no V1 `projects.json` and no prior registry
    still gets its (harmless, empty) registry written, exactly as before.
    `source_retired()` must stay False here even though the source is
    missing: "never had one" is not "was archived", and reporting retired
    too early would make the engine skip this step's very first real
    apply() forever, leaving the registry never created at all."""
    data_home, _settings_home = v1_homes
    journal, backups = journal_backups
    step = ProjectMigrationStep(journal=journal, backups=backups, data_home=data_home)
    assert step.source_retired() is False

    apply_report = step.apply()
    assert apply_report.ok
    assert not apply_report.detail.get("kept")

    layout = storage_layout_v2(data_home)
    registry = read_json(layout.projects_root / "registry.json")
    assert registry["data"] == {}


def test_role_agent_apply_keeps_registry_and_routing_when_v1_sources_gone(
    v1_homes, journal_backups
):
    data_home, settings_home = v1_homes
    journal, backups = journal_backups
    (settings_home / "custom-roles.json").write_text(
        json.dumps({"researcher": {"label": "Researcher"}}), encoding="utf-8"
    )
    (settings_home / "role-models.json").write_text(
        json.dumps({"backend": {"provider": "codex"}}), encoding="utf-8"
    )
    step = RoleAgentMigrationStep(
        journal=journal, backups=backups, data_home=data_home, settings_home=settings_home
    )
    _apply_only(step)

    layout = storage_layout_v2(data_home)
    registry_path = layout.agents / "custom" / "registry.json"
    routing_path = layout.config_dir / "routing.json"
    registry_before = read_json(registry_path)
    routing_before = read_json(routing_path)
    assert registry_before["data"]["researcher"]["label"] == "Researcher"
    assert routing_before["global"] == {"backend": "codex"}

    (settings_home / "custom-roles.json").unlink()
    (settings_home / "role-models.json").unlink()
    assert step.source_retired() is True

    apply_report = step.apply()
    assert apply_report.ok
    assert sorted(apply_report.detail.get("kept", [])) == ["registry", "routing"]
    assert read_json(registry_path) == registry_before
    assert read_json(routing_path) == routing_before


def test_role_agent_rollback_after_kept_apply_preserves_registry_and_routing(
    v1_homes, journal_backups
):
    """#605 M1: same byte-identical-after-rollback guarantee for the
    registry/routing pair `RoleAgentMigrationStep` keeps independently."""
    data_home, settings_home = v1_homes
    journal, backups = journal_backups
    (settings_home / "custom-roles.json").write_text(
        json.dumps({"researcher": {"label": "Researcher"}}), encoding="utf-8"
    )
    (settings_home / "role-models.json").write_text(
        json.dumps({"backend": {"provider": "codex"}}), encoding="utf-8"
    )
    step = RoleAgentMigrationStep(
        journal=journal, backups=backups, data_home=data_home, settings_home=settings_home
    )
    _apply_only(step)

    layout = storage_layout_v2(data_home)
    registry_path = layout.agents / "custom" / "registry.json"
    routing_path = layout.config_dir / "routing.json"
    registry_bytes_before = registry_path.read_bytes()
    routing_bytes_before = routing_path.read_bytes()

    (settings_home / "custom-roles.json").unlink()
    (settings_home / "role-models.json").unlink()
    apply_report = step.apply()
    assert sorted(apply_report.detail.get("kept", [])) == ["registry", "routing"]

    rollback_report = step.rollback()
    assert rollback_report.ok, rollback_report.summary
    assert registry_path.read_bytes() == registry_bytes_before
    assert routing_path.read_bytes() == routing_bytes_before


def test_readonly_registries_apply_keeps_target_when_v1_source_gone(v1_homes, journal_backups):
    data_home, settings_home = v1_homes
    journal, backups = journal_backups
    (settings_home / "provider-models.json").write_text(
        json.dumps({"claude": "opus"}), encoding="utf-8"
    )
    step = build_readonly_registries_step(
        journal, backups, data_home=data_home, settings_home=settings_home
    )
    _apply_only(step)

    target = next(m.target for m in step.mappings if m.name == "provider-models")
    before = read_json(target)
    assert before["data"] == {"claude": "opus"}

    (settings_home / "provider-models.json").unlink()

    apply_report = step.apply()
    assert apply_report.ok
    assert "provider-models" in apply_report.detail["kept"]
    assert read_json(target) == before  # not wiped


def test_readonly_registries_rollback_after_kept_apply_preserves_target_byte_identical(
    v1_homes, journal_backups
):
    """#605 M1: `RegistryCopyStep`'s own kept-branch must back up before
    skipping, so rollback restores rather than deletes."""
    data_home, settings_home = v1_homes
    journal, backups = journal_backups
    (settings_home / "provider-models.json").write_text(
        json.dumps({"claude": "opus"}), encoding="utf-8"
    )
    step = build_readonly_registries_step(
        journal, backups, data_home=data_home, settings_home=settings_home
    )
    _apply_only(step)

    target = next(m.target for m in step.mappings if m.name == "provider-models")
    target_bytes_before = target.read_bytes()

    (settings_home / "provider-models.json").unlink()
    apply_report = step.apply()
    assert "provider-models" in apply_report.detail["kept"]

    rollback_report = step.rollback()
    assert rollback_report.ok, rollback_report.summary
    assert target.read_bytes() == target_bytes_before


# ---------------------------------------------------------------------------
# Step 6 — credential reference (never copies credential bytes)
# ---------------------------------------------------------------------------


def test_credential_reference_never_copies_bytes(v1_homes, journal_backups, tmp_path):
    data_home, _settings_home = v1_homes
    journal, backups = journal_backups
    fake_claude_dir = tmp_path / "fake-claude-config"
    fake_claude_dir.mkdir()
    (fake_claude_dir / ".credentials.json").write_text(
        '{"secret": "do-not-copy"}', encoding="utf-8"
    )

    step = CredentialReferenceStep(
        journal=journal,
        backups=backups,
        data_home=data_home,
        refs_override={"claude": fake_claude_dir},
    )
    apply_report = _apply_only(step)
    assert apply_report.detail["written"] == ["claude"]

    layout = storage_layout_v2(data_home)
    record = read_json(layout.providers / "claude" / "provider.json")
    assert record["config_dir"] == str(fake_claude_dir)
    assert record["secret_ref"] == "secret://claude/default"
    assert "do-not-copy" not in json.dumps(record)  # the credential VALUE itself never appears

    # No credential bytes anywhere under the V2 layout.
    for path in layout.root.rglob("*"):
        if path.is_file():
            assert "do-not-copy" not in path.read_text(encoding="utf-8")


def test_credential_reference_records_absence_without_erroring(v1_homes, journal_backups, tmp_path):
    data_home, _settings_home = v1_homes
    journal, backups = journal_backups
    missing = tmp_path / "does-not-exist"
    step = CredentialReferenceStep(
        journal=journal, backups=backups, data_home=data_home, refs_override={"claude": missing}
    )
    apply_report = step.apply()
    assert apply_report.ok
    layout = storage_layout_v2(data_home)
    record = read_json(layout.providers / "claude" / "provider.json")
    assert record["config_dir_exists"] is False


# ---------------------------------------------------------------------------
# Step 7 — runtime triage
# ---------------------------------------------------------------------------


def test_runtime_triage_copies_state_and_leaves_cache_dirs_alone(
    v1_homes, journal_backups, tmp_path
):
    data_home, _settings_home = v1_homes
    journal, backups = journal_backups
    runtime_dir = tmp_path / "runtime"
    (runtime_dir / "tasks" / "demo").mkdir(parents=True)
    (runtime_dir / "tasks" / "demo" / "INDEX.md").write_text("ledger", encoding="utf-8")
    (runtime_dir / "tunnel").mkdir(parents=True)
    (runtime_dir / "tunnel" / "config.yml").write_text("cfg", encoding="utf-8")

    step = RuntimeTriageStep(
        journal=journal, backups=backups, data_home=data_home, runtime_dir=runtime_dir
    )
    inspect_report = step.inspect()
    assert inspect_report.detail["state_dirs"] == ["tasks"]
    assert inspect_report.detail["cache_dirs"] == ["tunnel"]

    _round_trip(step)

    # Cache dir was never copied anywhere, never deleted (read-only classification).
    layout = storage_layout_v2(data_home)
    assert not (layout.root / "tunnel").exists()
    assert (runtime_dir / "tunnel" / "config.yml").exists()


def test_runtime_triage_apply_then_rollback_removes_copied_dir(v1_homes, journal_backups, tmp_path):
    data_home, _settings_home = v1_homes
    journal, backups = journal_backups
    runtime_dir = tmp_path / "runtime"
    (runtime_dir / "role-memory" / "demo").mkdir(parents=True)
    (runtime_dir / "role-memory" / "demo" / "backend.md").write_text("notes", encoding="utf-8")

    step = RuntimeTriageStep(
        journal=journal, backups=backups, data_home=data_home, runtime_dir=runtime_dir
    )
    step.apply()
    layout = storage_layout_v2(data_home)
    target = layout.state_registry / "role-memory"
    assert (target / "demo" / "backend.md").exists()

    step.rollback()
    assert not target.exists()
    # V1 source untouched throughout.
    assert (runtime_dir / "role-memory" / "demo" / "backend.md").exists()


def test_runtime_triage_apply_merges_sessions_without_deleting_state_step_files(
    v1_homes, journal_backups, tmp_path
):
    """#350: `state` (step 5) writes autoresume.json/remote.json directly
    into `state/sessions/`; `runtime-triage` (step 8) copies
    RUNTIME_DIR/sessions into that SAME V2 directory. A wholesale
    rmtree+copytree there silently destroyed step 5's already-applied files
    while both steps still reported ok:true — only an immediate `validate`
    caught the loss. Reproduces the collision directly, without going
    through the CLI."""
    data_home, settings_home = v1_homes
    journal, backups = journal_backups
    (settings_home / "autoresume.json").write_text(json.dumps({"on": True}), encoding="utf-8")
    (settings_home / "takkub-remote-sessions.json").write_text(
        json.dumps({"remote": True}), encoding="utf-8"
    )
    state_step = build_state_step(
        journal, backups, data_home=data_home, settings_home=settings_home
    )
    assert state_step.apply().ok

    runtime_dir = tmp_path / "runtime"
    (runtime_dir / "sessions" / "2026-08-22" / "demo").mkdir(parents=True)
    (runtime_dir / "sessions" / "2026-08-22" / "demo" / "backend-090000.md").write_text(
        "note", encoding="utf-8"
    )
    triage_step = RuntimeTriageStep(
        journal=journal, backups=backups, data_home=data_home, runtime_dir=runtime_dir
    )
    assert triage_step.apply().ok

    layout = storage_layout_v2(data_home)
    assert read_json(layout.state_sessions / "autoresume.json")["data"] == {"on": True}
    assert read_json(layout.state_sessions / "remote.json")["data"] == {"remote": True}
    assert (layout.state_sessions / "2026-08-22" / "demo" / "backend-090000.md").exists()

    # The `state` step's own view of the world is still consistent — a
    # `validate` run immediately after apply must not find any mismatch.
    assert state_step.validate().ok
