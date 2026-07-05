"""Tests for provider-substitution note in _render_lead_context.

Verifies that the "Substituted providers" section:
  1. Appears with "ไม่ได้ติดตั้ง" when a provider binary is missing from PATH.
  2. Is absent when all providers are available (saves tokens on normal spawns).
  3. Distinguishes "toggled off (toggle)" from "not installed (CLI absent)"
     when both conditions exist simultaneously.
"""

from __future__ import annotations

import pathlib

import pytest

from agent_takkub import config as cfg_mod
from agent_takkub import lead_context as lc_mod
from agent_takkub import provider_config, provider_state


@pytest.fixture
def ctx(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Minimal filesystem scaffold for _render_lead_context with all
    provider-related side effects isolated (plan_tier, projects.json, etc.)."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cockpit = tmp_path / "cockpit"
    cockpit.mkdir()
    (cockpit / "CLAUDE.md").write_text("# test cockpit", encoding="utf-8")

    # Redirect filesystem roots so the function writes into tmp instead of the
    # real runtime/ and reads from the tmp cockpit CLAUDE.md.
    monkeypatch.setattr(cfg_mod, "REPO_ROOT", cockpit)
    monkeypatch.setattr(cfg_mod, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(cfg_mod, "PROJECTS_JSON", cockpit / "projects.json")
    monkeypatch.setattr(lc_mod, "REPO_ROOT", cockpit)
    monkeypatch.setattr(lc_mod, "ASSETS_ROOT", cockpit)
    monkeypatch.setattr(lc_mod, "RUNTIME_DIR", runtime)

    # Suppress Pro-plan note — not under test here.
    from agent_takkub import plan_tier

    monkeypatch.setattr(plan_tier, "is_pro", lambda: False)

    return runtime


def _render(project: str | None = None) -> str:
    """Call _render_lead_context and return the written file content."""
    from agent_takkub.lead_context import _render_lead_context

    path = _render_lead_context(project=project)
    assert path is not None, "_render_lead_context returned None (CLAUDE.md missing?)"
    return pathlib.Path(path).read_text(encoding="utf-8")


class TestProviderSubstitutionNote:
    def test_not_installed_shows_note(
        self, ctx: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """codex binary absent from PATH → substitution section appears and names
        the provider under 'ไม่ได้ติดตั้ง', distinguishing it from a toggle."""
        monkeypatch.setattr(provider_state, "is_disabled", lambda p: False)
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: p != "codex")

        content = _render()

        assert "🔄 Substituted providers" in content
        assert "ไม่ได้ติดตั้ง" in content
        assert "codex" in content
        # Toggle label must NOT appear — this is the "not installed" path, not toggle.
        assert "ปิดโดย user (toggle)" not in content

    def test_all_available_no_note(
        self, ctx: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both codex and gemini available → substitution section absent entirely."""
        monkeypatch.setattr(provider_state, "is_disabled", lambda p: False)
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)

        content = _render()

        assert "🔄 Substituted providers" not in content

    def test_toggle_vs_not_installed_distinguished(
        self, ctx: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """codex toggled off, gemini not installed → both bullets appear with
        their respective provider names under the correct labels."""
        monkeypatch.setattr(provider_state, "is_disabled", lambda p: p == "codex")
        # gemini: not toggled but binary missing; codex: toggled (skip available check)
        monkeypatch.setattr(provider_config, "_provider_available", lambda p: p != "gemini")

        content = _render()

        assert "🔄 Substituted providers" in content
        # Toggle label with codex
        assert "ปิดโดย user (toggle)" in content
        assert "codex" in content
        # Not-installed label with gemini
        assert "ไม่ได้ติดตั้ง" in content
        assert "gemini" in content
        # Both in the summary line
        assert "Claude-on-Claude" in content
