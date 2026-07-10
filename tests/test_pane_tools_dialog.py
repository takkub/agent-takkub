"""Pure-logic tests for pane_tools_dialog.

The Qt PaneToolsDialog ("🔧 Tools" button) was removed 2026-07-10 —
superseded 100% by SettingsWindow's native "👥 Team" view. This module now
only holds the pure per-role MCP/plugin policy helpers that settings_window.py
still depends on (``build_matrix``/``matrix_to_role_items``/``diff_role_items``/
``master_mcps``/``policy_role_items``/``discover_marketplaces``/
``parse_install_form``); see tests/test_settings_window.py for their
Qt-integration coverage.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from agent_takkub.pane_tools_dialog import (
    ROLES,
    _default_plugins_installed_file,
    build_matrix,
    diff_role_items,
    discover_marketplace_plugins,
    discover_marketplaces,
    matrix_to_role_items,
    parse_install_form,
)


def test_build_matrix_marks_enabled_items_true():
    role_items = {"frontend": ["playwright"], "backend": []}
    matrix = build_matrix(("frontend", "backend"), ["playwright", "obsidian-vault"], role_items)
    assert matrix == {
        "frontend": {"playwright": True, "obsidian-vault": False},
        "backend": {"playwright": False, "obsidian-vault": False},
    }


def test_build_matrix_missing_role_defaults_to_all_false():
    matrix = build_matrix(("qa",), ["playwright"], {})
    assert matrix == {"qa": {"playwright": False}}


def test_matrix_to_role_items_round_trips_and_sorts():
    matrix = {"qa": {"b": True, "a": True, "c": False}}
    assert matrix_to_role_items(matrix) == {"qa": ["a", "b"]}


def test_matrix_to_role_items_empty_when_all_unchecked():
    matrix = {"lead": {"obsidian-vault": False}}
    assert matrix_to_role_items(matrix) == {"lead": []}


def test_diff_role_items_detects_added_and_removed():
    original = {"qa": ["playwright"], "frontend": ["obsidian-vault"]}
    updated = {"qa": ["playwright", "chrome-devtools"], "frontend": []}
    changes = diff_role_items(original, updated)
    assert changes == {
        "qa": (["chrome-devtools"], []),
        "frontend": ([], ["obsidian-vault"]),
    }


def test_diff_role_items_no_changes_omits_role():
    same = {"qa": ["playwright"]}
    assert diff_role_items(same, same) == {}


def test_diff_role_items_handles_new_role_not_in_original():
    changes = diff_role_items({}, {"critic": ["chrome-devtools"]})
    assert changes == {"critic": (["chrome-devtools"], [])}


def test_discover_marketplace_plugins_reads_installed_registry(tmp_path):
    installed = tmp_path / "installed_plugins.json"
    installed.write_text(
        json.dumps({"plugins": {"pordee@pordee": [], "code-review@claude-plugins-official": []}}),
        encoding="utf-8",
    )
    assert discover_marketplace_plugins(installed) == [
        "code-review@claude-plugins-official",
        "pordee@pordee",
    ]


def test_discover_marketplace_plugins_missing_file_returns_empty(tmp_path):
    assert discover_marketplace_plugins(tmp_path / "nope.json") == []


def test_discover_marketplace_plugins_malformed_json_returns_empty(tmp_path):
    bad = tmp_path / "installed_plugins.json"
    bad.write_text("{not json", encoding="utf-8")
    assert discover_marketplace_plugins(bad) == []


def test_discover_marketplaces_intersects_safe_plugins(tmp_path):
    installed = tmp_path / "installed_plugins.json"
    installed.write_text(
        json.dumps(
            {
                "plugins": {
                    "superpowers@superpowers-dev": [],
                    "pordee@pordee": [],
                    "code-review@claude-plugins-official": [],
                    "frontend-design@claude-plugins-official": [],  # dedups → one column
                    "agent-skills@addy-agent-skills": [],
                    "claude-obsidian@claude-obsidian-marketplace": [],  # not in _SAFE_PLUGINS
                }
            }
        ),
        encoding="utf-8",
    )
    # Marketplace names (not name@marketplace), deduped, ∩ _SAFE_PLUGINS, sorted.
    # claude-obsidian-marketplace is dropped — pane injection can never load it.
    assert discover_marketplaces(installed) == [
        "addy-agent-skills",
        "claude-plugins-official",
        "pordee",
        "superpowers-dev",
    ]


def test_discover_marketplaces_missing_file_returns_empty(tmp_path):
    assert discover_marketplaces(tmp_path / "nope.json") == []


def test_discover_marketplaces_malformed_json_returns_empty(tmp_path):
    bad = tmp_path / "installed_plugins.json"
    bad.write_text("{not json", encoding="utf-8")
    assert discover_marketplaces(bad) == []


def test_parse_install_form_splits_args_on_whitespace():
    result = parse_install_form("my-mcp", "npx", "-y some-pkg@1.0.0")
    assert result == ("my-mcp", {"command": "npx", "args": ["-y", "some-pkg@1.0.0"]})


# ---------------------------------------------------------------------------
# M2: default installed-registry path resolves via
# config.default_claude_config_dir() (profile/isolated-mode aware), not a
# hardcoded ~/.claude — docs/reviews/2026-07-10-xplatform-CONSOLIDATED.md
# ---------------------------------------------------------------------------


def test_default_plugins_installed_file_uses_config_dir(tmp_path):
    with patch(
        "agent_takkub.config.default_claude_config_dir",
        return_value=tmp_path / "claude-config",
    ):
        path = _default_plugins_installed_file()
    assert path == tmp_path / "claude-config" / "plugins" / "installed_plugins.json"


def test_discover_marketplace_plugins_no_arg_resolves_default_file(tmp_path):
    cfg_dir = tmp_path / "claude-config"
    (cfg_dir / "plugins").mkdir(parents=True)
    (cfg_dir / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"pordee@pordee": []}}), encoding="utf-8"
    )
    with patch("agent_takkub.config.default_claude_config_dir", return_value=cfg_dir):
        assert discover_marketplace_plugins() == ["pordee@pordee"]


def test_discover_marketplaces_no_arg_resolves_default_file(tmp_path):
    cfg_dir = tmp_path / "claude-config"
    (cfg_dir / "plugins").mkdir(parents=True)
    (cfg_dir / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"superpowers@superpowers-dev": []}}), encoding="utf-8"
    )
    with patch("agent_takkub.config.default_claude_config_dir", return_value=cfg_dir):
        assert discover_marketplaces() == ["superpowers-dev"]


def test_parse_install_form_strips_whitespace_from_name_and_command():
    result = parse_install_form("  my-mcp  ", "  npx  ", "")
    assert result == ("my-mcp", {"command": "npx", "args": []})


def test_parse_install_form_rejects_missing_name():
    assert parse_install_form("", "npx", "-y pkg") is None


def test_parse_install_form_rejects_missing_command():
    assert parse_install_form("my-mcp", "", "-y pkg") is None


def test_roles_tuple_covers_expected_roles():
    assert set(ROLES) == {
        "lead",
        "frontend",
        "backend",
        "mobile",
        "devops",
        "qa",
        "reviewer",
        "critic",
        "designer",
        "analyst",
        "security",
        "docs",
    }
