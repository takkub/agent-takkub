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

    # Isolate pane_tools_policy's role-override file: a real machine may have
    # its own ~/.takkub/pane-tools.json with role overrides, which would leak
    # into effective_plugins() and make these tests flaky depending on whose
    # machine runs them (mirrors isolate_profiles below for user_profile).
    from agent_takkub import pane_tools_policy as ptp

    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    return cache


@pytest.fixture
def isolate_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, fake_cache: pathlib.Path
):
    """Isolate user_profile's on-disk state (FU2: custom-profile plugin dirs).

    `fake_cache`'s `Path.home()` patch is what `default_claude_config_dir()`
    reads for the *no-project* / default-profile path; `_DEFAULT_CONFIG_DIR`
    is pinned to the same `home/.claude` dir so both paths agree on "default".

    Also isolates pane_tools_policy's role-override file — this dev machine
    may have a real ``~/.takkub/pane-tools.json`` with its own role overrides,
    which would leak into `effective_plugins()` and make these tests flaky
    depending on whose machine runs them.
    """
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub import user_profile as up

    settings_home = tmp_path / "settings"
    monkeypatch.setattr(up, "_BASE_DIR", settings_home)
    monkeypatch.setattr(up, "_REGISTRY_PATH", settings_home / "user-profiles.json")
    monkeypatch.setattr(up, "_DEFAULT_CONFIG_DIR", tmp_path / "home" / ".claude")
    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", settings_home / "pane-tools.json")
    return up


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


class TestCustomProfilePluginDirs:
    """FU2 (2026-07-10 cross-platform followup): a project pinned to a custom
    profile (CLAUDE_CONFIG_DIR override) must resolve its plugin cache from
    THAT profile's config dir, not the cockpit's default one."""

    def test_project_with_no_profile_matches_default(self, isolate_profiles) -> None:
        """A project that never called set_profile() is on "default" — same
        result with or without a project arg."""
        with_project = " ".join(_default_plugin_dirs("backend", project="unregistered-proj"))
        without_project = " ".join(_default_plugin_dirs("backend"))
        assert with_project == without_project
        assert "superpowers-dev" in with_project

    def test_custom_profile_project_uses_its_own_cache(
        self, isolate_profiles, tmp_path: pathlib.Path
    ) -> None:
        """A project pinned to a custom profile must see that profile's
        plugins, not the default profile's — and not silently fall back."""
        custom_config_dir = tmp_path / "custom-profile"
        custom_cache = custom_config_dir / "plugins" / "cache"
        custom_cache.mkdir(parents=True)
        _make_plugin(custom_cache, "pordee")
        # deliberately NOT "superpowers-dev" — proves the result came from
        # the custom dir, not a fallback to the default ~/.claude cache.

        isolate_profiles.add_profile("work", str(custom_config_dir))
        isolate_profiles.set_profile("myproj", "work")

        joined = " ".join(_default_plugin_dirs("backend", project="myproj"))
        assert "pordee" in joined
        assert str(custom_config_dir) in joined
        assert "superpowers-dev" not in joined

    def test_different_project_default_profile_unaffected_by_others_custom(
        self, isolate_profiles, tmp_path: pathlib.Path
    ) -> None:
        """Registering a custom profile for one project must not leak into
        another project still on the default profile."""
        custom_config_dir = tmp_path / "custom-profile"
        custom_cache = custom_config_dir / "plugins" / "cache"
        custom_cache.mkdir(parents=True)
        _make_plugin(custom_cache, "pordee")
        isolate_profiles.add_profile("work", str(custom_config_dir))
        isolate_profiles.set_profile("myproj", "work")

        joined = " ".join(_default_plugin_dirs("backend", project="other-proj"))
        assert "superpowers-dev" in joined
        assert str(custom_config_dir) not in joined
