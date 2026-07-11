"""Tests for skill_scan.py — real .claude/skills/ discovery for the New Role form."""

from __future__ import annotations

from pathlib import Path

from agent_takkub import config
from agent_takkub.skill_scan import SkillInfo, scan_skills


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
