"""Tests for role-scoped plugin injection (`_default_plugin_dirs(role)`).

Plugins are handed to every spawned pane via `--plugin-dir`, and claude
loads each plugin's skill/agent descriptions into the pane's system prompt.
addy-agent-skills alone ships 44 skills + 7 agents (~3-5k tokens) that
duplicate the role `.md` prompts + takkub's own role panes, so it was pure
context bloat on every pane. The role policy trims what each pane receives:

  * every role → pordee only (2.2.61: the base is the same on every machine)
  * optional marketplaces → only through a role's own Settings override
  * addy-agent-skills → not grantable at all
  * no role    → the governable set found on disk (direct callers)
"""

from __future__ import annotations

import json
import pathlib

import pytest

from agent_takkub import lead_context
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

    # pane_tools_policy's V2 target (#504 cut half) is isolated automatically
    # by conftest.py's autouse `_isolate_runtime`, so a real machine's role
    # overrides can't leak into effective_plugins() here.
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
    from agent_takkub import user_profile as up

    settings_home = tmp_path / "settings"
    monkeypatch.setattr(up, "_BASE_DIR", settings_home)
    monkeypatch.setattr(up, "_REGISTRY_PATH", settings_home / "user-profiles.json")
    monkeypatch.setattr(up, "_DEFAULT_CONFIG_DIR", tmp_path / "home" / ".claude")
    return up


class TestRolePluginPolicy:
    """2.2.61: one base for every role — pordee. The fake cache still holds
    the retired marketplaces, as a machine that was provisioned earlier does."""

    ALL_ROLES = (
        "lead",
        "frontend",
        "backend",
        "mobile",
        "devops",
        "qa",
        "reviewer",
        "critic",
        "designer",
        "some-new-role",
    )

    def test_every_role_gets_only_the_base(self, fake_cache: pathlib.Path) -> None:
        for role in self.ALL_ROLES:
            joined = " ".join(_default_plugin_dirs(role))
            assert "pordee" in joined, role
            for retired in ("superpowers-dev", "addy-agent-skills", "ui-ux-pro-max-skill"):
                assert retired not in joined, (role, retired)

    def test_a_role_override_can_still_grant_an_optional_marketplace(
        self, fake_cache: pathlib.Path
    ) -> None:
        from agent_takkub import pane_tools_policy

        assert pane_tools_policy.set_role_items(
            "frontend", "plugins", ["pordee", "superpowers-dev"]
        )
        joined = " ".join(_default_plugin_dirs("frontend"))
        assert "superpowers-dev" in joined and "pordee" in joined
        assert "superpowers-dev" not in " ".join(_default_plugin_dirs("backend"))

    def test_addy_is_not_grantable_at_all(self, fake_cache: pathlib.Path) -> None:
        """Dead since long before 2.2.61 — not even in the governable list."""
        assert "addy-agent-skills" not in " ".join(_default_plugin_dirs())

    def test_no_role_is_the_governable_set_on_disk(self, fake_cache: pathlib.Path) -> None:
        """Direct callers with no role (doctor, smoke tests) see what is
        installed and governable."""
        joined = " ".join(_default_plugin_dirs())
        assert "pordee" in joined and "superpowers-dev" in joined


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
        assert "pordee" in with_project

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
        assert "pordee" in joined
        assert str(custom_config_dir) not in joined


class TestHookTimeoutNormalizationOnDiscovery:
    """#135: _default_plugin_dirs must normalize a low hook timeout in every
    plugin dir it discovers, on every call — a plugin injected into a pane
    that still ships a too-low timeout is exactly the bug (5s < node
    cold-start under multi-pane fan-out)."""

    def test_low_timeout_is_raised_when_plugin_is_discovered(
        self, fake_cache: pathlib.Path
    ) -> None:
        manifest = fake_cache / "pordee" / "pordee" / "1.0.0" / ".claude-plugin" / "plugin.json"
        manifest.write_text(
            json.dumps(
                {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "timeout": 5}]}]}}
            ),
            encoding="utf-8",
        )

        _default_plugin_dirs("backend")

        data = json.loads(manifest.read_text("utf-8"))
        assert data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"] >= 30

    def test_logs_when_a_timeout_is_actually_raised(
        self, fake_cache: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = fake_cache / "pordee" / "pordee" / "1.0.0" / ".claude-plugin" / "plugin.json"
        manifest.write_text(
            json.dumps(
                {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "timeout": 5}]}]}}
            ),
            encoding="utf-8",
        )
        logged: list[tuple] = []
        monkeypatch.setattr(
            lead_context, "_log_event", lambda event, **details: logged.append((event, details))
        )

        _default_plugin_dirs("backend")

        raised = [e for e in logged if e[0] == "plugin_hook_timeout_raised"]
        assert len(raised) == 1
        assert raised[0][1]["plugin"] == "pordee"

    def test_no_log_when_plugin_manifest_has_no_hooks(
        self, fake_cache: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # fake_cache's plugin.json is bare "{}" — nothing to raise.
        logged: list[tuple] = []
        monkeypatch.setattr(
            lead_context, "_log_event", lambda event, **details: logged.append((event, details))
        )

        _default_plugin_dirs("backend")

        assert [e for e in logged if e[0] == "plugin_hook_timeout_raised"] == []

    def test_broken_manifest_does_not_break_discovery(self, fake_cache: pathlib.Path) -> None:
        manifest = fake_cache / "pordee" / "pordee" / "1.0.0" / ".claude-plugin" / "plugin.json"
        manifest.write_text("{not valid json", encoding="utf-8")

        # Must not raise, and the plugin dir is still returned (a broken
        # hook manifest is not a reason to drop the plugin from the pane).
        joined = " ".join(_default_plugin_dirs("backend"))
        assert "pordee" in joined
