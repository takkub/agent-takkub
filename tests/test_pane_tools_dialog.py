"""Pure-logic tests for pane_tools_dialog — no QApplication needed."""

from __future__ import annotations

import json

from agent_takkub.pane_tools_dialog import (
    ROLES,
    build_matrix,
    diff_role_items,
    discover_marketplace_plugins,
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


def test_parse_install_form_splits_args_on_whitespace():
    result = parse_install_form("my-mcp", "npx", "-y some-pkg@1.0.0")
    assert result == ("my-mcp", {"command": "npx", "args": ["-y", "some-pkg@1.0.0"]})


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
    }
