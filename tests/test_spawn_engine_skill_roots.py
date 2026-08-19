"""spawn_engine._skill_roots_for_project — repairs the shipped-skill
surface (Phase 5a, epic #309) before every spawn, best-effort."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import config as config_mod
from agent_takkub import spawn_engine
from agent_takkub.core.capabilities import skill_store


def test_repairs_shipped_skill_surface_before_returning_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config_mod, "ASSETS_ROOT", tmp_path)
    monkeypatch.setattr(spawn_engine, "REPO_ROOT", tmp_path)
    real_skill = tmp_path / "capabilities" / "skills" / "debug-mantra"
    real_skill.mkdir(parents=True)
    (real_skill / "SKILL.md").write_text("# debug-mantra", encoding="utf-8")

    roots = spawn_engine._skill_roots_for_project("")

    assert tmp_path in roots
    surface = tmp_path / ".claude" / "skills" / "debug-mantra" / "SKILL.md"
    assert surface.is_file()


def test_never_raises_when_surface_repair_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(spawn_engine, "REPO_ROOT", tmp_path)

    def _boom() -> list[str]:
        raise OSError("simulated failure")

    monkeypatch.setattr(skill_store, "ensure_shipped_skill_surface", _boom)

    roots = spawn_engine._skill_roots_for_project("")  # must not raise

    assert tmp_path in roots
