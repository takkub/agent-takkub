"""Tests for skill_scan.py — real .claude/skills/ discovery for the New Role form."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import config, skill_scan
from agent_takkub.skill_scan import (
    SkillInfo,
    create_skill,
    delete_skill,
    ensure_project_skill_links,
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


# ── Central-home skills: real file lives in project_skills_dir, project path
# holds a junction (Windows) / symlink (POSIX). These run natively on both
# OSes — the CI win+mac matrix is what exercises both link kinds — so the
# create→link→scan→delete round-trip is real, not mocked.
@pytest.fixture
def central_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point PROJECT_SKILLS_HOME at a throwaway dir so central skills never
    touch the developer's real ~/.agent-takkub."""
    home = tmp_path / "central" / "project-skills"
    monkeypatch.setattr(config, "PROJECT_SKILLS_HOME", home)
    return home


class TestCentralSkills:
    def test_create_writes_central_and_links_into_project(
        self, tmp_path: Path, central_home: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        ok, err = create_skill(
            project, "central-skill", "does a thing", "body", project_ns="myproj"
        )
        assert ok, err
        # Real file lives centrally, keyed by project_ns…
        central_md = central_home / "myproj" / "central-skill" / "SKILL.md"
        assert central_md.is_file()
        assert "does a thing" in central_md.read_text(encoding="utf-8")
        # …and the project path resolves (through the link) to that same file.
        link = project / ".claude" / "skills" / "central-skill"
        assert link.exists()
        assert (link / "SKILL.md").read_text(encoding="utf-8") == central_md.read_text(
            encoding="utf-8"
        )

    def test_scan_discovers_central_skill_via_project_path(
        self, tmp_path: Path, central_home: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        create_skill(project, "linked-skill", "d", "b", project_ns="myproj")
        # scan_skills only looks at <project>/.claude/skills — the link makes
        # the central skill show up there transparently (this is why no
        # spawn-time root change is needed).
        names = {s.name for s in scan_skills(project)}
        assert "linked-skill" in names

    def test_is_writable_skill_via_central_extra_dir(
        self, tmp_path: Path, central_home: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        create_skill(project, "w-skill", "d", "b", project_ns="myproj")
        skill_md = next(s.path for s in scan_skills(project) if s.name == "w-skill")
        # The junctioned path resolves into the central store, so the project
        # root alone doesn't mark it writable — the central extra_dir does.
        assert is_writable_skill(skill_md, [project]) is False
        assert (
            is_writable_skill(skill_md, [project], extra_dirs=[config.project_skills_dir("myproj")])
            is True
        )

    def test_delete_removes_link_and_central_real_dir(
        self, tmp_path: Path, central_home: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        create_skill(project, "gone", "d", "b", project_ns="myproj")
        skill_md = next(s.path for s in scan_skills(project) if s.name == "gone")

        assert delete_skill(skill_md) is True

        # Both the project-side link AND the central real dir are gone — the
        # junction-safe delete removed the reparse point first, so the central
        # store wasn't deleted THROUGH a still-live link (and isn't orphaned).
        assert not (project / ".claude" / "skills" / "gone").exists()
        assert not (central_home / "myproj" / "gone").exists()
        assert scan_skills(project) == []

    def test_ensure_links_recreates_missing_link(self, tmp_path: Path, central_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        create_skill(project, "relink", "d", "b", project_ns="myproj")
        link = project / ".claude" / "skills" / "relink"
        # Simulate a lost link between sessions (remove just the reparse point).
        skill_scan._remove_link(link)
        assert not link.exists()

        errors = ensure_project_skill_links(project, "myproj")
        assert errors == []
        assert (link / "SKILL.md").is_file()

    def test_ensure_links_does_not_clobber_real_user_skill(
        self, tmp_path: Path, central_home: Path
    ) -> None:
        project = tmp_path / "proj"
        # A real (committed) user skill sharing the name must be left intact.
        real = project / ".claude" / "skills" / "shared"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text(
            "---\nname: shared\ndescription: user-owned\n---\nuser body", encoding="utf-8"
        )
        # A central skill of the same name also exists.
        (central_home / "myproj" / "shared").mkdir(parents=True)
        (central_home / "myproj" / "shared" / "SKILL.md").write_text(
            "---\nname: shared\ndescription: central\n---\ncentral body", encoding="utf-8"
        )
        ensure_project_skill_links(project, "myproj")
        # The user's file is untouched (not replaced by a link to central).
        assert "user-owned" in (real / "SKILL.md").read_text(encoding="utf-8")

    def test_create_rolls_back_central_on_link_failure(
        self, tmp_path: Path, central_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.setattr(skill_scan, "_make_link", lambda src, dst: "simulated link failure")
        ok, err = create_skill(project, "doomed", "d", "b", project_ns="myproj")
        assert ok is False
        assert "junction" in err
        # No orphaned central skill left behind (would never show in the project).
        assert not (central_home / "myproj" / "doomed").exists()

    def test_legacy_no_project_ns_writes_into_project(self, tmp_path: Path) -> None:
        # Bare/no-active-project path (project_ns=None) keeps writing directly
        # under the project root — no central store, no link.
        ok, err = create_skill(tmp_path, "legacy", "d", "b")
        assert ok, err
        assert (tmp_path / ".claude" / "skills" / "legacy" / "SKILL.md").is_file()


class TestConfigCentralPaths:
    def test_project_skills_dir_under_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        d = config.project_skills_dir("myproj")
        assert d.name == "myproj"
        assert d.parent == config.PROJECT_SKILLS_HOME.resolve()

    def test_project_skills_dir_rejects_traversal(self) -> None:
        with pytest.raises(ValueError):
            config.project_skills_dir("../escape")

    def test_project_docs_dir_under_docs(self) -> None:
        d = config.project_docs_dir("myproj")
        assert d.name == "myproj"
        assert d.parent == config.DOCS_DIR.resolve()

    def test_project_docs_dir_rejects_traversal(self) -> None:
        with pytest.raises(ValueError):
            config.project_docs_dir("../../etc")
