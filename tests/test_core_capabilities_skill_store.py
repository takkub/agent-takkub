"""core.capabilities.skill_store — shipped-skill surface linking (Phase 5a,
epic #309)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import config as config_mod
from agent_takkub.core.capabilities import skill_store


def test_shipped_skills_root_is_capabilities_skills_under_assets_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config_mod, "ASSETS_ROOT", tmp_path)

    assert skill_store.shipped_skills_root() == tmp_path / "capabilities" / "skills"


def test_ensure_surface_is_noop_when_new_store_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config_mod, "ASSETS_ROOT", tmp_path)

    assert skill_store.ensure_shipped_skill_surface() == []


def test_ensure_surface_links_every_skill(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config_mod, "ASSETS_ROOT", tmp_path)
    real_root = tmp_path / "capabilities" / "skills"
    for name in ("alpha", "beta"):
        d = real_root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"# {name}", encoding="utf-8")

    errors = skill_store.ensure_shipped_skill_surface()

    assert errors == []
    surface = tmp_path / ".claude" / "skills"
    assert (surface / "alpha" / "SKILL.md").is_file()
    assert (surface / "beta" / "SKILL.md").is_file()


def test_ensure_surface_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config_mod, "ASSETS_ROOT", tmp_path)
    real_root = tmp_path / "capabilities" / "skills" / "alpha"
    real_root.mkdir(parents=True)
    (real_root / "SKILL.md").write_text("# alpha", encoding="utf-8")

    first = skill_store.ensure_shipped_skill_surface()
    second = skill_store.ensure_shipped_skill_surface()

    assert first == []
    assert second == []


def test_ensure_surface_never_clobbers_foreign_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real (non-link) directory already at `.claude/skills/<name>` — e.g.
    a user's own committed skill of the same name — is left untouched."""
    monkeypatch.setattr(config_mod, "ASSETS_ROOT", tmp_path)
    real_root = tmp_path / "capabilities" / "skills" / "alpha"
    real_root.mkdir(parents=True)
    (real_root / "SKILL.md").write_text("# new", encoding="utf-8")

    foreign = tmp_path / ".claude" / "skills" / "alpha"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("# foreign, user-owned", encoding="utf-8")

    errors = skill_store.ensure_shipped_skill_surface()

    assert errors == []
    assert (foreign / "SKILL.md").read_text(encoding="utf-8") == "# foreign, user-owned"


def test_ensure_surface_reports_link_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config_mod, "ASSETS_ROOT", tmp_path)
    real_root = tmp_path / "capabilities" / "skills" / "alpha"
    real_root.mkdir(parents=True)
    (real_root / "SKILL.md").write_text("# alpha", encoding="utf-8")
    monkeypatch.setattr(skill_store, "_make_link", lambda src, dst: "simulated link failure")

    errors = skill_store.ensure_shipped_skill_surface()

    assert errors == ["alpha: simulated link failure"]
