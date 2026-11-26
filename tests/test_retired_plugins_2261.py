"""2.2.61: one base on every machine — pordee only. The old dev-team plugin
set is not installed, not injected, and uninstalled once (with a backup)."""

from __future__ import annotations

import json

from agent_takkub import config, lead_context, plugin_installer, removed_integrations


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _plugin_dir(claude, marketplace, plugin, version="1.0.0"):
    d = claude / "plugins" / "cache" / marketplace / plugin / version / ".claude-plugin"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text("{}", encoding="utf-8")
    return d.parent


def _machine(tmp_path, extra_installed=()):
    home = tmp_path / "home"
    claude = home / "claude-config"
    ids = [f"{p.key}@{p.marketplace}" for p in plugin_installer.RETIRED]
    ids += ["pordee@pordee", *extra_installed]
    _write(
        claude / "plugins" / "installed_plugins.json",
        {"version": 2, "plugins": {i: [] for i in ids}},
    )
    _write(
        claude / "settings.json",
        {"enabledPlugins": dict.fromkeys(ids, True), "theme": "dark", "pluginUsage": {ids[0]: 3}},
    )
    _write(
        claude / "plugins" / "known_marketplaces.json",
        {m: {"source": "x"} for m in {i.partition("@")[2] for i in ids}},
    )
    _write(
        home / "capabilities" / "mcp" / "permissions.json",
        {
            "version": 1,
            "roles": {
                "frontend": {
                    "mcps": ["playwright"],
                    "plugins": [
                        "claude-plugins-official",
                        "pordee",
                        "superpowers-dev",
                        "ui-ux-pro-max-skill",
                    ],
                }
            },
        },
    )
    for i in ids:
        plugin, _, marketplace = i.partition("@")
        _plugin_dir(claude, marketplace, plugin)
        (claude / "plugins" / "marketplaces" / marketplace).mkdir(parents=True, exist_ok=True)
    return home, claude


def test_base_is_pordee_for_every_role():
    assert config._SAFE_PLUGINS == ("pordee",)
    assert plugin_installer.RECOMMENDED == ()
    assert lead_context._TEAMMATE_PLUGINS == frozenset({"pordee"})
    for role in ("lead", "frontend", "backend", "designer", "critic", "qa", "brand-new-role"):
        got = lead_context._ROLE_PLUGIN_POLICY.get(role, lead_context._TEAMMATE_PLUGINS)
        assert got == frozenset({"pordee"})
    assert plugin_installer.missing_plugins(set()) == []  # nothing left to provision


def test_optional_marketplaces_stay_grantable_but_are_not_default():
    assert set(config._OPTIONAL_PLUGINS).isdisjoint(config._SAFE_PLUGINS)
    assert "addy-agent-skills" not in config.governable_plugins()
    assert "superpowers-dev" in config.governable_plugins()


def test_cleanup_uninstalls_the_retired_set_and_keeps_the_rest(tmp_path):
    home, claude = _machine(tmp_path)
    changed = removed_integrations.cleanup_retired_plugins(home=home, claude_dir=claude)
    assert changed > 0

    installed = json.loads((claude / "plugins" / "installed_plugins.json").read_text("utf-8"))
    assert list(installed["plugins"]) == ["pordee@pordee"] and installed["version"] == 2
    settings = json.loads((claude / "settings.json").read_text("utf-8"))
    assert settings["enabledPlugins"] == {"pordee@pordee": True}
    assert settings["theme"] == "dark" and settings["pluginUsage"] == {}
    perms = json.loads((home / "capabilities/mcp/permissions.json").read_text("utf-8"))
    assert perms["roles"]["frontend"] == {"mcps": ["playwright"], "plugins": ["pordee"]}
    known = json.loads((claude / "plugins" / "known_marketplaces.json").read_text("utf-8"))
    assert list(known) == ["pordee"]

    cache = claude / "plugins" / "cache"
    assert sorted(p.name for p in cache.iterdir()) == ["pordee"]
    assert sorted(p.name for p in (claude / "plugins" / "marketplaces").iterdir()) == ["pordee"]

    # nothing was deleted: the originals sit in the backup
    backups = list((home / "backups").glob("retired-plugins-*"))
    assert len(backups) == 1
    kept = backups[0] / "claude-config" / "plugins" / "cache" / "superpowers-dev" / "superpowers"
    assert (kept / "1.0.0" / ".claude-plugin" / "plugin.json").is_file()
    original = json.loads((backups[0] / "claude-config" / "settings.json").read_text("utf-8"))
    assert "superpowers@superpowers-dev" in original["enabledPlugins"]


def test_cleanup_is_idempotent(tmp_path):
    home, claude = _machine(tmp_path)
    removed_integrations.cleanup_retired_plugins(home=home, claude_dir=claude)
    assert removed_integrations.cleanup_retired_plugins(home=home, claude_dir=claude) == 0


def test_a_plugin_the_user_added_keeps_its_marketplace(tmp_path):
    """Exact ids only: another plugin from Anthropic's marketplace, and a
    plugin whose name merely contains a retired one, are not ours to remove."""
    extra = ("pr-helper@claude-plugins-official", "remember-me@some-marketplace")
    home, claude = _machine(tmp_path, extra_installed=extra)
    removed_integrations.cleanup_retired_plugins(home=home, claude_dir=claude)

    installed = json.loads((claude / "plugins" / "installed_plugins.json").read_text("utf-8"))
    assert set(installed["plugins"]) == {"pordee@pordee", *extra}
    cache = claude / "plugins" / "cache"
    assert (cache / "claude-plugins-official" / "pr-helper").is_dir()
    assert not (cache / "claude-plugins-official" / "code-review").exists()
    assert (claude / "plugins" / "marketplaces" / "claude-plugins-official").is_dir()
    known = json.loads((claude / "plugins" / "known_marketplaces.json").read_text("utf-8"))
    assert "claude-plugins-official" in known and "superpowers-dev" not in known
    perms = json.loads((home / "capabilities/mcp/permissions.json").read_text("utf-8"))
    assert perms["roles"]["frontend"]["plugins"] == ["claude-plugins-official", "pordee"]


def test_clean_machine_is_untouched(tmp_path):
    home = tmp_path / "home"
    (home / "claude-config").mkdir(parents=True)
    assert (
        removed_integrations.cleanup_retired_plugins(home=home, claude_dir=home / "claude-config")
        == 0
    )
    assert not (home / "backups").exists()


def test_boot_hook_never_touches_a_dev_checkout(monkeypatch):
    monkeypatch.setattr(config, "DATA_HOME", config.REPO_ROOT)
    monkeypatch.setattr(
        removed_integrations,
        "cleanup_retired_plugins",
        lambda **k: (_ for _ in ()).throw(AssertionError("must not run on a dev checkout")),
    )
    assert removed_integrations.run_boot_cleanups() == {}
