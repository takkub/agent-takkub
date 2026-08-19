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
    (settings_home / "role-providers.json").write_text(
        json.dumps({"backend": "codex"}), encoding="utf-8"
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
