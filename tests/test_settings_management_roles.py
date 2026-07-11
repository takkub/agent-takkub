"""Characterization + CRUD tests for settings_management's Roles slice.

The characterization tests (TestCharacterization*) pin down the JSON
semantics the new repository/service layer MUST preserve exactly, per the
task spec: default vs explicit-empty (MCP/plugins), forced providers
(lead/codex/gemini), custom role live registration, skill policy two-state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import (
    custom_roles,
    pane_tools_policy,
    provider_config,
    roles,
    shared_dev_tools,
    skill_policy,
)
from agent_takkub.settings_management.commands import (
    CreateRoleCommand,
    RoleAccessDraft,
    RoleGeneralDraft,
    UpdateRoleCommand,
)
from agent_takkub.settings_management.models import Ownership
from agent_takkub.settings_management.repositories import roles as roles_repo
from agent_takkub.settings_management.services import cleanup, relationships


@pytest.fixture(autouse=True)
def redirect_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Redirect every store the Roles slice touches into tmp, and clear the
    runtime custom-role registry so tests never leak into each other."""
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(pane_tools_policy, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(skill_policy, "SKILL_POLICY_FILE", tmp_path / "skill-policy.json")
    monkeypatch.setattr(provider_config, "_CONFIG_PATH", tmp_path / "role-providers.json")
    monkeypatch.setattr(provider_config, "_BASE_DIR", tmp_path)
    # Access-tab MCP writes now regen role variants (HIGH-4) — redirect the
    # master file so that never touches the real ~/.takkub runtime dir.
    monkeypatch.setattr(shared_dev_tools, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")

    saved = dict(roles._CUSTOM)
    roles._CUSTOM.clear()
    yield tmp_path
    roles._CUSTOM.clear()
    roles._CUSTOM.update(saved)


def _access_draft(**overrides) -> RoleAccessDraft:
    base = dict(provider="claude", skills=[], mcps=None, plugins=None)
    base.update(overrides)
    return RoleAccessDraft(**base)


def _general_draft(**overrides) -> RoleGeneralDraft:
    base = dict(label="Data Eng", color="#94a3b8", column=2, row=50, instructions="# Data Eng\n")
    base.update(overrides)
    return RoleGeneralDraft(**base)


class TestCharacterizationDefaultVsExplicitEmpty:
    """MCP/plugin tri-state: None (no policy entry) MUST stay distinguishable
    from an explicit empty list — collapsing them was a real regression
    class in pane_tools_policy history."""

    def test_no_entry_reads_as_use_defaults(self) -> None:
        access = relationships.get_role_access("backend")
        assert access.mcps is None
        assert access.plugins is None

    def test_explicit_empty_list_stays_distinguishable_from_defaults(self) -> None:
        pane_tools_policy.set_role_items("backend", "mcps", [])
        access = relationships.get_role_access("backend")
        assert access.mcps == ()
        assert access.mcps is not None

    def test_write_access_both_none_resets_role_to_defaults(self) -> None:
        pane_tools_policy.set_role_items("backend", "mcps", ["playwright"])
        result = relationships.write_access("backend", _access_draft(mcps=None, plugins=None))
        assert result.ok
        assert "backend" not in pane_tools_policy.load_policy()
        access = relationships.get_role_access("backend")
        assert access.mcps is None

    def test_write_access_explicit_empty_list_persists_as_empty_not_default(self) -> None:
        result = relationships.write_access("backend", _access_draft(mcps=[], plugins=["github"]))
        assert result.ok
        access = relationships.get_role_access("backend")
        assert access.mcps == ()
        assert access.plugins == ("github",)


class TestCharacterizationForcedProviders:
    def test_lead_provider_unlocked_but_defaults_to_claude(self) -> None:
        # #101 (2026-07-11) removed lead from _FORCED_PROVIDER — degraded-mode
        # unlock. Lead still DEFAULTS to claude, but is no longer forced, so
        # the Access tab may offer an override (with capability-loss notice).
        detail = roles_repo.get("lead")
        assert detail.access.provider == "claude"
        assert detail.access.provider_forced is False

    def test_codex_forced_provider_cannot_be_overridden_via_write_access(self) -> None:
        result = relationships.write_access("codex", _access_draft(provider="claude"))
        assert result.ok
        # save_role_overrides silently drops forced-role overrides — the
        # provider stays codex regardless of what the draft asked for.
        assert provider_config.provider_for("codex") == "codex"

    def test_gemini_forced_provider_cannot_be_overridden(self) -> None:
        relationships.write_access("gemini", _access_draft(provider="codex"))
        assert provider_config.provider_for("gemini") == "gemini"

    def test_unforced_role_override_is_respected(self) -> None:
        relationships.write_access("backend", _access_draft(provider="codex"))
        assert provider_config.provider_for("backend") == "codex"


class TestCharacterizationCustomRoleLiveRegistration:
    def test_create_registers_role_immediately_no_restart_needed(self) -> None:
        result = roles_repo.create(
            CreateRoleCommand(name="data-eng", general=_general_draft(), access=_access_draft())
        )
        assert result.ok
        # Immediately resolvable in-process, mirroring custom_roles' own
        # "spawnable without a cockpit restart" contract.
        assert roles.by_name("data-eng") is not None
        assert "data-eng" in roles.all_role_names()

    def test_delete_unregisters_role_immediately(self) -> None:
        roles_repo.create(
            CreateRoleCommand(name="data-eng", general=_general_draft(), access=_access_draft())
        )
        plan = roles_repo.delete_plan("data-eng")
        assert plan.deletable
        result = roles_repo.delete("data-eng", plan.version)
        assert result.ok
        assert roles.by_name("data-eng") is None


class TestCharacterizationSkillPolicyTwoState:
    """Skill policy has no fallback-default table (unlike MCP/plugins):
    missing and explicit-empty both mean "inject nothing"."""

    def test_missing_and_explicit_empty_both_read_as_no_skills(self) -> None:
        assert relationships.get_role_access("backend").skills == ()
        skill_policy.set_role_skills("backend", [])
        assert relationships.get_role_access("backend").skills == ()

    def test_explicit_selection_round_trips(self) -> None:
        relationships.write_access("backend", _access_draft(skills=["cockpit-ui-style"]))
        assert relationships.get_role_access("backend").skills == ("cockpit-ui-style",)


class TestRoleRepositoryList:
    def test_lists_builtin_and_custom(self) -> None:
        roles_repo.create(
            CreateRoleCommand(name="data-eng", general=_general_draft(), access=_access_draft())
        )
        summaries = roles_repo.list()
        names = {s.name for s in summaries}
        assert "backend" in names
        assert "data-eng" in names
        by_name = {s.name: s for s in summaries}
        assert by_name["backend"].ownership is Ownership.BUILT_IN
        assert by_name["data-eng"].ownership is Ownership.CUSTOM

    def test_query_filters_by_name_or_label(self) -> None:
        names = {s.name for s in roles_repo.list(query="back")}
        assert names == {"backend"}


class TestRoleRepositoryCapabilities:
    def test_builtin_role_cannot_be_deleted(self) -> None:
        caps = roles_repo.capabilities("backend")
        assert caps.can_delete is False
        assert caps.can_update is True
        assert caps.reason

    def test_custom_role_fully_editable(self) -> None:
        roles_repo.create(
            CreateRoleCommand(name="data-eng", general=_general_draft(), access=_access_draft())
        )
        caps = roles_repo.capabilities("data-eng")
        assert caps.can_delete is True
        assert caps.can_update is True


class TestRoleRepositoryCreateUpdateDelete:
    def test_create_rejects_invalid_name(self) -> None:
        result = roles_repo.create(
            CreateRoleCommand(name="Bad Name!", general=_general_draft(), access=_access_draft())
        )
        assert not result.ok

    def test_create_rejects_reserved_name(self) -> None:
        result = roles_repo.create(
            CreateRoleCommand(name="backend", general=_general_draft(), access=_access_draft())
        )
        assert not result.ok

    def test_create_writes_instructions_file(self) -> None:
        roles_repo.create(
            CreateRoleCommand(
                name="data-eng",
                general=_general_draft(instructions="# hello\n"),
                access=_access_draft(),
            )
        )
        detail = roles_repo.get("data-eng")
        assert detail.instructions == "# hello\n"

    def test_update_persists_general_and_access(self) -> None:
        roles_repo.create(
            CreateRoleCommand(name="data-eng", general=_general_draft(), access=_access_draft())
        )
        result = roles_repo.update(
            "data-eng",
            UpdateRoleCommand(
                general=_general_draft(label="Data Engineer 2", instructions="# v2\n"),
                access=_access_draft(skills=["cockpit-ui-style"]),
            ),
        )
        assert result.ok
        detail = roles_repo.get("data-eng")
        assert detail.label == "Data Engineer 2"
        assert detail.instructions == "# v2\n"
        assert detail.access.skills == ("cockpit-ui-style",)

    def test_update_builtin_role_cannot_change_general_but_can_change_access(self) -> None:
        result = roles_repo.update(
            "backend",
            UpdateRoleCommand(
                general=_general_draft(label="Hacked"),
                access=_access_draft(skills=["cockpit-ui-style"]),
            ),
        )
        assert result.ok
        detail = roles_repo.get("backend")
        assert detail.label == "Backend"  # unchanged — built-in general is read-only
        assert detail.access.skills == ("cockpit-ui-style",)

    def test_delete_builtin_role_is_rejected(self) -> None:
        plan = roles_repo.delete_plan("backend")
        assert not plan.deletable
        result = roles_repo.delete("backend", plan.version)
        assert not result.ok

    def test_delete_stale_plan_version_is_rejected(self) -> None:
        roles_repo.create(
            CreateRoleCommand(name="data-eng", general=_general_draft(), access=_access_draft())
        )
        plan = roles_repo.delete_plan("data-eng")
        # Mutate state after the plan was computed — the signature changes.
        skill_policy.set_role_skills("data-eng", ["cockpit-ui-style"])
        result = roles_repo.delete("data-eng", plan.version)
        assert not result.ok

    def test_delete_removes_registry_and_policy_entries(self) -> None:
        roles_repo.create(
            CreateRoleCommand(
                name="data-eng",
                general=_general_draft(),
                access=_access_draft(skills=["cockpit-ui-style"], mcps=["playwright"]),
            )
        )
        plan = roles_repo.delete_plan("data-eng")
        result = roles_repo.delete("data-eng", plan.version)
        assert result.ok
        assert "data-eng" not in custom_roles.load_custom_roles()
        assert "data-eng" not in pane_tools_policy.load_policy()
        assert "data-eng" not in skill_policy.load_policy()


class TestRelationshipWriteExceptionBoundary:
    """MED-5: a filesystem error from any of the four Access-tab stores
    must come back as a failed OperationResult, not escape write_access
    uncaught (provider_config.save_role_overrides can raise OSError from
    mkdir/write/replace — only RuntimeError was caught before)."""

    def test_provider_store_oserror_is_caught_not_propagated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a: object, **k: object) -> None:
            raise OSError("simulated disk failure")

        monkeypatch.setattr(provider_config, "save_role_overrides", boom)
        result = relationships.write_access("backend", _access_draft(provider="codex"))
        assert not result.ok
        assert "simulated disk failure" in result.message

    def test_late_store_failure_rolls_back_earlier_stores_in_same_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Seed pre-call state, then attempt a write that changes BOTH the
        # provider and skills before the (last) mcps write fails.
        relationships.write_access("backend", _access_draft(provider="codex"))

        def boom(role: str, kind: str, names: list[str]) -> bool:
            raise OSError("simulated disk failure")

        monkeypatch.setattr(pane_tools_policy, "set_role_items", boom)
        result = relationships.write_access(
            "backend",
            _access_draft(provider="claude", skills=["cockpit-ui-style"], mcps=["playwright"]),
        )
        assert not result.ok
        # Both the provider write and the skills write already landed on
        # disk earlier in this same call — both must be rolled back.
        assert provider_config.provider_for("backend") == "codex"
        assert relationships.get_role_access("backend").skills == ()


class TestAggregateTransactionRollback:
    """HIGH-2: role create/update/delete is one aggregate transaction —
    a failure partway through must roll back every store already written
    AND never stage the live in-memory registry mutation."""

    def test_create_rolls_back_registry_and_file_when_access_write_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(skill_policy, "set_role_skills", lambda role, names: False)
        result = roles_repo.create(
            CreateRoleCommand(
                name="data-eng",
                general=_general_draft(),
                access=_access_draft(skills=["cockpit-ui-style"]),
            )
        )
        assert not result.ok
        assert "data-eng" not in custom_roles.load_custom_roles()
        assert not custom_roles.role_file_path("data-eng").is_file()
        # Live registry never staged — the role must not be spawnable.
        assert roles.by_name("data-eng") is None

    def test_update_rolls_back_registry_when_md_write_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        roles_repo.create(
            CreateRoleCommand(name="data-eng", general=_general_draft(), access=_access_draft())
        )

        from pathlib import Path as _Path

        real_write_text = _Path.write_text

        def flaky_write_text(self: _Path, *a: object, **k: object) -> int:
            if self.name == "data-eng.md":
                raise OSError("simulated disk failure")
            return real_write_text(self, *a, **k)

        monkeypatch.setattr(_Path, "write_text", flaky_write_text)

        result = roles_repo.update(
            "data-eng",
            UpdateRoleCommand(general=_general_draft(label="Hacked Label"), access=_access_draft()),
        )
        assert not result.ok
        # Registry entry rolled back to the pre-update label.
        assert custom_roles.load_custom_roles()["data-eng"].label == "Data Eng"
        # Live registry never re-staged with the failed update's label.
        assert roles.by_name("data-eng").label == "Data Eng"

    def test_delete_rolls_back_registry_when_skill_policy_cleanup_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        roles_repo.create(
            CreateRoleCommand(
                name="data-eng",
                general=_general_draft(),
                access=_access_draft(skills=["cockpit-ui-style"]),
            )
        )
        plan = roles_repo.delete_plan("data-eng")

        monkeypatch.setattr(skill_policy, "save_policy", lambda policy: False)
        result = roles_repo.delete("data-eng", plan.version)

        assert not result.ok
        assert "data-eng" in custom_roles.load_custom_roles()
        assert custom_roles.role_file_path("data-eng").is_file()
        # Live registry never unregistered — the role stays spawnable.
        assert roles.by_name("data-eng") is not None


class TestReferenceAwareDelete:
    def test_role_referenced_by_pipeline_template_blocks_delete(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agent_takkub import pipeline_config

        monkeypatch.setattr(pipeline_config, "_PATH", tmp_path / "pipelines.json")

        roles_repo.create(
            CreateRoleCommand(name="data-eng", general=_general_draft(), access=_access_draft())
        )
        pipeline_config.path(None).write_text(
            '{"templates": [{"id": "t1", "name": "Custom Pipeline",'
            ' "hops": [[{"role": "data-eng"}]]}]}',
            encoding="utf-8",
        )
        plan = cleanup.role_delete_plan("data-eng")
        assert not plan.deletable
        assert any("Custom Pipeline" in b for b in plan.blockers)
