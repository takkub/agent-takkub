"""Tests for setup.py's asset-staging build step.

setup.py lives at the repo root (not under src/agent_takkub), so it's loaded
via importlib rather than a normal package import.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest
import setuptools

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SETUP_PY = _REPO_ROOT / "setup.py"

# setup.py calls `setup(cmdclass=...)` unconditionally at module scope (no
# `if __name__ == "__main__":` guard), which would otherwise parse pytest's
# own sys.argv as distutils commands and blow up. Stub out setuptools.setup
# before exec'ing the module so `from setuptools import setup` binds the
# no-op instead — the module body still defines _stage_assets/build_py, we
# just skip the actual distutils entry point.
_real_setuptools_setup = setuptools.setup
setuptools.setup = lambda **kwargs: None
try:
    _spec = importlib.util.spec_from_file_location("_setup_under_test", _SETUP_PY)
    setup_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(setup_mod)  # type: ignore[union-attr]
finally:
    setuptools.setup = _real_setuptools_setup


@pytest.fixture
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch repo root with a valid CLAUDE.md + .claude/agents/*.md,
    pointed at by setup_mod's module-level _ROOT/_ASSETS globals."""
    root = tmp_path / "repo"
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("# playbook", encoding="utf-8")
    (root / ".claude" / "agents" / "backend.md").write_text("# backend role", encoding="utf-8")
    assets = root / "src" / "agent_takkub" / "_assets"
    monkeypatch.setattr(setup_mod, "_ROOT", root)
    monkeypatch.setattr(setup_mod, "_ASSETS", assets)
    return root


class TestStageAssets:
    def test_stages_claude_md_and_agent_files(self, fake_root: Path) -> None:
        setup_mod._stage_assets()
        assets = setup_mod._ASSETS
        assert (assets / "CLAUDE.md").read_text(encoding="utf-8") == "# playbook"
        assert (assets / ".claude" / "agents" / "backend.md").read_text(
            encoding="utf-8"
        ) == "# backend role"

    def test_wipes_stale_assets_before_restaging(self, fake_root: Path) -> None:
        setup_mod._ASSETS.mkdir(parents=True)
        (setup_mod._ASSETS / "stale.txt").write_text("old", encoding="utf-8")
        setup_mod._stage_assets()
        assert not (setup_mod._ASSETS / "stale.txt").exists()

    def test_raises_if_claude_md_missing(self, fake_root: Path) -> None:
        (fake_root / "CLAUDE.md").unlink()
        with pytest.raises(RuntimeError, match=r"CLAUDE\.md"):
            setup_mod._stage_assets()

    def test_raises_if_agents_dir_missing(self, fake_root: Path) -> None:
        shutil.rmtree(fake_root / ".claude" / "agents")
        with pytest.raises(RuntimeError, match="agents"):
            setup_mod._stage_assets()

    def test_raises_if_agents_dir_empty(self, fake_root: Path) -> None:
        (fake_root / ".claude" / "agents" / "backend.md").unlink()
        with pytest.raises(RuntimeError, match="agents"):
            setup_mod._stage_assets()

    def test_ignores_non_md_files_in_agents_dir(self, fake_root: Path) -> None:
        (fake_root / ".claude" / "agents" / "README.txt").write_text("x", encoding="utf-8")
        setup_mod._stage_assets()
        assert not (setup_mod._ASSETS / ".claude" / "agents" / "README.txt").exists()


class TestManifestIncludesRootAssets:
    """Guards the sdist path: CLAUDE.md and .claude/agents/*.md live outside
    src/agent_takkub, so setuptools' sdist command needs an explicit
    MANIFEST.in include or a wheel built from an sdist ships an empty
    _assets/ (see docs/audit/2026-07-05-isolation-plan-crosscheck-codex.md,
    finding 4)."""

    def test_manifest_in_exists_and_covers_root_assets(self) -> None:
        manifest = _REPO_ROOT / "MANIFEST.in"
        assert manifest.is_file()
        text = manifest.read_text(encoding="utf-8")
        assert "CLAUDE.md" in text
        assert ".claude/agents" in text
