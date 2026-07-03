"""Tests for role-scoped plugin injection (`_default_plugin_dirs(role)`).

Plugins are handed to every spawned pane via `--plugin-dir`, and claude
loads each plugin's skill/agent descriptions into the pane's system prompt.
addy-agent-skills alone ships 44 skills + 7 agents (~3-5k tokens) that
duplicate the role `.md` prompts + takkub's own role panes, so it was pure
context bloat on every pane. The role policy trims what each pane receives:

  * lead       → pordee only (orchestrates, doesn't implement)
  * teammates  → superpowers-dev + pordee (TDD/debug/brainstorm for real work)
  * addy-agent-skills → dropped for every role
  * no role    → full _SAFE_PLUGINS set (back-compat for direct callers)
"""

from __future__ import annotations

import pathlib

import pytest

from agent_takkub.lead_context import _default_plugin_dirs


def _make_plugin(cache: pathlib.Path, marketplace: str, version: str = "1.0.0") -> None:
    """Create a fake ~/.claude/plugins/cache/<mp>/<mp>/<ver>/.claude-plugin/plugin.json."""
    d = cache / marketplace / marketplace / version / ".claude-plugin"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text("{}", encoding="utf-8")


@pytest.fixture
def fake_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    home = tmp_path / "home"
    cache = home / ".claude" / "plugins" / "cache"
    cache.mkdir(parents=True)
    for mp in ("superpowers-dev", "addy-agent-skills", "pordee", "ui-ux-pro-max-skill"):
        _make_plugin(cache, mp)
    monkeypatch.setattr(pathlib.Path, "home", lambda: home)
    return cache


class TestRolePluginPolicy:
    def test_lead_gets_only_pordee(self, fake_cache: pathlib.Path) -> None:
        joined = " ".join(_default_plugin_dirs("lead"))
        assert "pordee" in joined
        assert "superpowers-dev" not in joined
        assert "addy-agent-skills" not in joined

    def test_teammate_gets_superpowers_and_pordee_not_addy(self, fake_cache: pathlib.Path) -> None:
        joined = " ".join(_default_plugin_dirs("backend"))
        assert "superpowers-dev" in joined
        assert "pordee" in joined
        assert "addy-agent-skills" not in joined

    def test_addy_dropped_from_every_role(self, fake_cache: pathlib.Path) -> None:
        for role in (
            "lead",
            "frontend",
            "backend",
            "mobile",
            "devops",
            "qa",
            "reviewer",
            "critic",
            "designer",
        ):
            assert "addy-agent-skills" not in " ".join(_default_plugin_dirs(role))

    def test_no_role_is_backcompat_full_set(self, fake_cache: pathlib.Path) -> None:
        """Direct callers with no role still get the full discovered set."""
        joined = " ".join(_default_plugin_dirs())
        assert "superpowers-dev" in joined
        assert "pordee" in joined
        assert "addy-agent-skills" in joined

    def test_unknown_role_falls_back_to_teammate_set(self, fake_cache: pathlib.Path) -> None:
        """A future/unclassified role is treated as a teammate, not lead."""
        joined = " ".join(_default_plugin_dirs("some-new-role"))
        assert "superpowers-dev" in joined
        assert "pordee" in joined
        assert "addy-agent-skills" not in joined

    def test_design_roles_get_ui_ux_pro_max(self, fake_cache: pathlib.Path) -> None:
        """frontend/critic/designer inject the UI/UX Pro Max design skill."""
        for role in ("frontend", "critic", "designer"):
            joined = " ".join(_default_plugin_dirs(role))
            assert "ui-ux-pro-max-skill" in joined, role
            # still get the normal teammate set too
            assert "superpowers-dev" in joined

    def test_non_design_roles_do_not_get_ui_ux(self, fake_cache: pathlib.Path) -> None:
        """backend/qa/devops must NOT pay for the design skill's context."""
        for role in ("backend", "qa", "devops", "reviewer", "lead"):
            joined = " ".join(_default_plugin_dirs(role))
            assert "ui-ux-pro-max-skill" not in joined, role
