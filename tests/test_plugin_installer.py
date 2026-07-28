"""Unit tests for the recommended dev-team plugin set + installer logic.

Covers the pure pieces (output parsing, on-disk status, missing computation) so
the 🧩 Plugins button's behaviour is verified without shelling out to claude or
launching the GUI. The subprocess install path is exercised indirectly via the
parser; the network call itself is not unit-tested.
"""

from __future__ import annotations

import json
import stat
from unittest.mock import patch

from agent_takkub import plugin_installer as pi


def test_recommended_set_shape():
    keys = [p.key for p in pi.RECOMMENDED]
    # The dev-team plugins, in display order.
    assert keys == [
        "superpowers",
        "frontend-design",
        "code-review",
        "security-guidance",
        "remember",
        "ui-ux-pro-max",
    ]
    # Hook-heavy ones are user-only (not pushed into panes).
    by_key = {p.key: p for p in pi.RECOMMENDED}
    assert by_key["frontend-design"].pane_loaded is True
    assert by_key["code-review"].pane_loaded is True
    assert by_key["security-guidance"].pane_loaded is False
    assert by_key["remember"].pane_loaded is False
    # UI/UX Pro Max is a skill (pane-loadable); role-scoping to design panes
    # lives in lead_context._ROLE_PLUGIN_POLICY, not here.
    assert by_key["ui-ux-pro-max"].pane_loaded is True
    assert by_key["ui-ux-pro-max"].marketplace == "ui-ux-pro-max-skill"


def _make_installed(cache, marketplace, plugin, version="1.0.0"):
    """Create a loadable cache layout: <mp>/<plugin>/<ver>/.claude-plugin/plugin.json."""
    d = cache / marketplace / plugin / version / ".claude-plugin"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text("{}", encoding="utf-8")


def test_installed_on_disk(tmp_path):
    cache = tmp_path / ".claude" / "plugins" / "cache"
    # frontend-design + code-review + superpowers fully installed.
    _make_installed(cache, "claude-plugins-official", "frontend-design")
    _make_installed(cache, "claude-plugins-official", "code-review")
    _make_installed(cache, "superpowers-dev", "superpowers", "5.1.0")

    have = pi.installed_on_disk(home=tmp_path)
    assert have == {"frontend-design", "code-review", "superpowers"}

    missing = {p.key for p in pi.missing_plugins(have)}
    assert missing == {"security-guidance", "remember", "ui-ux-pro-max"}


def test_installed_on_disk_matches_install_target_when_data_home_differs(monkeypatch, tmp_path):
    # F2 regression: an installed build's DATA_HOME != REPO_ROOT, so
    # default_claude_config_dir() != ~/.claude. installed_on_disk() must read
    # plugins from the SAME dir _claude_env() installs them into — otherwise
    # install_plugin() reports "not found on disk" right after a real install.
    isolated_config_dir = tmp_path / "agent-takkub-data" / "claude-config"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    with patch(
        "agent_takkub.config.default_claude_config_dir",
        return_value=isolated_config_dir,
    ):
        install_target = pi._claude_env()["CLAUDE_CONFIG_DIR"]
        assert install_target == str(isolated_config_dir)

        cache = isolated_config_dir / "plugins" / "cache"
        _make_installed(cache, "claude-plugins-official", "frontend-design")

        have = pi.installed_on_disk()
        assert have == {"frontend-design"}

        # A ~/.claude-only cache (the pre-fix hardcoded base) must NOT be
        # seen — proves installed_on_disk() no longer reads the wrong dir.
        home_cache = tmp_path / ".claude" / "plugins" / "cache"
        _make_installed(home_cache, "claude-plugins-official", "code-review")
        assert pi.installed_on_disk() == {"frontend-design"}


def test_installed_on_disk_ignores_partial_install(tmp_path):
    # A plugin folder with NO version/.claude-plugin/plugin.json (interrupted
    # install) must NOT count as installed — panes wouldn't load it either.
    cache = tmp_path / ".claude" / "plugins" / "cache"
    (cache / "claude-plugins-official" / "frontend-design").mkdir(parents=True)
    assert pi.installed_on_disk(home=tmp_path) == set()


def test_ensure_marketplaces_dedupes_by_repo(monkeypatch):
    calls: list = []
    monkeypatch.setattr(pi, "_ensure_marketplace", lambda repo: calls.append(repo) or (True, "ok"))

    pi.ensure_marketplaces(list(pi.RECOMMENDED))

    # 6 plugins but only 3 distinct marketplace repos → 3 adds, not 6.
    assert calls == list(dict.fromkeys(p.marketplace_repo for p in pi.RECOMMENDED))
    assert len(calls) == 3


def test_install_plugin_success_by_exit_code(monkeypatch):
    # Exit 0 with reworded output (no "successfully installed") + the plugin
    # present on disk → success.
    class _P:
        returncode = 0
        stdout = "Installed frontend-design"
        stderr = ""

    target = pi.RECOMMENDED[1]
    monkeypatch.setattr(pi, "_claude", lambda *a, **k: _P())
    monkeypatch.setattr(pi, "installed_on_disk", lambda: {target.key})
    ok, _msg = pi.install_plugin(target, ensure_marketplace=False)
    assert ok is True


def test_install_plugin_repairs_half_clone_once(monkeypatch, tmp_path):
    # The registry can say installed while an interrupted clone left no
    # loadable plugin.  Repair clears registry + cache, then installs once more.
    class _P:
        returncode = 0
        stdout = "nothing to do"
        stderr = ""

    target = pi.RECOMMENDED[1]
    plugin_id = f"{target.key}@{target.marketplace}"
    claude_calls: list[tuple[tuple, dict]] = []
    uninstall_calls: list[str] = []
    disk_results = iter((set(), {target.key}))
    config_dir = tmp_path / "claude-config"
    partial_cache = config_dir / "plugins" / "cache" / target.marketplace / target.key
    partial_cache.mkdir(parents=True)
    partial_pack = partial_cache / "partial.pack"
    partial_pack.write_text("incomplete", encoding="utf-8")
    partial_pack.chmod(stat.S_IREAD)

    def fake_claude(*args, **kwargs):
        claude_calls.append((args, kwargs))
        return _P()

    monkeypatch.setattr(pi, "_claude", fake_claude)
    monkeypatch.setattr(
        pi,
        "uninstall_plugin",
        lambda candidate: uninstall_calls.append(candidate) or (True, "uninstalled"),
    )
    monkeypatch.setattr(pi, "installed_on_disk", lambda: next(disk_results))
    with patch(
        "agent_takkub.config.default_claude_config_dir",
        return_value=config_dir,
    ):
        ok, msg = pi.install_plugin(target, ensure_marketplace=False)

    assert (ok, msg) == (True, "installed (repaired half-clone)")
    assert uninstall_calls == [plugin_id]
    assert [call[0] for call in claude_calls] == [
        ("plugin", "install", plugin_id),
        ("plugin", "install", plugin_id),
    ]
    assert partial_cache.exists() is False


def test_install_plugin_half_clone_repair_failure(monkeypatch, tmp_path):
    # Repair is attempted only once; a second missing-on-disk result returns a
    # stable actionable error instead of looping or suggesting a restart.
    class _P:
        returncode = 0
        stdout = "nothing to do"
        stderr = ""

    target = pi.RECOMMENDED[1]
    plugin_id = f"{target.key}@{target.marketplace}"
    claude_calls: list[tuple] = []
    uninstall_calls: list[str] = []
    disk_results = iter((set(), set()))

    monkeypatch.setattr(
        pi,
        "_claude",
        lambda *args, **kwargs: claude_calls.append(args) or _P(),
    )
    monkeypatch.setattr(
        pi,
        "uninstall_plugin",
        lambda candidate: uninstall_calls.append(candidate) or (True, "uninstalled"),
    )
    monkeypatch.setattr(pi, "installed_on_disk", lambda: next(disk_results))
    with patch(
        "agent_takkub.config.default_claude_config_dir",
        return_value=tmp_path / "claude-config",
    ):
        ok, msg = pi.install_plugin(target, ensure_marketplace=False)

    assert ok is False
    assert msg == (
        "CLI reported success but plugin not found on disk (repair failed — check npm/git access)"
    )
    assert uninstall_calls == [plugin_id]
    assert claude_calls == [
        ("plugin", "install", plugin_id),
        ("plugin", "install", plugin_id),
    ]


def test_install_plugin_failure_by_exit_code(monkeypatch):
    class _P:
        returncode = 1
        stdout = ""
        stderr = "not found in marketplace"

    monkeypatch.setattr(pi, "_claude", lambda *a, **k: _P())
    ok, msg = pi.install_plugin(pi.RECOMMENDED[1], ensure_marketplace=False)
    assert ok is False
    assert "not found" in msg


def test_missing_plugins_all_when_empty():
    missing = pi.missing_plugins(set())
    assert len(missing) == len(pi.RECOMMENDED)


# ---------------------------------------------------------------------------
# _claude — resolved exe (M3: bare "claude" fails Windows .cmd shim under
# shell=False) + explicit CLAUDE_CONFIG_DIR propagation
# ---------------------------------------------------------------------------


def test_claude_uses_resolved_executable_not_bare_name(monkeypatch, tmp_path):
    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _P()

    monkeypatch.setattr(pi.subprocess, "run", fake_run)
    with patch(
        "agent_takkub.config.find_claude_executable",
        return_value=r"C:\resolved\claude.exe",
    ):
        pi._claude("plugin", "list")

    assert captured["argv"][0] == r"C:\resolved\claude.exe"
    assert captured["argv"][1:] == ["plugin", "list"]


def test_claude_env_sets_config_dir_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    with patch(
        "agent_takkub.config.default_claude_config_dir",
        return_value=tmp_path / "claude-config",
    ):
        env = pi._claude_env()
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude-config")


def test_claude_env_preserves_existing_config_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/profile/override")
    with patch(
        "agent_takkub.config.default_claude_config_dir",
        return_value=tmp_path / "claude-config",
    ):
        env = pi._claude_env()
    assert env["CLAUDE_CONFIG_DIR"] == "/profile/override"


# ---------------------------------------------------------------------------
# normalize_plugin_hook_timeout — #135 (plugin hook timeout too low for
# multi-pane fan-out spawn load)
# ---------------------------------------------------------------------------


def _write_manifest(plugin_dir, payload: dict) -> None:
    d = plugin_dir / ".claude-plugin"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps(payload), encoding="utf-8")


def test_normalize_raises_low_timeout_to_floor(tmp_path):
    plugin_dir = tmp_path / "pordee" / "pordee" / "abc123"
    _write_manifest(
        plugin_dir,
        {
            "name": "pordee",
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "x", "timeout": 5}]}],
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "y", "timeout": 5}]}
                ],
            },
        },
    )

    changes = pi.normalize_plugin_hook_timeout(plugin_dir, floor=30)

    assert len(changes) == 2
    assert {c["event"] for c in changes} == {"SessionStart", "UserPromptSubmit"}
    assert all(c["old"] == 5 and c["new"] == 30 for c in changes)
    manifest = json.loads((plugin_dir / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    assert manifest["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] == 30
    assert manifest["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"] == 30


def test_normalize_leaves_timeout_at_or_above_floor_untouched(tmp_path):
    plugin_dir = tmp_path / "mp" / "plug" / "1.0.0"
    _write_manifest(
        plugin_dir,
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "timeout": 60}]}]}},
    )
    before = (plugin_dir / ".claude-plugin" / "plugin.json").read_text("utf-8")

    changes = pi.normalize_plugin_hook_timeout(plugin_dir, floor=30)

    assert changes == []
    assert (plugin_dir / ".claude-plugin" / "plugin.json").read_text("utf-8") == before


def test_normalize_never_invents_a_missing_timeout_key(tmp_path):
    plugin_dir = tmp_path / "mp" / "plug" / "1.0.0"
    _write_manifest(
        plugin_dir,
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "x"}]}]}},
    )

    changes = pi.normalize_plugin_hook_timeout(plugin_dir, floor=30)

    assert changes == []
    manifest = json.loads((plugin_dir / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    assert "timeout" not in manifest["hooks"]["SessionStart"][0]["hooks"][0]


def test_normalize_swallows_unreadable_manifest(tmp_path):
    plugin_dir = tmp_path / "mp" / "plug" / "1.0.0"
    d = plugin_dir / ".claude-plugin"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text("{not valid json", encoding="utf-8")

    changes = pi.normalize_plugin_hook_timeout(plugin_dir, floor=30)

    assert changes == []


def test_normalize_no_manifest_at_all_is_a_noop(tmp_path):
    plugin_dir = tmp_path / "mp" / "plug" / "1.0.0"
    plugin_dir.mkdir(parents=True)

    assert pi.normalize_plugin_hook_timeout(plugin_dir, floor=30) == []


def test_normalize_is_idempotent_second_call_does_not_rewrite(tmp_path, monkeypatch):
    import pathlib

    plugin_dir = tmp_path / "pordee" / "pordee" / "abc123"
    _write_manifest(
        plugin_dir,
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "timeout": 5}]}]}},
    )

    first = pi.normalize_plugin_hook_timeout(plugin_dir, floor=30)
    assert len(first) == 1

    write_calls: list = []
    real_write_text = pathlib.Path.write_text

    def spy_write_text(self, *args, **kwargs):
        write_calls.append(self)
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", spy_write_text)

    second = pi.normalize_plugin_hook_timeout(plugin_dir, floor=30)

    assert second == []
    assert write_calls == []  # no tmp-file write attempted on the no-op pass


def test_normalize_uses_hooks_json_convention_file(tmp_path):
    # Some plugins (superpowers, remember, security-guidance) declare hooks
    # via hooks/hooks.json instead of inline in plugin.json.
    plugin_dir = tmp_path / "mp" / "plug" / "1.0.0"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "plug"}), encoding="utf-8"
    )
    hooks_dir = plugin_dir / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "hooks.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "timeout": 5}]}]}}),
        encoding="utf-8",
    )

    changes = pi.normalize_plugin_hook_timeout(plugin_dir, floor=30)

    assert len(changes) == 1
    manifest = json.loads((hooks_dir / "hooks.json").read_text("utf-8"))
    assert manifest["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] == 30


def test_plugin_hook_timeout_floor_env_override(monkeypatch):
    monkeypatch.setenv("TAKKUB_PLUGIN_HOOK_TIMEOUT_FLOOR", "45")
    assert pi._plugin_hook_timeout_floor() == 45


def test_plugin_hook_timeout_floor_default_on_bad_env(monkeypatch):
    monkeypatch.setenv("TAKKUB_PLUGIN_HOOK_TIMEOUT_FLOOR", "not-a-number")
    assert pi._plugin_hook_timeout_floor() == pi.PLUGIN_HOOK_TIMEOUT_FLOOR_DEFAULT


def test_claude_passes_env_to_subprocess_run(monkeypatch, tmp_path):
    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _P()

    monkeypatch.setattr(pi.subprocess, "run", fake_run)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    with (
        patch("agent_takkub.config.find_claude_executable", return_value="claude"),
        patch(
            "agent_takkub.config.default_claude_config_dir",
            return_value=tmp_path / "claude-config",
        ),
    ):
        pi._claude("plugin", "list")

    assert captured["env"] is not None
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude-config")
