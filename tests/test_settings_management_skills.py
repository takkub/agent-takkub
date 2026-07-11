"""Characterization + CRUD tests for settings_management's Skills slice.

Ownership is derived from *where* a SKILL.md lives: `_project_roots()`
(`Path.cwd()`) -> PROJECT (writable, full CRUD); `_shipped_roots()`
(`config.REPO_ROOT`/`ASSETS_ROOT`) -> SHIPPED (bundled with cockpit,
read-only); anything else discovered -> EXTERNAL.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import config, skill_policy
from agent_takkub.settings_management.commands import CreateSkillCommand, UpdateSkillCommand
from agent_takkub.settings_management.models import Ownership
from agent_takkub.settings_management.repositories import skills as skills_repo


@pytest.fixture(autouse=True)
def redirect_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    shipped_dir = tmp_path / "shipped"
    shipped_dir.mkdir()

    monkeypatch.chdir(project_dir)
    monkeypatch.setattr(config, "REPO_ROOT", shipped_dir)
    monkeypatch.setattr(config, "ASSETS_ROOT", shipped_dir)
    # Real repo's own role docs would otherwise leak into `_assigned_roles`'s
    # word-boundary regex scan (skill_audit falls back to `config.AGENTS_DIR`
    # when cwd has no `.claude/agents`) — point it at an empty dir instead.
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "no-agents-here")
    monkeypatch.setattr(config, "CUSTOM_AGENTS_DIR", tmp_path / "no-custom-agents-here")
    monkeypatch.setattr(skill_policy, "SKILL_POLICY_FILE", tmp_path / "skill-policy.json")
    yield tmp_path


def _write_shipped_skill(
    tmp_path: Path, name: str = "shipped-skill", description: str = "ships with cockpit"
) -> Path:
    shipped_dir = tmp_path / "shipped" / ".claude" / "skills" / name
    shipped_dir.mkdir(parents=True)
    path = shipped_dir / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n", encoding="utf-8"
    )
    return path


class TestOwnership:
    def test_project_skill_is_project_owned_and_fully_editable(self) -> None:
        result = skills_repo.create(
            CreateSkillCommand(name="my-skill", description="d", instructions="body")
        )
        assert result.ok
        detail = skills_repo.get("my-skill")
        assert detail.ownership is Ownership.PROJECT
        assert detail.capabilities.can_update is True
        assert detail.capabilities.can_delete is True

    def test_shipped_skill_is_read_only_and_offers_duplicate(self, tmp_path: Path) -> None:
        _write_shipped_skill(tmp_path)
        detail = skills_repo.get("shipped-skill")
        assert detail.ownership is Ownership.SHIPPED
        assert detail.capabilities.can_update is False
        assert detail.capabilities.can_delete is False
        assert detail.capabilities.reason

    def test_project_skill_shadows_shipped_skill_of_same_name(self, tmp_path: Path) -> None:
        _write_shipped_skill(tmp_path, name="dup-name")
        result = skills_repo.duplicate_to_project("dup-name")
        assert result.ok
        detail = skills_repo.get("dup-name")
        assert detail.ownership is Ownership.PROJECT


class TestCreateUpdateDelete:
    def test_create_rejects_name_collision(self) -> None:
        skills_repo.create(CreateSkillCommand(name="skill-a", description="", instructions=""))
        result = skills_repo.create(
            CreateSkillCommand(name="skill-a", description="", instructions="")
        )
        assert not result.ok

    def test_update_preserves_unknown_frontmatter_keys(self) -> None:
        skills_repo.create(
            CreateSkillCommand(name="skill-a", description="d1", instructions="body")
        )
        detail = skills_repo.get("skill-a")
        path = Path(detail.path)
        # Hand-add an extra frontmatter key the form never exposes.
        text = path.read_text(encoding="utf-8")
        text = text.replace("description: d1", "description: d1\nlicense: MIT")
        path.write_text(text, encoding="utf-8")

        result = skills_repo.update(
            "skill-a", UpdateSkillCommand(description="d2", instructions="body v2")
        )
        assert result.ok
        new_text = path.read_text(encoding="utf-8")
        assert "license: MIT" in new_text
        assert "description: d2" in new_text
        detail = skills_repo.get("skill-a")
        assert detail.instructions.strip() == "body v2"

    def test_update_rejects_read_only_skill(self, tmp_path: Path) -> None:
        _write_shipped_skill(tmp_path)
        result = skills_repo.update(
            "shipped-skill", UpdateSkillCommand(description="hacked", instructions="x")
        )
        assert not result.ok

    def test_delete_removes_file_and_skill_policy_reference(self) -> None:
        skills_repo.create(CreateSkillCommand(name="skill-a", description="", instructions=""))
        skill_policy.set_role_skills("backend", ["skill-a"])
        plan = skills_repo.delete_plan("skill-a")
        assert plan.deletable
        assert "backend" in plan.effects[-1]
        result = skills_repo.delete("skill-a", plan.version)
        assert result.ok
        assert "skill-a" not in {s.name for s in skills_repo.list()}
        assert "skill-a" not in skill_policy.effective_skills("backend")

    def test_delete_stale_plan_version_is_rejected(self) -> None:
        skills_repo.create(CreateSkillCommand(name="skill-a", description="", instructions=""))
        plan = skills_repo.delete_plan("skill-a")
        skill_policy.set_role_skills("backend", ["skill-a"])
        result = skills_repo.delete("skill-a", plan.version)
        assert not result.ok

    def test_delete_read_only_skill_is_rejected(self, tmp_path: Path) -> None:
        _write_shipped_skill(tmp_path)
        plan = skills_repo.delete_plan("shipped-skill")
        assert not plan.deletable
        result = skills_repo.delete("shipped-skill", plan.version)
        assert not result.ok


class TestAssignedRoles:
    def test_assigned_roles_from_skill_policy(self) -> None:
        skills_repo.create(CreateSkillCommand(name="skill-a", description="", instructions=""))
        skill_policy.set_role_skills("backend", ["skill-a"])
        detail = skills_repo.get("skill-a")
        assert "backend" in detail.assigned_roles

    def test_no_assignment_reads_as_empty(self) -> None:
        skills_repo.create(CreateSkillCommand(name="skill-a", description="", instructions=""))
        detail = skills_repo.get("skill-a")
        assert detail.assigned_roles == ()


class TestList:
    def test_list_query_filters_by_name_and_description(self) -> None:
        skills_repo.create(CreateSkillCommand(name="alpha", description="", instructions=""))
        skills_repo.create(
            CreateSkillCommand(name="beta", description="matches query", instructions="")
        )
        names = {s.name for s in skills_repo.list(query="alpha")}
        assert names == {"alpha"}
        names = {s.name for s in skills_repo.list(query="matches")}
        assert names == {"beta"}
