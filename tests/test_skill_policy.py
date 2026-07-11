"""Tests for skill_policy: per-role skill injection policy (#103 phase 4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import skill_policy


@pytest.fixture
def policy_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect skill_policy.SKILL_POLICY_FILE to tmp."""
    policy_file = tmp_path / "skill-policy.json"
    monkeypatch.setattr(skill_policy, "SKILL_POLICY_FILE", policy_file)
    return policy_file


class TestLoadPolicy:
    def test_returns_empty_dict_when_file_missing(self, policy_file: Path) -> None:
        assert not policy_file.exists()
        assert skill_policy.load_policy() == {}

    def test_returns_empty_dict_on_corrupt_json(self, policy_file: Path) -> None:
        policy_file.write_text("{invalid json")
        assert skill_policy.load_policy() == {}

    def test_returns_empty_dict_when_root_not_dict(self, policy_file: Path) -> None:
        policy_file.write_text("[]")
        assert skill_policy.load_policy() == {}

    def test_returns_empty_dict_when_roles_missing(self, policy_file: Path) -> None:
        policy_file.write_text('{"version": 1}')
        assert skill_policy.load_policy() == {}

    def test_loads_valid_policy(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {"qa": ["debug-mantra", "verify"], "backend": []},
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        result = skill_policy.load_policy()
        assert result.get("qa") == ["debug-mantra", "verify"]
        assert result.get("backend") == []

    def test_filters_unknown_roles(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {"qa": ["debug-mantra"], "never-registered-ghost": ["x"]},
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        result = skill_policy.load_policy()
        assert "qa" in result
        assert "never-registered-ghost" not in result

    def test_filters_invalid_skill_names(self, policy_file: Path) -> None:
        payload = {"version": 1, "roles": {"qa": ["debug-mantra", "invalid name!"]}}
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        result = skill_policy.load_policy()
        assert result["qa"] == ["debug-mantra"]

    def test_skips_role_with_non_list_value(self, policy_file: Path) -> None:
        payload = {"version": 1, "roles": {"qa": "not-a-list", "backend": ["verify"]}}
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        result = skill_policy.load_policy()
        assert "qa" not in result
        assert "backend" in result


class TestSavePolicy:
    def test_creates_file_on_success(self, policy_file: Path) -> None:
        assert skill_policy.save_policy({"qa": ["debug-mantra"]})
        assert policy_file.exists()

    def test_writes_valid_schema(self, policy_file: Path) -> None:
        skill_policy.save_policy({"qa": ["debug-mantra"]})
        data = json.loads(policy_file.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert data["roles"]["qa"] == ["debug-mantra"]

    def test_rejects_unknown_role(self, policy_file: Path) -> None:
        assert not skill_policy.save_policy({"never-registered-ghost": ["x"]})
        assert not policy_file.exists()

    def test_rejects_invalid_skill_name(self, policy_file: Path) -> None:
        assert not skill_policy.save_policy({"qa": ["invalid name!"]})
        assert not policy_file.exists()

    def test_empty_policy_deletes_file(self, policy_file: Path) -> None:
        skill_policy.save_policy({"qa": ["debug-mantra"]})
        assert policy_file.exists()
        assert skill_policy.save_policy({})
        assert not policy_file.exists()

    def test_atomic_write_leaves_no_tmp(self, policy_file: Path) -> None:
        skill_policy.save_policy({"qa": ["debug-mantra"]})
        assert list(policy_file.parent.glob("*.json.tmp")) == []


class TestEffectiveSkills:
    def test_returns_empty_list_when_role_not_in_policy(self, policy_file: Path) -> None:
        assert skill_policy.effective_skills("backend") == []

    def test_returns_assigned_skills(self, policy_file: Path) -> None:
        skill_policy.save_policy({"qa": ["debug-mantra", "verify"]})
        assert skill_policy.effective_skills("qa") == ["debug-mantra", "verify"]


class TestSetRoleSkills:
    def test_sets_and_reads_back(self, policy_file: Path) -> None:
        assert skill_policy.set_role_skills("qa", ["debug-mantra"])
        assert skill_policy.effective_skills("qa") == ["debug-mantra"]

    def test_rejects_unknown_role(self, policy_file: Path) -> None:
        assert not skill_policy.set_role_skills("never-registered-ghost", ["x"])

    def test_rejects_invalid_name(self, policy_file: Path) -> None:
        assert not skill_policy.set_role_skills("qa", ["bad name!"])

    def test_overwrites_previous_assignment(self, policy_file: Path) -> None:
        skill_policy.set_role_skills("qa", ["debug-mantra"])
        skill_policy.set_role_skills("qa", ["verify"])
        assert skill_policy.effective_skills("qa") == ["verify"]

    def test_empty_list_clears_assignment(self, policy_file: Path) -> None:
        skill_policy.set_role_skills("qa", ["debug-mantra"])
        skill_policy.set_role_skills("qa", [])
        assert skill_policy.effective_skills("qa") == []


class TestSkillMatrixRoles:
    def test_excludes_shell(self) -> None:
        assert "shell" not in skill_policy.skill_matrix_roles()

    def test_includes_codex_and_gemini(self) -> None:
        roles = skill_policy.skill_matrix_roles()
        assert "codex" in roles
        assert "gemini" in roles


class TestRenderSkillAppendix:
    @pytest.fixture
    def project_root(self, tmp_path: Path) -> Path:
        skills = tmp_path / ".claude" / "skills"
        (skills / "debug-mantra").mkdir(parents=True)
        (skills / "debug-mantra" / "SKILL.md").write_text(
            "---\nname: debug-mantra\ndescription: reproduce and trace bugs\n---\nbody",
            encoding="utf-8",
        )
        (skills / "unassigned-skill").mkdir(parents=True)
        (skills / "unassigned-skill" / "SKILL.md").write_text(
            "---\nname: unassigned-skill\ndescription: not assigned to anyone\n---\nbody",
            encoding="utf-8",
        )
        return tmp_path

    def test_empty_when_role_has_no_assignment(self, policy_file: Path, project_root: Path) -> None:
        assert skill_policy.render_skill_appendix("qa", [project_root], "none") == ""

    def test_empty_when_assigned_skill_not_found_in_roots(
        self, policy_file: Path, tmp_path: Path
    ) -> None:
        skill_policy.set_role_skills("qa", ["debug-mantra"])
        empty_root = tmp_path / "empty-project"
        empty_root.mkdir()
        result = skill_policy.render_skill_appendix("qa", [empty_root], "append_system_prompt_file")
        assert result == ""

    def test_claude_style_names_skill_and_description(
        self, policy_file: Path, project_root: Path
    ) -> None:
        skill_policy.set_role_skills("qa", ["debug-mantra"])
        result = skill_policy.render_skill_appendix(
            "qa", [project_root], "append_system_prompt_file"
        )
        assert "debug-mantra" in result
        assert "reproduce and trace bugs" in result
        assert "Skill Matrix" in result
        # claude has a real Skill tool — no "no Skill tool" disclaimer needed
        assert "ไม่มีระบบ Skill tool" not in result

    def test_codex_style_includes_path_and_disclaimer(
        self, policy_file: Path, project_root: Path
    ) -> None:
        skill_policy.set_role_skills("backend", ["debug-mantra"])
        result = skill_policy.render_skill_appendix("backend", [project_root], "agents_md_file")
        assert "debug-mantra" in result
        assert "ไม่มีระบบ Skill tool" in result
        assert str(project_root / ".claude" / "skills" / "debug-mantra" / "SKILL.md") in result

    def test_unknown_context_strategy_returns_empty(
        self, policy_file: Path, project_root: Path
    ) -> None:
        skill_policy.set_role_skills("qa", ["debug-mantra"])
        assert skill_policy.render_skill_appendix("qa", [project_root], "none") == ""

    def test_only_assigned_skills_appear_not_every_skill_in_project(
        self, policy_file: Path, project_root: Path
    ) -> None:
        skill_policy.set_role_skills("qa", ["debug-mantra"])
        result = skill_policy.render_skill_appendix(
            "qa", [project_root], "append_system_prompt_file"
        )
        assert "unassigned-skill" not in result
