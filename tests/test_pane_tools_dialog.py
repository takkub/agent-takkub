"""Pure-logic tests for pane_tools_dialog — no QApplication needed."""

from __future__ import annotations

import json
from unittest.mock import patch

from PyQt6.QtWidgets import QMessageBox

from agent_takkub.pane_tools_dialog import (
    ROLES,
    TAB_MCP,
    TAB_PLUGINS,
    TAB_TEAM,
    _default_plugins_installed_file,
    build_matrix,
    diff_role_items,
    discover_marketplace_plugins,
    discover_marketplaces,
    matrix_to_role_items,
    parse_install_form,
    tool_hint,
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


# ---------------------------------------------------------------------------
# A6-redesign: tool_hint (Step-2 tool card blurbs) + tab index constants
# ---------------------------------------------------------------------------


def test_tool_hint_known_name_returns_specific_blurb():
    assert tool_hint("playwright") == "เปิด browser เทส"


def test_tool_hint_unknown_name_falls_back_to_generic():
    assert tool_hint("some-brand-new-mcp") == "เครื่องมือเสริม"


def test_tab_indices_are_distinct_and_ordered():
    assert (TAB_MCP, TAB_PLUGINS, TAB_TEAM) == (0, 1, 2)


# ---------------------------------------------------------------------------
# A6-redesign: Team & Roles tab — guided create, end-to-end under a real
# (offscreen) QApplication
# ---------------------------------------------------------------------------


def test_dialog_opens_to_requested_tab(monkeypatch, tmp_path):
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub.pane_tools_dialog import PaneToolsDialog

    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")

    dlg = PaneToolsDialog(initial_tab=TAB_TEAM)
    try:
        assert dlg._tabs.currentIndex() == TAB_TEAM
    finally:
        dlg.deleteLater()


def test_new_role_tab_creates_and_registers_role(monkeypatch, tmp_path):
    from agent_takkub import custom_roles, roles
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub.pane_tools_dialog import PaneToolsDialog

    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    saved_custom = dict(roles._CUSTOM)
    roles._CUSTOM.clear()

    dlg = PaneToolsDialog()
    try:
        dlg._nr_name.setText("data-eng")
        dlg._nr_label.setText("Data Eng")
        dlg._nr_instructions.setPlainText("Handles ETL pipelines and warehouse schemas.")
        dlg._on_create_role_clicked()

        assert "data-eng" in dlg._status_label.text()
        resolved = roles.by_name("data-eng")
        assert resolved is not None
        assert resolved.label == "Data Eng"
        assert custom_roles.role_file_path("data-eng").exists()
        # Form clears after a successful create.
        assert dlg._nr_name.text() == ""
    finally:
        dlg.deleteLater()
        roles._CUSTOM.clear()
        roles._CUSTOM.update(saved_custom)


def test_new_role_tab_rejects_collision_with_builtin(monkeypatch, tmp_path):
    from agent_takkub import custom_roles
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub.pane_tools_dialog import PaneToolsDialog

    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")

    dlg = PaneToolsDialog()
    try:
        dlg._nr_name.setText("qa")
        dlg._nr_instructions.setPlainText("shadow qa")
        with patch("agent_takkub.pane_tools_dialog.QMessageBox.warning") as warn:
            dlg._on_create_role_clicked()
        warn.assert_called_once()
        assert not custom_roles.role_file_path("qa").exists()
    finally:
        dlg.deleteLater()


def test_team_list_shows_created_custom_role_as_removable(monkeypatch, tmp_path):
    from agent_takkub import custom_roles, roles
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub.pane_tools_dialog import PaneToolsDialog

    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    saved_custom = dict(roles._CUSTOM)
    roles._CUSTOM.clear()

    dlg = PaneToolsDialog()
    try:
        dlg._nr_name.setText("data-eng")
        dlg._nr_instructions.setPlainText("Handles ETL pipelines.")
        dlg._on_create_role_clicked()

        # The freshly created custom role gets its own row in the left list,
        # distinct from `roles.ALL_DEFAULT` (which stays fixed regardless of
        # what gets created — the built-in section never changes size).
        row_count = dlg._team_list_layout.count()
        assert row_count > len(roles.ALL_DEFAULT)
    finally:
        dlg.deleteLater()
        roles._CUSTOM.clear()
        roles._CUSTOM.update(saved_custom)


def test_delete_role_removes_from_registry_and_live_process(monkeypatch, tmp_path):
    from agent_takkub import custom_roles, roles
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub.pane_tools_dialog import PaneToolsDialog

    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    saved_custom = dict(roles._CUSTOM)
    roles._CUSTOM.clear()

    dlg = PaneToolsDialog()
    try:
        dlg._nr_name.setText("data-eng")
        dlg._on_create_role_clicked()
        assert roles.by_name("data-eng") is not None

        with patch(
            "agent_takkub.pane_tools_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            dlg._on_delete_role_clicked("data-eng")

        assert "data-eng" not in custom_roles.load_custom_roles()
        assert roles.by_name("data-eng") is None
    finally:
        dlg.deleteLater()
        roles._CUSTOM.clear()
        roles._CUSTOM.update(saved_custom)


def test_delete_role_declined_confirm_keeps_role(monkeypatch, tmp_path):
    from agent_takkub import custom_roles, roles
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub.pane_tools_dialog import PaneToolsDialog

    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    saved_custom = dict(roles._CUSTOM)
    roles._CUSTOM.clear()

    dlg = PaneToolsDialog()
    try:
        dlg._nr_name.setText("data-eng")
        dlg._on_create_role_clicked()

        with patch(
            "agent_takkub.pane_tools_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ):
            dlg._on_delete_role_clicked("data-eng")

        assert "data-eng" in custom_roles.load_custom_roles()
        assert roles.by_name("data-eng") is not None
    finally:
        dlg.deleteLater()
        roles._CUSTOM.clear()
        roles._CUSTOM.update(saved_custom)


def test_new_role_auto_color_follows_typed_name_until_touched(monkeypatch, tmp_path):
    from agent_takkub import custom_roles, project_nav
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub.pane_tools_dialog import PaneToolsDialog

    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")

    dlg = PaneToolsDialog()
    try:
        dlg._nr_name.setText("data-eng")
        assert dlg._nr_color == project_nav._avatar_color("data-eng")

        # Picking a swatch marks the color as user-touched — further name
        # edits must NOT clobber the explicit choice.
        other = next(c for c in project_nav._AVATAR_COLORS if c != dlg._nr_color)
        dlg._on_swatch_clicked(other)
        dlg._nr_name.setText("data-eng-2")
        assert dlg._nr_color == other
    finally:
        dlg.deleteLater()


def test_new_role_overlap_warning_uses_friendly_wording(monkeypatch, tmp_path):
    from agent_takkub import custom_roles, skill_audit
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub.pane_tools_dialog import PaneToolsDialog

    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(skill_audit, "audit_new_role_text", lambda name, text: [("qa", 0.72)])

    dlg = PaneToolsDialog()
    try:
        dlg._nr_name.setText("shadow-qa")
        dlg._nr_instructions.setPlainText("some instructions")
        text = dlg._nr_overlap_label.text()
        assert "qa (72%)" in text
        assert "ตั้งใจแยก ok เลย" in text
    finally:
        dlg.deleteLater()


def test_advanced_section_starts_collapsed_and_toggles(monkeypatch, tmp_path):
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub.pane_tools_dialog import PaneToolsDialog

    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")

    dlg = PaneToolsDialog()
    try:
        # The dialog is never shown in this test (offscreen QPA, no exec()),
        # so isVisible() is always False regardless of state (ancestor-chain
        # visibility) — isHidden() reflects the widget's own explicit
        # hide()/setVisible() flag instead, which is what `_on_toggle_advanced`
        # actually toggles.
        assert dlg._nr_advanced_body.isHidden()
        dlg._on_toggle_advanced()
        assert not dlg._nr_advanced_body.isHidden()
    finally:
        dlg.deleteLater()


def test_advanced_provider_field_disabled_pending_103(monkeypatch, tmp_path):
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub.pane_tools_dialog import PaneToolsDialog

    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")

    dlg = PaneToolsDialog()
    try:
        assert not dlg._nr_provider_combo.isEnabled()
    finally:
        dlg.deleteLater()
