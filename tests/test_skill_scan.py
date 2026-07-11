"""Tests for skill_scan.py — real .claude/skills/ discovery for the New Role form."""

from __future__ import annotations

from pathlib import Path

from agent_takkub import config
from agent_takkub.skill_scan import (
    SkillInfo,
    create_skill,
    delete_skill,
    is_writable_skill,
    scan_skills,
    validate_skill_name,
)


def _write_skill(root: Path, name: str, description: str, *, nested: bool = True) -> None:
    if nested:
        d = root / ".claude" / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        target = d / "SKILL.md"
    else:
        d = root / ".claude" / "skills"
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"{name}.md"
    target.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n", encoding="utf-8"
    )


def test_scan_skills_finds_nested_skill_md(tmp_path: Path) -> None:
    _write_skill(tmp_path, "my-skill", "does a thing")
    result = scan_skills(tmp_path)
    assert result == [SkillInfo(name="my-skill", description="does a thing", path=result[0].path)]


def test_scan_skills_finds_flat_md(tmp_path: Path) -> None:
    _write_skill(tmp_path, "flat-skill", "flat layout", nested=False)
    result = scan_skills(tmp_path)
    assert [s.name for s in result] == ["flat-skill"]


def test_scan_skills_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert scan_skills(tmp_path / "does-not-exist") == []


def test_scan_skills_sorted_by_name(tmp_path: Path) -> None:
    _write_skill(tmp_path, "zeta", "z")
    _write_skill(tmp_path, "alpha", "a")
    result = scan_skills(tmp_path)
    assert [s.name for s in result] == ["alpha", "zeta"]


def test_scan_skills_dedupes_by_name_first_root_wins(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write_skill(root_a, "shared", "from a")
    _write_skill(root_b, "shared", "from b")
    result = scan_skills([root_a, root_b])
    assert [s.description for s in result] == ["from a"]


def test_scan_skills_falls_back_to_dirname_when_frontmatter_missing_name(tmp_path: Path) -> None:
    d = tmp_path / ".claude" / "skills" / "fallback-name"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
    result = scan_skills(tmp_path)
    assert [s.name for s in result] == ["fallback-name"]
    assert result[0].description == ""


def test_scan_skills_tolerates_malformed_yaml(tmp_path: Path) -> None:
    d = tmp_path / ".claude" / "skills" / "bad-yaml"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\n: not: valid: yaml:\n---\nbody", encoding="utf-8")
    result = scan_skills(tmp_path)
    assert [s.name for s in result] == ["bad-yaml"]


def test_scan_skills_finds_real_repo_skill() -> None:
    """The task spec's concrete example — cockpit-ui-style must exist for
    real under this checkout's .claude/skills/ and be discoverable."""
    result = scan_skills(config.REPO_ROOT)
    names = {s.name for s in result}
    assert "cockpit-ui-style" in names


class TestValidateSkillName:
    def test_empty_name_rejected(self) -> None:
        ok, err = validate_skill_name("")
        assert ok is False
        assert err

    def test_invalid_charset_rejected(self) -> None:
        ok, err = validate_skill_name("My Skill!")
        assert ok is False
        assert err

    def test_path_traversal_rejected(self) -> None:
        ok, _err = validate_skill_name("../../etc")
        assert ok is False

    def test_valid_kebab_case_accepted(self) -> None:
        ok, err = validate_skill_name("my-new-skill")
        assert ok is True
        assert err == ""

    def test_collides_with_existing_name(self) -> None:
        ok, err = validate_skill_name("git", existing={"git", "debug-mantra"})
        assert ok is False
        assert "git" in err

    def test_collision_check_is_case_insensitive(self) -> None:
        ok, _err = validate_skill_name("Git", existing={"git"})
        assert ok is False


class TestCreateSkill:
    def test_writes_frontmatter_and_body(self, tmp_path: Path) -> None:
        ok, err = create_skill(tmp_path, "my-skill", "does a thing", "# My Skill\n\nbody text")
        assert ok is True
        assert err == ""
        content = (tmp_path / ".claude" / "skills" / "my-skill" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "name: my-skill" in content
        assert "description: does a thing" in content
        assert "body text" in content

    def test_scan_skills_finds_the_created_skill(self, tmp_path: Path) -> None:
        create_skill(tmp_path, "fresh-skill", "fresh", "content")
        names = {s.name for s in scan_skills(tmp_path)}
        assert "fresh-skill" in names

    def test_default_body_when_instructions_blank(self, tmp_path: Path) -> None:
        ok, _err = create_skill(tmp_path, "bare-skill", "bare", "")
        assert ok is True
        content = (tmp_path / ".claude" / "skills" / "bare-skill" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "bare-skill" in content

    def test_invalid_name_rejected_without_writing(self, tmp_path: Path) -> None:
        ok, err = create_skill(tmp_path, "Bad Name!", "x", "y")
        assert ok is False
        assert err
        assert not (tmp_path / ".claude" / "skills").exists()

    def test_duplicate_name_rejected(self, tmp_path: Path) -> None:
        create_skill(tmp_path, "dup", "first", "content")
        ok, err = create_skill(tmp_path, "dup", "second", "content")
        assert ok is False
        assert "dup" in err

    def test_name_colliding_via_existing_arg_rejected(self, tmp_path: Path) -> None:
        ok, err = create_skill(tmp_path, "already-there", "x", "y", existing={"already-there"})
        assert ok is False
        assert err


class TestDeleteSkill:
    def test_deletes_nested_skill_folder(self, tmp_path: Path) -> None:
        create_skill(tmp_path, "to-delete", "desc", "content")
        result = scan_skills(tmp_path)
        skill_md = next(s.path for s in result if s.name == "to-delete")
        assert delete_skill(skill_md) is True
        assert not skill_md.parent.exists()
        assert scan_skills(tmp_path) == []

    def test_deletes_flat_md_file(self, tmp_path: Path) -> None:
        d = tmp_path / ".claude" / "skills"
        d.mkdir(parents=True)
        target = d / "flat-skill.md"
        target.write_text("---\nname: flat-skill\n---\n", encoding="utf-8")
        assert delete_skill(target) is True
        assert not target.exists()

    def test_missing_path_does_not_raise(self, tmp_path: Path) -> None:
        assert delete_skill(tmp_path / "nowhere" / "SKILL.md") is True


class TestIsWritableSkill:
    def test_skill_under_writable_root_is_writable(self, tmp_path: Path) -> None:
        create_skill(tmp_path, "proj-skill", "desc", "content")
        skill_md = tmp_path / ".claude" / "skills" / "proj-skill" / "SKILL.md"
        assert is_writable_skill(skill_md, [tmp_path]) is True

    def test_skill_outside_writable_roots_is_not_writable(self, tmp_path: Path) -> None:
        bundled_root = tmp_path / "bundled"
        create_skill(bundled_root, "shipped-skill", "desc", "content")
        skill_md = bundled_root / ".claude" / "skills" / "shipped-skill" / "SKILL.md"
        project_root = tmp_path / "project"
        assert is_writable_skill(skill_md, [project_root]) is False

    def test_empty_writable_roots_is_not_writable(self, tmp_path: Path) -> None:
        create_skill(tmp_path, "x", "x", "x")
        skill_md = tmp_path / ".claude" / "skills" / "x" / "SKILL.md"
        assert is_writable_skill(skill_md, []) is False
