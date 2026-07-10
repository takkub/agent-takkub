"""Pure-logic tests for pane_tools_dialog — no QApplication needed."""

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


# ── Qt construction smoke test ────────────────────────────────────────────────
# The pure-logic tests above structurally cannot catch a crash inside the
# QDialog constructor (they never touch Qt). PaneToolsDialog.__init__ builds
# both tabs eagerly, so simply constructing it exercises _fill_matrix_table for
# real — the exact path where a bad `setAlignment(box, int)` call raised
# TypeError and made the 🔧 Tools chip silently do nothing (Qt swallows the
# exception in the clicked slot). Construct + drive save under the session
# QApplication (conftest `_qt_session_app`, offscreen platform).


def test_dialog_constructs_and_save_runs(monkeypatch, tmp_path):
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub.pane_tools_dialog import PaneToolsDialog

    # Isolate the policy file so a real ~/.takkub/pane-tools.json is untouched.
    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")

    dlg = PaneToolsDialog()
    try:
        # Both matrices built without raising = the regression is gone.
        assert set(dlg._mcp_boxes) == set(ROLES)
        assert set(dlg._plugin_boxes) == set(ROLES)
        # Save with no edits must run cleanly and report status, not throw.
        dlg._on_save_clicked()
        assert dlg._status_label.text()
    finally:
        dlg.deleteLater()


def test_save_plugins_only_change_does_not_wipe_mcps(monkeypatch, tmp_path):
    """Regression: a plugins-only Save must not silently deny a role's MCPs.

    ``set_role_items`` seeds a *fresh* role entry's sibling kind to ``[]`` (an
    explicit deny). So persisting only a plugin change once stripped
    playwright + chrome-devtools from qa/critic/designer — QA silently lost the
    ability to drive a browser. ``_on_save_clicked`` now writes BOTH kinds for
    any changed role; qa's MCPs must survive a plugins-only edit.
    """
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub import shared_dev_tools as sdt
    from agent_takkub.pane_tools_dialog import PaneToolsDialog

    # Isolate the policy file AND the master MCP registry so real config and
    # the runtime shared-mcp.json are untouched and the matrix is deterministic.
    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    master = tmp_path / "shared-mcp.json"
    master.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {"type": "stdio", "command": "npx", "args": []},
                    "chrome-devtools": {"type": "stdio", "command": "npx", "args": []},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sdt, "SHARED_MCP_FILE", master)

    dlg = PaneToolsDialog()
    try:
        # qa's MCP boxes are checked by built-in default (playwright + chrome-devtools).
        assert dlg._mcp_boxes["qa"]["playwright"].isChecked()
        assert dlg._mcp_boxes["qa"]["chrome-devtools"].isChecked()
        # Simulate a plugins-ONLY change for qa (a plugin that no longer has a
        # matrix column — e.g. uninstalled — reads as "removed" on Save).
        dlg._orig_plugin_items = {role: [] for role in dlg._orig_plugin_items}
        dlg._orig_plugin_items["qa"] = ["pordee"]
        dlg._on_save_clicked()
        # qa's MCPs must be preserved, NOT collapsed to an empty deny override.
        assert ptp.effective_mcps("qa", None) == frozenset({"playwright", "chrome-devtools"})
    finally:
        dlg.deleteLater()


def test_plugin_matrix_renders_marketplace_defaults_checked(monkeypatch, tmp_path):
    """Regression: the Plugins matrix must render each role's default
    marketplaces CHECKED, and a no-op Save must NOT write a deny-all override.

    The matrix once used ``name@marketplace`` columns while the policy stores
    *marketplace* names, so every plugin cell rendered unchecked even when the
    plugins were enabled, and any Save wiped every role's plugins (the
    2026-07-02 deny-all). Columns are now marketplace-granular, so a checkbox's
    identity matches what ``effective_plugins`` reads back.
    """
    from agent_takkub import pane_tools_dialog as ptd
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub.pane_tools_dialog import PaneToolsDialog

    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    # Deterministic marketplace columns regardless of the host's install registry
    # (CI runners have no cockpit plugins installed).
    monkeypatch.setattr(
        ptd,
        "discover_marketplaces",
        lambda: ["addy-agent-skills", "claude-plugins-official", "pordee", "superpowers-dev"],
    )

    dlg = PaneToolsDialog()
    try:
        # frontend inherits the teammate default: superpowers-dev + pordee +
        # claude-plugins-official checked; addy-agent-skills is safe-but-off.
        fe = dlg._plugin_boxes["frontend"]
        assert fe["superpowers-dev"].isChecked()
        assert fe["pordee"].isChecked()
        assert fe["claude-plugins-official"].isChecked()
        assert not fe["addy-agent-skills"].isChecked()
        # lead gets only pordee.
        lead = dlg._plugin_boxes["lead"]
        assert lead["pordee"].isChecked()
        assert not lead["superpowers-dev"].isChecked()

        # A Save with no edits is a true no-op: no plugin override is written, so
        # effective_plugins still falls through to the built-in default (None
        # here = no override), NOT an empty deny.
        dlg._on_save_clicked()
        assert "frontend" not in ptp.load_policy()
        assert ptp.effective_plugins("frontend", None) is None
    finally:
        dlg.deleteLater()
