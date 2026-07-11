"""Widget smoke tests for settings_window.SettingsWindow.

Offscreen QPA (session-scoped QApplication from tests/conftest.py) —
"tofu" widget-property assertions per the task spec: stacked-page count, nav
switching, matrix cell toggle state, pipeline hop rendering, and real
config-persist wiring (create_role, pane_tools_policy, pipeline_config).
Full interactive visual verification is left to the user per the project's
targeted-tests rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QMessageBox

from agent_takkub import (
    claude_auth_config,
    config,
    custom_roles,
    pane_tools_policy,
    pipeline_config,
    project_nav,
    provider_config,
    provider_state,
    settings_window,
    shared_dev_tools,
    user_profile,
)
from agent_takkub import roles as roles_mod


@pytest.fixture(autouse=True)
def _isolate_settings_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect every on-disk store SettingsWindow touches to tmp, and clear
    the runtime custom-role registry so tests never leak into each other or
    the real ~/.takkub. provider_config's own paths are already isolated by
    the autouse fixture in tests/conftest.py."""
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(pipeline_config, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(pipeline_config, "_PATH", tmp_path / "pipelines.json")
    monkeypatch.setattr(provider_state, "_PATH", tmp_path / "disabled-providers.json")
    monkeypatch.setattr(pane_tools_policy, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(shared_dev_tools, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
    # Users view (VIEW_USERS) touches user_profile's registry on every
    # SettingsWindow() construction (list_profiles() is called eagerly to
    # build the Profiles/Claude Auth tabs) — isolate it like every other
    # store above so tests never read/write the real ~/.takkub registry.
    monkeypatch.setattr(user_profile, "_REGISTRY_PATH", tmp_path / "user-profiles.json")
    monkeypatch.setattr(user_profile, "_DEFAULT_CONFIG_DIR", tmp_path / "default-claude-config")
    saved = dict(roles_mod._CUSTOM)
    roles_mod._CUSTOM.clear()
    yield
    roles_mod._CUSTOM.clear()
    roles_mod._CUSTOM.update(saved)


class TestSettingsWindowStructure:
    def test_has_nine_stacked_views(self) -> None:
        # 8 original views + the new real Skill Catalog (index 8).
        dlg = settings_window.SettingsWindow()
        assert dlg._stack.count() == 9
        dlg.deleteLater()

    def test_initial_view_defaults_to_providers_roles(self) -> None:
        dlg = settings_window.SettingsWindow()
        assert dlg._stack.currentIndex() == settings_window.VIEW_PROVIDERS_ROLES
        dlg.deleteLater()

    def test_nav_click_switches_stack_page(self) -> None:
        dlg = settings_window.SettingsWindow()
        dlg._nav_buttons[settings_window.VIEW_MCP_MATRIX].click()
        assert dlg._stack.currentIndex() == settings_window.VIEW_MCP_MATRIX
        dlg.deleteLater()

    def test_active_nav_property_tracks_current_view(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        dlg._goto_view(settings_window.VIEW_MCP_MATRIX)
        assert dlg._nav_buttons[settings_window.VIEW_MCP_MATRIX].property("active") is True
        assert dlg._nav_buttons[settings_window.VIEW_PIPELINE_BUILDER].property("active") is False
        dlg.deleteLater()

    def test_save_button_uses_gold_style(self) -> None:
        dlg = settings_window.SettingsWindow()
        assert dlg._save_btn.objectName() == "goldButton"
        dlg.deleteLater()

    def test_header_updates_with_view(self) -> None:
        dlg = settings_window.SettingsWindow()
        dlg._goto_view(settings_window.VIEW_NEW_ROLE)
        assert dlg._content_title.text() == "New Role"
        dlg.deleteLater()


class TestNewRoleView:
    def test_create_role_persists_and_registers_live(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        dlg._nr_name.setText("data-eng")
        dlg._nr_label.setText("Data Eng")
        dlg._nr_instructions.setPlainText("do data things")
        dlg._on_create_role_clicked()

        assert "data-eng" in custom_roles.load_custom_roles()
        assert roles_mod.by_name("data-eng") is not None
        assert dlg._nr_status.text().startswith("✓")
        # Form resets on success (status message is deliberately kept).
        assert dlg._nr_name.text() == ""
        dlg.deleteLater()

    def test_reserved_name_rejected_without_creating(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        dlg._nr_name.setText("lead")
        dlg._on_create_role_clicked()

        assert "lead" not in custom_roles.load_custom_roles()
        assert dlg._nr_status.text().startswith("⚠️")
        dlg.deleteLater()

    def test_footer_save_apply_creates_role_and_accepts(self) -> None:
        """Codex High #2 — footer Save & Apply while on the New Role view
        must dispatch to the real create transaction, not just save
        provider/pipeline state and close over an untouched form."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        dlg._nr_name.setText("data-eng")
        dlg._nr_label.setText("Data Eng")

        dlg._on_save_apply_clicked()

        assert "data-eng" in custom_roles.load_custom_roles()
        assert dlg.result() == QDialog.DialogCode.Accepted
        dlg.deleteLater()

    def test_footer_save_apply_invalid_form_does_not_close_dialog(self) -> None:
        """A reserved/invalid name must not accept() and discard the form —
        the old behavior saved provider/pipeline state and closed regardless
        of whether New Role's own form was valid."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        dlg._nr_name.setText("lead")  # reserved, create_role() rejects it

        dlg._on_save_apply_clicked()

        assert dlg.result() != QDialog.DialogCode.Accepted
        assert dlg._nr_status.text().startswith("⚠️")
        dlg.deleteLater()

    def test_new_role_fields_mark_dirty(self) -> None:
        """Codex Medium #6 — New Role's fields didn't feed _mark_dirty at
        all, so no unsaved-changes indicator ever showed for this view."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        assert dlg._dirty is False
        dlg._nr_name.setText("data-eng")
        assert dlg._dirty is True
        dlg.deleteLater()

    def test_default_swatch_color_is_in_palette(self) -> None:
        """Codex/Gemini #17 — the initial swatch color must be one of the
        selectable palette colors so a swatch shows selected on first open."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        assert dlg._nr_color == project_nav._AVATAR_COLORS[0]
        assert dlg._nr_color in project_nav._AVATAR_COLORS
        dlg.deleteLater()


class TestNewRoleSkillPicker:
    """New Role form's real-skill checkbox list (scans .claude/skills/)."""

    @staticmethod
    def _write_skill(root: Path, name: str, description: str) -> None:
        d = root / ".claude" / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n", encoding="utf-8"
        )

    def test_checkbox_list_populated_from_scanned_skills(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
        self._write_skill(tmp_path, "test-skill", "does a thing")
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        assert [s.name for s, _chk in dlg._nr_skill_checks] == ["test-skill"]
        dlg.deleteLater()

    def test_no_skills_dir_shows_empty_list_without_crashing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        assert dlg._nr_skill_checks == []
        dlg.deleteLater()

    def test_selected_skill_embedded_into_default_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#4 in the task spec — an empty Instructions box still gets the
        skill reference embedded into the generated default template."""
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
        self._write_skill(tmp_path, "test-skill", "does a thing")
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        dlg._nr_name.setText("data-eng")
        dlg._nr_label.setText("Data Eng")
        dlg._nr_skill_checks[0][1].setChecked(True)

        assert dlg._on_create_role_clicked() is True

        role_file = custom_roles.CUSTOM_AGENTS_DIR / "data-eng.md"
        text = role_file.read_text(encoding="utf-8")
        assert "อ่าน skill: test-skill — does a thing" in text
        dlg.deleteLater()

    def test_selected_skill_embedded_into_typed_instructions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
        self._write_skill(tmp_path, "test-skill", "does a thing")
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        dlg._nr_name.setText("data-eng")
        dlg._nr_instructions.setPlainText("custom instructions here")
        dlg._nr_skill_checks[0][1].setChecked(True)

        assert dlg._on_create_role_clicked() is True

        role_file = custom_roles.CUSTOM_AGENTS_DIR / "data-eng.md"
        text = role_file.read_text(encoding="utf-8")
        assert "custom instructions here" in text
        assert "อ่าน skill: test-skill — does a thing" in text
        dlg.deleteLater()

    def test_unchecked_skill_not_embedded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
        self._write_skill(tmp_path, "test-skill", "does a thing")
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        dlg._nr_name.setText("data-eng")
        dlg._nr_instructions.setPlainText("custom instructions here")

        assert dlg._on_create_role_clicked() is True

        role_file = custom_roles.CUSTOM_AGENTS_DIR / "data-eng.md"
        text = role_file.read_text(encoding="utf-8")
        assert "test-skill" not in text
        dlg.deleteLater()


class TestProvidersRolesView:
    def test_save_apply_persists_role_enabled_and_provider(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        dlg._role_toggles["qa"].setChecked(False)
        combo = dlg._role_provider_combos["backend"]
        combo.setCurrentIndex(combo.findData("codex"))
        dlg._on_save_apply_clicked()

        payload = pipeline_config.load(None)
        assert payload["rolesEnabled"]["qa"] is False
        assert provider_config.provider_for("backend") == "codex"
        assert dlg.result() == QDialog.DialogCode.Accepted
        dlg.deleteLater()

    def test_save_apply_stages_provider_disable_without_writing_disk(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        dlg._provider_toggles["codex"].setChecked(False)
        dlg._on_save_apply_clicked()

        assert dlg.pending_provider_disabled == {"codex": True}
        # Caller (user_actions._on_team_chip_clicked) applies this via
        # orchestrator.toggle_provider — SettingsWindow itself never writes
        # disabled-providers.json directly.
        assert provider_state.is_disabled("codex") is False
        dlg.deleteLater()

    def test_reset_reverts_unsaved_toggle(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        dlg._role_toggles["qa"].setChecked(False)
        assert dlg._dirty is True
        dlg._on_reset_clicked()
        assert dlg._role_toggles["qa"].isChecked() is True
        assert dlg._dirty is False
        dlg.deleteLater()

    def test_lead_row_is_locked(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        assert "lead" not in dlg._role_toggles
        assert "lead" not in dlg._role_provider_combos
        dlg.deleteLater()

    def test_save_apply_preserves_out_of_scope_role_override(self) -> None:
        """Codex High #1 — save_role_overrides() used to full-replace the
        entire role-providers file with only the roles this page renders a
        combo for; a custom role's pre-existing override (never shown here)
        must survive a Save & Apply of an unrelated built-in role."""
        provider_config.save_providers({"data-eng": "codex"})
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        combo = dlg._role_provider_combos["backend"]
        combo.setCurrentIndex(combo.findData("gemini"))

        dlg._on_save_apply_clicked()

        assert provider_config.load_providers() == {"data-eng": "codex", "backend": "gemini"}
        dlg.deleteLater()

    def test_save_apply_disabled_until_dirty(self) -> None:
        """Gemini #16 — nothing staged at open time means nothing to apply."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        assert dlg._save_btn.isEnabled() is False
        dlg._role_toggles["qa"].setChecked(False)
        assert dlg._save_btn.isEnabled() is True
        dlg.deleteLater()

    def test_reset_on_one_view_keeps_another_views_dirty_state(self) -> None:
        """Codex Medium #6 — dirty must be tracked per-view, not globally."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        dlg._role_toggles["qa"].setChecked(False)
        dlg._goto_view(settings_window.VIEW_NEW_ROLE)
        dlg._nr_name.setText("data-eng")
        assert dlg._dirty is True

        dlg._on_reset_clicked()  # reverts the New Role view only

        assert dlg._nr_name.text() == ""
        # Providers & Roles' staged qa-disable must still be dirty/unsaved.
        assert dlg._dirty is True
        assert dlg._role_toggles["qa"].isChecked() is False
        dlg.deleteLater()

    def test_substitute_badge_shown_when_selected_provider_unavailable(self) -> None:
        """Gemini #12 — the "→ Claude" substitute badge reflects the combo's
        current selection, not just the on-disk value. (Offscreen tests never
        `.show()` the dialog, so `isVisible()` always reads False regardless
        of state — `isHidden()` reflects the widget's own `setVisible()`
        call, same pattern as `_mcp_empty`/`_plugins_empty` above.)"""
        provider_state.set_disabled("codex", True)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        combo = dlg._role_provider_combos["backend"]
        badge = dlg._role_provider_badges["backend"]
        assert badge.isHidden() is True  # default is claude — no substitution

        combo.setCurrentIndex(combo.findData("codex"))
        assert badge.isHidden() is False

        combo.setCurrentIndex(combo.findData("claude"))
        assert badge.isHidden() is True
        dlg.deleteLater()

    def test_builtin_role_has_no_delete_button(self) -> None:
        """Built-in roles must never render the delete affordance custom
        roles get (critic visual-review round-2 #1)."""
        from PyQt6.QtWidgets import QPushButton

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        row = dlg._role_toggles["qa"].parent()
        assert not any(
            isinstance(w, QPushButton) and w.text() == "✕" for w in row.findChildren(QPushButton)
        )
        dlg.deleteLater()

    def test_custom_role_has_delete_button_that_removes_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Critic visual-review round-2 #1 — a custom role can be created but
        was previously never removable from the UI (Nielsen #3)."""
        from PyQt6.QtWidgets import QPushButton

        custom_roles.create_role("data-eng", "Data Eng", "#112233", 1, 5, "x")
        role = custom_roles.load_custom_roles()["data-eng"]
        roles_mod.register_role(role)
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        row = dlg._role_toggles["data-eng"].parent()
        delete_btn = next(w for w in row.findChildren(QPushButton) if w.text() == "✕")

        delete_btn.click()

        assert "data-eng" not in custom_roles.load_custom_roles()
        assert not custom_roles.role_file_path("data-eng").exists()
        assert roles_mod.by_name("data-eng") is None
        assert "data-eng" not in dlg._role_toggles
        dlg.deleteLater()

    def test_delete_declined_keeps_custom_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from PyQt6.QtWidgets import QPushButton

        custom_roles.create_role("data-eng", "Data Eng", "#112233", 1, 5, "x")
        role = custom_roles.load_custom_roles()["data-eng"]
        roles_mod.register_role(role)
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        row = dlg._role_toggles["data-eng"].parent()
        delete_btn = next(w for w in row.findChildren(QPushButton) if w.text() == "✕")

        delete_btn.click()

        assert "data-eng" in custom_roles.load_custom_roles()
        assert "data-eng" in dlg._role_toggles
        dlg.deleteLater()


class TestMcpMatrixView:
    def test_grid_has_a_toggle_per_role_per_item(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            shared_dev_tools, "list_master_mcps", lambda: ["playwright", "context7"]
        )
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_MCP_MATRIX)
        assert set(dlg._mcp_toggles.keys()) == set(settings_window._matrix_roles())
        for items in dlg._mcp_toggles.values():
            assert set(items.keys()) == {"playwright", "context7"}
        # Widgets never .show()'n in offscreen tests always report
        # isVisible()=False regardless of state (ancestor-chain visibility);
        # isHidden() reflects the widget's own explicit setVisible() call.
        assert dlg._mcp_empty.isHidden()
        dlg.deleteLater()

    def test_empty_registry_shows_empty_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shared_dev_tools, "list_master_mcps", lambda: [])
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_MCP_MATRIX)
        assert not dlg._mcp_empty.isHidden()
        dlg.deleteLater()

    def test_toggle_cell_marks_dirty_and_save_persists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shared_dev_tools, "list_master_mcps", lambda: ["playwright"])
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_MCP_MATRIX)
        toggle = dlg._mcp_toggles["backend"]["playwright"]
        assert toggle.isChecked() is False
        toggle.setChecked(True)
        assert dlg._dirty is True

        dlg._on_save_apply_clicked()

        assert pane_tools_policy.effective_mcps("backend") == frozenset({"playwright"})
        dlg.deleteLater()


class TestPluginsMatrixView:
    def test_denylist_banner_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from PyQt6.QtWidgets import QLabel

        monkeypatch.setattr(settings_window.pane_tools_dialog, "discover_marketplaces", lambda: [])
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PLUGINS_MATRIX)
        view = dlg._stack.widget(settings_window.VIEW_PLUGINS_MATRIX).widget()
        banner_texts = [
            lbl.text() for lbl in view.findChildren(QLabel) if lbl.objectName() == "infoBanner"
        ]
        assert any("denylist" in t for t in banner_texts)
        dlg.deleteLater()

    def test_grid_has_a_toggle_per_role_per_marketplace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            settings_window.pane_tools_dialog, "discover_marketplaces", lambda: ["pordee"]
        )
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PLUGINS_MATRIX)
        assert set(dlg._plugin_toggles.keys()) == set(settings_window._matrix_roles())
        for items in dlg._plugin_toggles.values():
            assert set(items.keys()) == {"pordee"}
        assert dlg._plugins_empty.isHidden()
        dlg.deleteLater()

    def test_toggle_cell_marks_dirty_and_save_persists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "backend" has no built-in plugin-policy override (falls back to
        # _TEAMMATE_PLUGINS, which does NOT include ui-ux-pro-max-skill — a
        # design-only marketplace), so this cell starts unchecked, unlike
        # e.g. "pordee" which every teammate gets by default.
        monkeypatch.setattr(
            settings_window.pane_tools_dialog,
            "discover_marketplaces",
            lambda: ["ui-ux-pro-max-skill"],
        )
        monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PLUGINS_MATRIX)
        toggle = dlg._plugin_toggles["backend"]["ui-ux-pro-max-skill"]
        assert toggle.isChecked() is False
        toggle.setChecked(True)
        assert dlg._dirty is True

        dlg._on_save_apply_clicked()

        # The role's built-in defaults not rendered as a column here (this
        # machine's marketplace list) are preserved via _hidden_plugin_defaults
        # (see settings_window._reload_plugins_matrix's own note) — Save adds
        # the newly-checked column on TOP of them, it doesn't replace them.
        assert pane_tools_policy.effective_plugins("backend") == frozenset(
            {"ui-ux-pro-max-skill", "superpowers-dev", "pordee", "claude-plugins-official"}
        )
        dlg.deleteLater()


class TestRoleOverlapView:
    """The renamed old "Skill Catalog" — a ROLE-scope TF-IDF overlap audit,
    not a skill browser (2026-07-11 rename)."""

    def test_selecting_role_updates_detail_and_overlap_badge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        docs = {"backend": "database schema api endpoint", "frontend": "react component css"}
        monkeypatch.setattr(settings_window.skill_audit, "load_all_role_docs", lambda: docs)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_ROLE_OVERLAP)
        assert dlg._overlap_list.count() == 2

        row = next(
            i
            for i in range(dlg._overlap_list.count())
            if dlg._overlap_list.item(i).data(Qt.ItemDataRole.UserRole) == "backend"
        )
        dlg._overlap_list.setCurrentRow(row)
        assert dlg._overlap_detail_text.toPlainText() == docs["backend"]
        assert dlg._overlap_badge.text().startswith("✓")
        dlg.deleteLater()


class TestSkillCatalogView:
    """The new, real skill browser backed by skill_scan (SKILL section)."""

    def test_lists_scanned_skills_with_desc_and_referencing_roles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import skill_scan

        skills = [
            skill_scan.SkillInfo(
                name="cockpit-ui-style",
                description="design system for the cockpit UI",
                path=Path("/x/.claude/skills/cockpit-ui-style/SKILL.md"),
            ),
            skill_scan.SkillInfo(
                name="debug-mantra", description="debugging discipline", path=Path("/x/db.md")
            ),
        ]
        monkeypatch.setattr(settings_window.skill_scan, "scan_skills", lambda _roots: list(skills))
        monkeypatch.setattr(
            settings_window.skill_audit,
            "load_all_role_docs",
            lambda: {"frontend": "must read cockpit-ui-style before UI work", "qa": "run tests"},
        )
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_SKILL_CATALOG)
        assert dlg._catalog_list.count() == 2

        row = next(
            i
            for i in range(dlg._catalog_list.count())
            if dlg._catalog_list.item(i).data(Qt.ItemDataRole.UserRole) == "cockpit-ui-style"
        )
        dlg._catalog_list.setCurrentRow(row)
        assert dlg._catalog_name.text() == "cockpit-ui-style"
        assert "design system" in dlg._catalog_desc.text()
        # frontend's doc mentions the skill name → surfaced as a referencing role
        assert "Frontend" in dlg._catalog_roles.text()
        dlg.deleteLater()

    def test_short_skill_name_does_not_false_match_on_prose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A short/common skill name must reference the skill as a whole word —
        it must NOT surface a role just because its letters appear inside an
        unrelated word (raw substring: ``"git" in "github"`` → True)."""
        from agent_takkub import skill_scan

        skills = [
            skill_scan.SkillInfo(name="git", description="git workflow", path=Path("/x/git.md")),
        ]
        monkeypatch.setattr(settings_window.skill_scan, "scan_skills", lambda _roots: list(skills))
        monkeypatch.setattr(
            settings_window.skill_audit,
            "load_all_role_docs",
            lambda: {
                # substring "git" is present (github / digital) but never as a
                # standalone word → must NOT count as referencing the skill
                "backend": "push to github and deploy the digital dashboard",
                # whole-word reference → SHOULD count
                "devops": "อ่าน skill: git ก่อนเริ่มงานที่เกี่ยวข้อง",
            },
        )
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_SKILL_CATALOG)
        row = next(
            i
            for i in range(dlg._catalog_list.count())
            if dlg._catalog_list.item(i).data(Qt.ItemDataRole.UserRole) == "git"
        )
        dlg._catalog_list.setCurrentRow(row)
        text = dlg._catalog_roles.text()
        assert "DevOps" in text  # whole-word "git" reference surfaces
        assert "Backend" not in text  # github/digital substring must not
        dlg.deleteLater()

    def test_empty_catalog_shows_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings_window.skill_scan, "scan_skills", lambda _roots: [])
        monkeypatch.setattr(settings_window.skill_audit, "load_all_role_docs", lambda: {})
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_SKILL_CATALOG)
        assert dlg._catalog_list.count() == 0
        assert "ไม่พบ skill" in dlg._catalog_name.text()
        dlg.deleteLater()


class TestPipelineBuilderView:
    def test_hops_render_for_active_template(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        assert dlg._pb_hops_lay.count() > 0
        dlg.deleteLater()

    def test_palette_click_appends_a_solo_hop(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        before = len(dlg._pb_hops)
        dlg._on_palette_role_clicked("backend")
        assert len(dlg._pb_hops) == before + 1
        assert dlg._pb_hops[-1] == [
            {"role": "backend", "cwd": "", "requiresCommit": False, "autoChain": False}
        ]
        assert dlg._dirty is True
        dlg.deleteLater()

    def test_remove_hop_shrinks_list(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        dlg._on_palette_role_clicked("backend")
        n = len(dlg._pb_hops)
        dlg._on_remove_hop_clicked(n - 1)
        assert len(dlg._pb_hops) == n - 1
        dlg.deleteLater()

    def test_save_apply_persists_staged_hop_edit(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        template_id = dlg._pb_template_id
        dlg._on_palette_role_clicked("backend")
        expected_len = len(dlg._pb_hops)

        dlg._on_save_apply_clicked()

        payload = pipeline_config.load(None)
        tpl = next(t for t in payload["templates"] if t["id"] == template_id)
        assert len(tpl["hops"]) == expected_len
        dlg.deleteLater()


class TestSaveApplyAtomicity:
    def test_failed_tools_policy_write_rolls_back_provider_and_pipeline_writes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex High #3 — Save & Apply writes 3 separate JSON stores
        (role-providers, pipelines, pane-tools policy) in sequence; a
        failure in the LAST stage must not leave the first two committed
        (previously each store wrote through independently with no shared
        transaction, so a late failure left an inconsistent, half-applied
        state and still reported "Save failed" as if nothing landed)."""
        monkeypatch.setattr(shared_dev_tools, "list_master_mcps", lambda: ["playwright"])
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        dlg._role_toggles["qa"].setChecked(False)
        combo = dlg._role_provider_combos["backend"]
        combo.setCurrentIndex(combo.findData("codex"))
        dlg._goto_view(settings_window.VIEW_MCP_MATRIX)
        dlg._mcp_toggles["backend"]["playwright"].setChecked(True)

        monkeypatch.setattr(pane_tools_policy, "set_role_items", lambda *a, **k: False)

        dlg._on_save_apply_clicked()

        assert provider_config.load_providers().get("backend") != "codex"
        assert pipeline_config.load(None)["rolesEnabled"].get("qa", True) is True
        assert dlg.result() != QDialog.DialogCode.Accepted
        assert dlg._dirty is True
        dlg.deleteLater()


class TestTemplatesView:
    def test_builtin_template_listed_and_delete_disabled(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_TEMPLATES)
        assert dlg._tpl_list.count() >= 1
        assert dlg._tpl_delete_btn.isEnabled() is False  # first row is builtin
        dlg.deleteLater()

    def test_duplicate_creates_non_builtin_copy(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_TEMPLATES)
        before = len(dlg._pipeline_payload["templates"])
        dlg._on_template_duplicate_clicked()

        assert len(dlg._pipeline_payload["templates"]) == before + 1
        payload = pipeline_config.load(None)
        assert len(payload["templates"]) == before + 1
        new_tpl = payload["templates"][-1]
        assert new_tpl["builtin"] is False
        dlg.deleteLater()

    def test_delete_removes_duplicated_template(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_TEMPLATES)
        dlg._on_template_duplicate_clicked()
        dlg._reload_templates_list()
        dlg._tpl_list.setCurrentRow(dlg._tpl_list.count() - 1)
        before = len(dlg._pipeline_payload["templates"])

        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
        dlg._on_template_delete_clicked()

        assert len(dlg._pipeline_payload["templates"]) == before - 1
        dlg.deleteLater()

    def test_edit_hops_switches_to_pipeline_builder_view(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_TEMPLATES)
        dlg._tpl_list.setCurrentRow(0)
        dlg._on_template_edit_hops_clicked()
        assert dlg._stack.currentIndex() == settings_window.VIEW_PIPELINE_BUILDER
        dlg.deleteLater()

    def test_long_template_name_is_elided_not_hard_clipped(self) -> None:
        """Critic #2026-07-10 v2 regression — 'Feature (UI+API)' rendered as
        'Feature (UI+AP' (clipped mid-glyph, no ellipsis) because the
        fixed-width BUILT-IN chip left too little room for the label."""
        from PyQt6.QtGui import QFontMetrics

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_TEMPLATES)
        metrics = QFontMetrics(dlg._tpl_list.font())
        long_name = "A Very Long Template Name That Cannot Possibly Fit (UI+API)"
        elided = dlg._elide_template_name(metrics, long_name, avail_width=60)
        assert elided != long_name
        assert elided.endswith("…")  # real ellipsis, not a mid-word hard clip
        assert long_name.startswith(elided[:-1])
        dlg.deleteLater()

    def test_short_template_name_not_elided(self) -> None:
        from PyQt6.QtGui import QFontMetrics

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_TEMPLATES)
        metrics = QFontMetrics(dlg._tpl_list.font())
        short_name = "Blank"
        elided = dlg._elide_template_name(metrics, short_name, avail_width=500)
        assert elided == short_name
        dlg.deleteLater()

    def test_compact_chip_width_reserves_space_for_builtin_badge(self) -> None:
        from PyQt6.QtGui import QFontMetrics

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_TEMPLATES)
        metrics = QFontMetrics(dlg._tpl_list.font())
        width = dlg._compact_chip_width(metrics, "BUILT-IN")
        assert width > metrics.horizontalAdvance("BUILT-IN")
        dlg.deleteLater()

    def test_builtin_row_label_carries_full_name_as_tooltip(self) -> None:
        """Even when elided, the full name must stay reachable (tooltip) —
        eliding must not be a silent data loss."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_TEMPLATES)
        first_tpl = dlg._pipeline_payload["templates"][0]
        row_widget = dlg._tpl_list.itemWidget(dlg._tpl_list.item(0))
        name_label = row_widget.layout().itemAt(0).widget()
        assert name_label.toolTip() == first_tpl["name"]
        dlg.deleteLater()


class TestUsersView:
    """2026-07-11 — Users tab (#8), ported from the old standalone
    open_user_profiles_dialog modal QDialog. Covers the task spec's tofu
    checklist: nav item present + clickable, widgets present, real profile
    list render, and the config-persist wiring (add/remove profile,
    Claude Auth save) — mirrors TestNewRoleView's pattern for a non-matrix
    "list ธรรมดา" view."""

    def test_users_nav_item_present_and_clickable(self) -> None:
        dlg = settings_window.SettingsWindow()
        assert settings_window.VIEW_USERS in dlg._nav_buttons
        dlg._nav_buttons[settings_window.VIEW_USERS].click()
        assert dlg._stack.currentIndex() == settings_window.VIEW_USERS
        assert dlg._content_title.text() == "Users"
        dlg.deleteLater()

    def test_profiles_tab_renders_real_profile_list(self, tmp_path: Path) -> None:
        user_profile.add_profile("work", str(tmp_path / "work-cfg"), share_sessions=False)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        assert dlg._up_profile_list.count() == 2
        assert dlg._up_auth_combo.count() == 2
        assert "work" in dlg._up_profile_list.item(1).text()
        dlg.deleteLater()

    def test_remove_and_share_disabled_for_default_row(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        dlg._up_profile_list.setCurrentRow(0)
        assert dlg._up_remove_btn.isEnabled() is False
        assert dlg._up_share_btn.isEnabled() is False
        dlg.deleteLater()

    def test_add_profile_persists_and_updates_both_tabs(self, tmp_path: Path) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        dlg._up_add_name.setText("work")
        dlg._up_add_dir.setText(str(tmp_path / "work-cfg"))
        dlg._up_add_share_chk.setChecked(False)  # isolated — skip junction provisioning

        dlg._on_users_add_profile_clicked()

        assert any(p["name"] == "work" for p in user_profile.list_profiles())
        assert dlg._up_profile_list.count() == 2
        assert dlg._up_auth_combo.count() == 2
        assert dlg._up_add_name.text() == ""  # form clears on success
        dlg.deleteLater()

    def test_invalid_profile_name_rejected_without_creating(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        dlg._up_add_name.setText("default")  # reserved name
        dlg._up_add_dir.setText("whatever")

        dlg._on_users_add_profile_clicked()

        assert dlg._up_profile_list.count() == 1  # unchanged — still just default
        dlg.deleteLater()

    def test_remove_profile_persists_and_updates_auth_combo(self, tmp_path: Path) -> None:
        user_profile.add_profile("work", str(tmp_path / "work-cfg"), share_sessions=False)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        row = next(
            i
            for i in range(dlg._up_profile_list.count())
            if "work" in dlg._up_profile_list.item(i).text()
        )
        dlg._up_profile_list.setCurrentRow(row)

        dlg._on_users_remove_profile_clicked()

        assert not any(p["name"] == "work" for p in user_profile.list_profiles())
        assert dlg._up_profile_list.count() == 1
        assert dlg._up_auth_combo.count() == 1
        dlg.deleteLater()

    def test_claude_auth_save_persists_per_profile(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        dlg._up_base_url.setText("https://api.deepseek.com/anthropic")
        dlg._up_api_key.setText("sk-test")

        dlg._on_users_save_auth_clicked()

        saved = claude_auth_config.load_claude_auth(dlg._users_auth_dir("default"))
        assert saved.base_url == "https://api.deepseek.com/anthropic"
        assert saved.api_key == "sk-test"
        assert "Claude auth saved" in dlg._up_status.text()
        dlg.deleteLater()

    def test_env_var_row_save_persists_extra_env(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        # _load_users_auth_profile always seeds one blank row on open.
        assert len(dlg._up_env_rows) == 1
        name_edit, value_edit, _row = dlg._up_env_rows[0]
        name_edit.setText("ANTHROPIC_DEFAULT_SONNET_MODEL")
        value_edit.setText("qwen/qwen3-coder:free")

        dlg._on_users_save_auth_clicked()

        saved = claude_auth_config.load_claude_auth(dlg._users_auth_dir("default"))
        assert saved.extra_env == {"ANTHROPIC_DEFAULT_SONNET_MODEL": "qwen/qwen3-coder:free"}
        dlg.deleteLater()

    def test_switching_auth_profile_reloads_fields(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "work-cfg"
        user_profile.add_profile("work", str(work_dir), share_sessions=False)
        claude_auth_config.save_claude_auth(
            claude_auth_config.ClaudeAuthConfig(base_url="https://openrouter.ai/api"), work_dir
        )
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        assert dlg._up_base_url.text() == ""  # default profile has no override

        idx = dlg._up_auth_combo.findText("work")
        dlg._up_auth_combo.setCurrentIndex(idx)

        assert dlg._up_base_url.text() == "https://openrouter.ai/api"
        dlg.deleteLater()
