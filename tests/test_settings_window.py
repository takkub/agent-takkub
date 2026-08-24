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
    performance_settings,
    pipeline_config,
    project_nav,
    provider_config,
    provider_state,
    role_models,
    settings_window,
    shared_dev_tools,
    skill_policy,
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
    monkeypatch.setattr(skill_policy, "SKILL_POLICY_FILE", tmp_path / "skill-policy.json")
    monkeypatch.setattr(shared_dev_tools, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
    # Users view (VIEW_USERS) touches user_profile's registry on every
    # SettingsWindow() construction (list_profiles() is called eagerly to
    # build the Profiles/Claude Auth tabs) — isolate it like every other
    # store above so tests never read/write the real ~/.takkub registry.
    monkeypatch.setattr(user_profile, "_REGISTRY_PATH", tmp_path / "user-profiles.json")
    monkeypatch.setattr(user_profile, "_DEFAULT_CONFIG_DIR", tmp_path / "default-claude-config")
    # Providers & Roles' per-role model/effort combos write through
    # role_models.set_model/set_effort on every Save & Apply — isolate like
    # every other store above so a test never touches the real
    # ~/.takkub/role-models.json.
    monkeypatch.setattr(role_models, "_PATH", tmp_path / "role-models.json")
    monkeypatch.setattr(performance_settings, "path", lambda: tmp_path / "performance.json")
    # Core V2 views (VIEW_CORE_V2_*, epic #309 Phase 9) build unconditionally
    # in _build_content — every SettingsWindow() construction now touches
    # core_v2_settings' file (under config.SETTINGS_HOME) and every
    # core.accounts/brain/versioning store (under config.RUNTIME_DIR), same
    # isolation pattern test_core_brain_adapter.py uses for RUNTIME_DIR.
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    saved = dict(roles_mod._CUSTOM)
    roles_mod._CUSTOM.clear()
    yield
    roles_mod._CUSTOM.clear()
    roles_mod._CUSTOM.update(saved)


class TestSettingsWindowStructure:
    def test_has_twenty_one_stacked_views(self) -> None:
        # 10 stable views + Performance (10) + 6 Core V2 views (11-16,
        # epic #309 Phase 9) + 4 Knowledge & Design views (17-20, final
        # closeout pack 2).
        dlg = settings_window.SettingsWindow()
        assert dlg._stack.count() == 21
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

    def test_performance_preset_persists_and_requests_live_reload(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PERFORMANCE)
        dlg._performance_mode.setCurrentIndex(dlg._performance_mode.findData("safe"))
        assert settings_window.VIEW_PERFORMANCE in dlg._dirty_views
        dlg._on_save_apply_clicked()
        assert dlg.pending_performance_reload is True
        saved = performance_settings.load()
        assert saved.mode == "safe"
        assert saved.max_heavy_global == 2


class TestNewRoleView:
    def test_create_role_persists_and_registers_live(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        dlg._nr_name.setText("data-eng")
        dlg._nr_label.setText("Data Eng")
        dlg._nr_instructions.setPlainText("do data things")
        dlg._on_create_role_clicked()

        assert "data-eng" in custom_roles.load_custom_roles()
        assert roles_mod.by_name("data-eng") is not None
        assert dlg._nr_status.text().startswith("OK:")
        # Form resets on success (status message is deliberately kept).
        assert dlg._nr_name.text() == ""
        dlg.deleteLater()

    def test_reserved_name_rejected_without_creating(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        dlg._nr_name.setText("lead")
        dlg._on_create_role_clicked()

        assert "lead" not in custom_roles.load_custom_roles()
        assert dlg._nr_status.text().startswith("!")
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
        assert dlg._nr_status.text().startswith("!")
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

    @pytest.fixture(autouse=True)
    def _isolate_skill_roots(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`_new_role_skill_roots` also falls back to `config.ASSETS_ROOT` (the
        installed-build read path for the shipped default skill bundle) — on
        this dev checkout that's the real worktree root, which has real
        `.claude/skills/*`. Pin both roots to tmp_path so these tests stay
        isolated from the repo's actual skill bundle."""
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(config, "ASSETS_ROOT", tmp_path)

    def test_checkbox_list_populated_from_scanned_skills(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write_skill(tmp_path, "test-skill", "does a thing")
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        assert [s.name for s, _chk in dlg._nr_skill_checks] == ["test-skill"]
        dlg.deleteLater()

    def test_no_skills_dir_shows_empty_list_without_crashing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        assert dlg._nr_skill_checks == []
        dlg.deleteLater()

    def test_selected_skill_embedded_into_default_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#4 in the task spec — an empty Instructions box still gets the
        skill reference embedded into the generated default template."""
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
        self._write_skill(tmp_path, "test-skill", "does a thing")
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        dlg._nr_name.setText("data-eng")
        dlg._nr_instructions.setPlainText("custom instructions here")

        assert dlg._on_create_role_clicked() is True

        role_file = custom_roles.CUSTOM_AGENTS_DIR / "data-eng.md"
        text = role_file.read_text(encoding="utf-8")
        assert "test-skill" not in text
        dlg.deleteLater()

    def test_assets_root_fallback_finds_shipped_skill_bundle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Installed-build regression guard: on a pip/npm build `REPO_ROOT`
        resolves to an empty venv ancestor, but `ASSETS_ROOT` (the staged
        wheel data) has the shipped default skill bundle — the picker must
        still find it via that fallback."""
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "empty-venv-ancestor")
        assets_root = tmp_path / "assets"
        monkeypatch.setattr(config, "ASSETS_ROOT", assets_root)
        self._write_skill(assets_root, "bundled-skill", "ships in the wheel")

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        assert [s.name for s, _chk in dlg._nr_skill_checks] == ["bundled-skill"]
        dlg.deleteLater()


class TestProvidersRolesView:
    def test_bulk_provider_control_updates_every_role_and_marks_dirty(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        bulk = dlg._bulk_role_provider_combo
        bulk.setCurrentIndex(bulk.findData("codex"))

        assert dlg._bulk_role_provider_btn.isEnabled() is True
        dlg._bulk_role_provider_btn.click()

        assert all(combo.currentData() == "codex" for combo in dlg._role_provider_combos.values())
        assert dlg._dirty is True
        assert dlg._save_btn.isEnabled() is True
        # Lead is part of "all roles", so its existing capability warning
        # must update through the same signal path as a manual row edit.
        assert dlg._lead_warning_lbl.isHidden() is False
        dlg.deleteLater()

    def test_bulk_provider_change_saves_all_rendered_roles(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        bulk = dlg._bulk_role_provider_combo
        bulk.setCurrentIndex(bulk.findData("codex"))
        dlg._bulk_role_provider_btn.click()
        roles = tuple(dlg._role_provider_combos)

        dlg._on_save_apply_clicked()

        assert all(provider_config.provider_for(role) == "codex" for role in roles)
        dlg.deleteLater()

    def test_reset_reverts_bulk_provider_change_and_clears_picker(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        bulk = dlg._bulk_role_provider_combo
        bulk.setCurrentIndex(bulk.findData("codex"))
        dlg._bulk_role_provider_btn.click()

        dlg._on_reset_clicked()

        assert all(
            combo.currentData() == provider_config.CLAUDE
            for combo in dlg._role_provider_combos.values()
        )
        assert bulk.currentIndex() == -1
        assert dlg._bulk_role_provider_btn.isEnabled() is False
        assert dlg._dirty is False
        dlg.deleteLater()

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

    def test_lead_row_is_unlocked_but_has_no_pipeline_toggle(self) -> None:
        # Issue #101: Lead's CLI is no longer forced to claude, so it now
        # gets a provider combo like any other role — but it's still not a
        # dev-pipeline participant, so no enable/disable toggle for it.
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        assert "lead" not in dlg._role_toggles
        assert "lead" in dlg._role_provider_combos
        assert dlg._role_provider_combos["lead"].currentData() == "claude"
        assert dlg._lead_warning_lbl is not None
        # Offscreen tests never `.show()` the dialog, so `isVisible()` always
        # reads False regardless of state — `isHidden()` reflects the
        # widget's own `setVisible()` call (same pattern as the substitute
        # badge test below).
        assert dlg._lead_warning_lbl.isHidden() is True
        dlg.deleteLater()

    def test_lead_warning_shows_when_switched_off_claude(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        combo = dlg._role_provider_combos["lead"]
        combo.setCurrentIndex(combo.findData("codex"))
        assert dlg._lead_warning_lbl.isHidden() is False
        dlg.deleteLater()

    def test_lead_provider_override_saves(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        combo = dlg._role_provider_combos["lead"]
        combo.setCurrentIndex(combo.findData("codex"))
        dlg._on_save_apply_clicked()
        assert provider_config.provider_for("lead") == "codex"
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
            isinstance(w, QPushButton) and w.text() == "x" for w in row.findChildren(QPushButton)
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
        delete_btn = next(w for w in row.findChildren(QPushButton) if w.text() == "x")

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
        delete_btn = next(w for w in row.findChildren(QPushButton) if w.text() == "x")

        delete_btn.click()

        assert "data-eng" in custom_roles.load_custom_roles()
        assert "data-eng" in dlg._role_toggles
        dlg.deleteLater()


class TestRoleEffortCombo:
    """Per-role reasoning-effort override (#136 follow-up): a combo next to
    the model picker, gated by provider_spec.effort_levels_for and
    repopulated whenever provider or model changes."""

    def test_default_provider_offers_claude_effort_levels(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        combo = dlg._role_effort_combos["backend"]
        assert combo.isEnabled() is True
        levels = [combo.itemData(i) for i in range(combo.count())]
        assert levels == ["", "low", "medium", "high", "xhigh", "max"]
        assert combo.currentData() == ""  # nothing saved yet -> "(default)"
        dlg.deleteLater()

    def test_switching_provider_to_unsupported_disables_combo(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        provider_combo = dlg._role_provider_combos["backend"]
        effort_combo = dlg._role_effort_combos["backend"]

        # opencode has effort_flag=None (#103 documented gap) — gemini/agy
        # regained --effort in #323, so it no longer belongs in this case.
        provider_combo.setCurrentIndex(provider_combo.findData("opencode"))

        assert effort_combo.isEnabled() is False
        dlg.deleteLater()

    def test_gemini_offers_effort_levels(self) -> None:
        """#323: gemini/agy regained --effort (upstream #125 fix, agy 1.1.10+)
        after #103 had marked it unsupported — the combo must re-enable with
        agy's own low/medium/high levels, not claude's five-level scale."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        provider_combo = dlg._role_provider_combos["backend"]
        effort_combo = dlg._role_effort_combos["backend"]

        provider_combo.setCurrentIndex(provider_combo.findData("gemini"))

        assert effort_combo.isEnabled() is True
        levels = [effort_combo.itemData(i) for i in range(effort_combo.count())]
        assert levels == ["", "low", "medium", "high"]
        dlg.deleteLater()

    def test_switching_model_to_haiku_disables_and_keeps_prior_selection(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        model_combo = dlg._role_model_combos["backend"]
        effort_combo = dlg._role_effort_combos["backend"]
        effort_combo.setCurrentIndex(effort_combo.findData("high"))
        assert effort_combo.currentData() == "high"

        model_combo.setCurrentText("claude-haiku-4-5")

        assert effort_combo.isEnabled() is False
        # State preserved for display, not silently reset to "(default)".
        assert effort_combo.currentData() == "high"
        dlg.deleteLater()

    def test_switching_provider_back_re_enables_and_restores_levels(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        provider_combo = dlg._role_provider_combos["backend"]
        effort_combo = dlg._role_effort_combos["backend"]

        provider_combo.setCurrentIndex(provider_combo.findData("opencode"))
        assert effort_combo.isEnabled() is False
        provider_combo.setCurrentIndex(provider_combo.findData("claude"))

        assert effort_combo.isEnabled() is True
        levels = [effort_combo.itemData(i) for i in range(effort_combo.count())]
        assert levels == ["", "low", "medium", "high", "xhigh", "max"]
        dlg.deleteLater()

    def test_save_apply_persists_effort_selection(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        effort_combo = dlg._role_effort_combos["backend"]
        effort_combo.setCurrentIndex(effort_combo.findData("xhigh"))

        dlg._on_save_apply_clicked()

        assert role_models.effort_for("backend", "claude") == "xhigh"
        dlg.deleteLater()

    def test_save_apply_drops_stale_effort_for_unsupported_model_and_notifies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        role_models.set_model("backend", "claude", "claude-sonnet-5")
        role_models.set_effort("backend", "claude", "high")

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        # Combo was populated from disk with "high" selected; now the user
        # switches the model under it to one that can't take --effort at all,
        # without touching the effort combo itself.
        dlg._role_model_combos["backend"].setCurrentText("claude-haiku-4-5")
        assert dlg._role_effort_combos["backend"].isEnabled() is False

        notices: list[tuple] = []
        monkeypatch.setattr(
            QMessageBox,
            "information",
            lambda *a, **k: notices.append(a) or QMessageBox.StandardButton.Ok,
        )

        dlg._on_save_apply_clicked()

        assert role_models.effort_for("backend", "claude") is None
        assert len(notices) == 1
        assert "backend" in notices[0][2]  # (self, title, text) — text mentions the role
        dlg.deleteLater()

    def test_save_apply_no_notice_when_nothing_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        notices: list[tuple] = []
        monkeypatch.setattr(
            QMessageBox,
            "information",
            lambda *a, **k: notices.append(a) or QMessageBox.StandardButton.Ok,
        )

        dlg._on_save_apply_clicked()

        assert notices == []
        dlg.deleteLater()

    def test_reset_restores_effort_combo_from_disk(self) -> None:
        role_models.set_model("backend", "claude", "claude-sonnet-5")
        role_models.set_effort("backend", "claude", "medium")
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        effort_combo = dlg._role_effort_combos["backend"]
        effort_combo.setCurrentIndex(effort_combo.findData("max"))

        dlg._on_reset_clicked()

        assert effort_combo.currentData() == "medium"
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


class TestSkillMatrixView:
    """Role × skill toggle grid (#103 phase 4) — persists to skill_policy,
    NOT pane_tools_policy. Unlike MCP/Plugins Matrix, codex and gemini get
    rows here (skill_policy.skill_matrix_roles(), not
    settings_window._matrix_roles())."""

    def _fake_skills(self, *names: str) -> list:
        from agent_takkub import skill_scan

        return [skill_scan.SkillInfo(name=n, description=f"{n} desc", path=Path(n)) for n in names]

    def test_grid_has_a_toggle_per_role_per_skill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            settings_window.skill_scan,
            "scan_skills",
            lambda roots: self._fake_skills("debug-mantra", "verify"),
        )
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_SKILL_MATRIX)
        assert set(dlg._skill_toggles.keys()) == set(skill_policy.skill_matrix_roles())
        for items in dlg._skill_toggles.values():
            assert set(items.keys()) == {"debug-mantra", "verify"}
        assert "codex" in dlg._skill_toggles
        assert "gemini" in dlg._skill_toggles
        assert "shell" not in dlg._skill_toggles
        assert dlg._skill_matrix_empty.isHidden()
        dlg.deleteLater()

    def test_empty_catalog_shows_empty_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings_window.skill_scan, "scan_skills", lambda roots: [])
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_SKILL_MATRIX)
        assert not dlg._skill_matrix_empty.isHidden()
        dlg.deleteLater()

    def test_toggle_cell_marks_dirty_and_save_persists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            settings_window.skill_scan,
            "scan_skills",
            lambda roots: self._fake_skills("debug-mantra"),
        )
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_SKILL_MATRIX)
        toggle = dlg._skill_toggles["backend"]["debug-mantra"]
        assert toggle.isChecked() is False
        toggle.setChecked(True)
        assert dlg._dirty is True

        dlg._on_save_apply_clicked()

        assert skill_policy.effective_skills("backend") == ["debug-mantra"]
        dlg.deleteLater()

    def test_reset_reverts_unsaved_toggle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            settings_window.skill_scan,
            "scan_skills",
            lambda roots: self._fake_skills("debug-mantra"),
        )
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_SKILL_MATRIX)
        dlg._skill_toggles["backend"]["debug-mantra"].setChecked(True)
        dlg._mark_dirty()
        dlg._on_reset_clicked()
        assert dlg._skill_toggles["backend"]["debug-mantra"].isChecked() is False
        assert skill_policy.effective_skills("backend") == []
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
        assert dlg._overlap_badge.text().startswith("OK:")
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


class TestNewSkillForm:
    """+ New Skill / delete — closes the create+delete half of the Skill
    Catalog lifecycle loop (list/select already existed)."""

    @pytest.fixture(autouse=True)
    def _isolate_skill_roots(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Keep the bundled cockpit-checkout scan roots empty/isolated so
        # only the fake "active project" root (tmp_path) has skills.
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "no-bundle-here")
        monkeypatch.setattr(config, "ASSETS_ROOT", tmp_path / "no-bundle-here")
        monkeypatch.setattr(settings_window, "_allowed_project_roots", lambda _project: [tmp_path])
        # Route central skill storage (create_skill writes here + junctions
        # back into tmp_path/.claude/skills) at a throwaway dir so tests never
        # write into the real ~/.agent-takkub / repo project-skills.
        monkeypatch.setattr(config, "PROJECT_SKILLS_HOME", tmp_path / "central-skills")

    def test_create_writes_file_and_refreshes_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._ns_name.setText("my-new-skill")
        dlg._ns_desc.setText("does a thing")
        dlg._ns_instructions.setPlainText("body content")
        dlg._on_create_skill_clicked()

        assert (tmp_path / ".claude" / "skills" / "my-new-skill" / "SKILL.md").is_file()
        assert dlg._ns_status.text().startswith("OK:")
        assert dlg._ns_name.text() == ""
        assert "my-new-skill" in {s.name for s in dlg._catalog_skills}
        dlg.deleteLater()

    def test_create_without_active_project_shows_warning(self, tmp_path: Path) -> None:
        dlg = settings_window.SettingsWindow(
            project=None, initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._ns_name.setText("orphan-skill")
        dlg._on_create_skill_clicked()

        assert dlg._ns_status.text().startswith("!")
        assert not (tmp_path / ".claude" / "skills" / "orphan-skill").exists()
        dlg.deleteLater()

    def test_invalid_name_rejected(self, tmp_path: Path) -> None:
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._ns_name.setText("../escape")
        dlg._on_create_skill_clicked()

        assert dlg._ns_status.text().startswith("!")
        assert not (tmp_path / ".claude" / "skills").exists()
        dlg.deleteLater()

    def test_duplicate_name_rejected(self, tmp_path: Path) -> None:
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._ns_name.setText("dup-skill")
        dlg._on_create_skill_clicked()
        dlg._ns_name.setText("dup-skill")
        dlg._on_create_skill_clicked()

        assert dlg._ns_status.text().startswith("!")
        dlg.deleteLater()

    def test_created_skill_shows_delete_button(self, tmp_path: Path) -> None:
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._ns_name.setText("deletable-skill")
        dlg._on_create_skill_clicked()

        assert dlg._catalog_delete_btn.isHidden() is False
        dlg.deleteLater()

    def test_bundled_skill_has_no_delete_button(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import skill_scan

        bundled = skill_scan.SkillInfo(
            name="bundled",
            description="ships with cockpit",
            path=config.REPO_ROOT / ".claude" / "skills" / "bundled" / "SKILL.md",
        )
        monkeypatch.setattr(settings_window.skill_scan, "scan_skills", lambda _roots: [bundled])
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        assert dlg._catalog_delete_btn.isHidden() is True
        dlg.deleteLater()

    def test_delete_confirmed_removes_skill_and_refreshes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._ns_name.setText("goner")
        dlg._on_create_skill_clicked()
        assert (tmp_path / ".claude" / "skills" / "goner").is_dir()

        monkeypatch.setattr(
            settings_window.QMessageBox,
            "question",
            staticmethod(lambda *a, **k: settings_window.QMessageBox.StandardButton.Yes),
        )
        dlg._on_delete_skill_clicked()

        assert not (tmp_path / ".claude" / "skills" / "goner").exists()
        assert "goner" not in {s.name for s in dlg._catalog_skills}
        dlg.deleteLater()

    def test_delete_declined_keeps_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._ns_name.setText("keeper")
        dlg._on_create_skill_clicked()

        monkeypatch.setattr(
            settings_window.QMessageBox,
            "question",
            staticmethod(lambda *a, **k: settings_window.QMessageBox.StandardButton.No),
        )
        dlg._on_delete_skill_clicked()

        assert (tmp_path / ".claude" / "skills" / "keeper").is_dir()
        dlg.deleteLater()

    def test_created_skill_appears_in_new_role_picker(self, tmp_path: Path) -> None:
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._ns_name.setText("picker-visible")
        dlg._on_create_skill_clicked()

        assert "picker-visible" in {s.name for s, _chk in dlg._nr_skill_checks}
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


class TestSkillDescriptionClamp:
    """New Role picker's description clamp (design critique #1 —
    docs/design/2026-08-13-new-role-critique.md)."""

    def test_short_description_unchanged(self) -> None:
        assert settings_window._clamp_skill_description("does a thing") == "does a thing"

    def test_long_description_truncated_with_ellipsis(self) -> None:
        long_desc = "x" * 200
        clamped = settings_window._clamp_skill_description(long_desc)
        assert len(clamped) <= settings_window._SKILL_DESC_CLAMP_CHARS
        assert clamped.endswith("…")

    def test_row_sets_full_text_as_tooltip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "no-bundle-here")
        monkeypatch.setattr(config, "ASSETS_ROOT", tmp_path / "no-bundle-here")
        monkeypatch.setattr(settings_window, "_allowed_project_roots", lambda _project: [tmp_path])
        long_desc = "y" * 200
        d = tmp_path / ".claude" / "skills" / "verbose-skill"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: verbose-skill\ndescription: {long_desc}\n---\n\nbody\n", encoding="utf-8"
        )
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_NEW_ROLE
        )
        _skill, chk = dlg._nr_skill_checks[0]
        desc_label = chk.parentWidget().findChildren(settings_window.QLabel)[-1]
        assert desc_label.toolTip() == long_desc
        assert desc_label.text() != long_desc
        dlg.deleteLater()

    def test_skills_container_does_not_widen_with_long_descriptions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard for docs/audit/2026-08-13-new-role-redesign.md
        finding #1: packing `f"{name} — {description}"` into a single
        unwrapped QCheckBox blew the skills container's sizeHint().width()
        out to 2405px (measured against a debug-mantra-length description),
        pushing the Label field and MCP/Plugins toggle off-screen. The fix
        (name-only checkbox + wrapped, clamped description label) must keep
        the container's natural width bounded regardless of description
        length — this seeds a real skill with a debug-mantra-scale (~250
        char) description and asserts the container never balloons back."""
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "no-bundle-here")
        monkeypatch.setattr(config, "ASSETS_ROOT", tmp_path / "no-bundle-here")
        monkeypatch.setattr(settings_window, "_allowed_project_roots", lambda _project: [tmp_path])
        long_desc = (
            "Four-mantra debugging discipline — reproduce, trace the fail path, "
            "falsify the hypothesis, cross-reference every breadcrumb. Recite the "
            "mantra block verbatim at the start of any debugging session, then "
            "apply the four steps in order before proposing any fix."
        )
        assert len(long_desc) > 200  # debug-mantra scale, per the audit measurement
        d = tmp_path / ".claude" / "skills" / "debug-mantra"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: debug-mantra\ndescription: {long_desc}\n---\n\nbody\n", encoding="utf-8"
        )
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_NEW_ROLE
        )
        width = dlg._nr_skills_container.sizeHint().width()
        # Audit doc measured 2405px before the fix, 554px after; keep a wide
        # margin above the "after" figure without re-permitting the overflow.
        assert width < 900, f"skills container sizeHint width regressed to {width}px"
        dlg.deleteLater()


class TestAutoskillsPanel:
    """Skill Catalog's "ดึง skill ตาม stack" button — bridges
    :mod:`autoskills_installer` on a worker thread, gated behind an explicit
    user confirmation (:class:`settings_window._AutoskillsConfirmDialog`)
    before anything is written."""

    @pytest.fixture(autouse=True)
    def _isolate_skill_roots(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "no-bundle-here")
        monkeypatch.setattr(config, "ASSETS_ROOT", tmp_path / "no-bundle-here")
        monkeypatch.setattr(settings_window, "_allowed_project_roots", lambda _project: [tmp_path])

    def test_scan_without_active_project_shows_warning(self, tmp_path: Path) -> None:
        dlg = settings_window.SettingsWindow(
            project=None, initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._on_autoskills_scan_clicked()
        assert dlg._as_status.text().startswith("!")
        dlg.deleteLater()

    def test_scan_disables_button_and_starts_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = {}

        def _fake_start(self_thread: object) -> None:
            started["project_root"] = self_thread._project_root

        monkeypatch.setattr(settings_window._AutoskillsPreviewThread, "start", _fake_start)
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._on_autoskills_scan_clicked()

        assert dlg._as_scan_btn.isEnabled() is False
        assert started["project_root"] == tmp_path
        dlg.deleteLater()

    def test_preview_error_shows_warning_and_reenables_button(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            settings_window.QMessageBox, "warning", staticmethod(lambda *a, **k: None)
        )
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._as_scan_btn.setEnabled(False)
        result = settings_window.autoskills_installer.PreviewResult(
            ok=False, error="ไม่พบ autoskills และไม่พบ npx บนเครื่องนี้"
        )
        dlg._on_autoskills_preview_ready(result)
        assert dlg._as_scan_btn.isEnabled() is True
        dlg.deleteLater()

    def test_preview_empty_skills_genuine_negative_shows_info_no_dialog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = {}
        monkeypatch.setattr(
            settings_window.QMessageBox,
            "information",
            staticmethod(lambda *a, **k: seen.setdefault("information", True)),
        )
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        result = settings_window.autoskills_installer.PreviewResult(
            ok=True, stack=["node"], skills=[], no_skills_for_stack=True
        )
        dlg._on_autoskills_preview_ready(result)
        assert seen.get("information") is True
        assert dlg._as_scan_btn.isEnabled() is True
        dlg.deleteLater()

    def test_preview_empty_skills_unparsed_shows_raw_output_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty `skills` with `no_skills_for_stack=False` means the parser
        didn't recognize the CLI's output — must show raw_output, never
        claim "not found" (the exact bug this round fixed)."""
        seen = {}
        monkeypatch.setattr(
            settings_window.QMessageBox,
            "warning",
            staticmethod(lambda self_dlg, title, text: seen.setdefault("text", text)),
        )
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        result = settings_window.autoskills_installer.PreviewResult(
            ok=True, stack=[], skills=[], raw_output="some unrecognized CLI output"
        )
        dlg._on_autoskills_preview_ready(result)
        assert "some unrecognized CLI output" in seen["text"]
        assert dlg._as_scan_btn.isEnabled() is True
        dlg.deleteLater()

    def test_preview_with_skills_opens_confirm_dialog_and_starts_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidate = settings_window.autoskills_installer.SkillCandidate(
            name="react-testing", source="https://skills.sh/react-testing"
        )
        result = settings_window.autoskills_installer.PreviewResult(
            ok=True, stack=["react"], skills=[candidate]
        )

        monkeypatch.setattr(
            settings_window._AutoskillsConfirmDialog,
            "exec",
            lambda self_dlg: settings_window.QDialog.DialogCode.Accepted,
        )
        started = {}

        def _fake_start(self_thread: object) -> None:
            started["project_root"] = self_thread._project_root
            started["selected"] = self_thread._selected_names

        monkeypatch.setattr(settings_window._AutoskillsInstallThread, "start", _fake_start)

        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._on_autoskills_preview_ready(result)

        assert started["project_root"] == tmp_path
        assert started["selected"] == ["react-testing"]
        dlg.deleteLater()

    def test_preview_dialog_cancelled_does_not_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidate = settings_window.autoskills_installer.SkillCandidate(name="react-testing")
        result = settings_window.autoskills_installer.PreviewResult(ok=True, skills=[candidate])
        monkeypatch.setattr(
            settings_window._AutoskillsConfirmDialog,
            "exec",
            lambda self_dlg: settings_window.QDialog.DialogCode.Rejected,
        )

        def _fail_start(self_thread: object) -> None:
            raise AssertionError("install must not start when the confirm dialog is cancelled")

        monkeypatch.setattr(settings_window._AutoskillsInstallThread, "start", _fail_start)
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._on_autoskills_preview_ready(result)  # must not raise
        dlg.deleteLater()

    def test_install_result_reports_overwritten_and_reloads_catalog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reloaded = {}
        monkeypatch.setattr(
            settings_window.QMessageBox, "information", staticmethod(lambda *a, **k: None)
        )
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        monkeypatch.setattr(
            dlg, "_reload_skill_catalog", lambda: reloaded.setdefault("called", True)
        )
        result = settings_window.autoskills_installer.InstallResult(
            ok=True, written=["a"], skipped=["b"], overwritten=["c"]
        )
        dlg._on_autoskills_install_ready(result)
        assert reloaded.get("called") is True
        assert dlg._as_scan_btn.isEnabled() is True
        dlg.deleteLater()

    def test_install_overwrite_failed_shows_critical_and_forces_reload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = {}
        monkeypatch.setattr(
            settings_window.QMessageBox,
            "critical",
            staticmethod(lambda *a, **k: seen.setdefault("critical", True)),
        )
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        result = settings_window.autoskills_installer.InstallResult(
            ok=False, overwrite_failed=["c"], error="data loss"
        )
        dlg._on_autoskills_install_ready(result)
        assert seen.get("critical") is True
        dlg.deleteLater()

    def test_confirm_dialog_selected_names_reflects_checkboxes(self) -> None:
        candidates = [
            settings_window.autoskills_installer.SkillCandidate(name="a", source="https://x/a"),
            settings_window.autoskills_installer.SkillCandidate(name="b"),
        ]
        result = settings_window.autoskills_installer.PreviewResult(ok=True, skills=candidates)
        dialog = settings_window._AutoskillsConfirmDialog(result)
        assert dialog.selected_names() == ["a", "b"]  # default: all ticked
        dialog._checks[1][1].setChecked(False)
        assert dialog.selected_names() == ["a"]
        dialog.deleteLater()

    def test_confirm_dialog_flagged_skill_starts_unchecked_and_shows_warning(self) -> None:
        """A skill the CLI itself annotated (e.g. "security check ⚠") must
        not be pre-ticked — the user has to opt in deliberately — and the
        annotation text must be visible in the dialog, not silently dropped."""
        flagged = settings_window.autoskills_installer.SkillCandidate(
            name="python-executor", source="inferen-sh › Python", notes="security check ⚠"
        )
        clean = settings_window.autoskills_installer.SkillCandidate(
            name="nodejs-backend-patterns", source="wshobson › Node.js"
        )
        result = settings_window.autoskills_installer.PreviewResult(
            ok=True, skills=[flagged, clean]
        )
        dialog = settings_window._AutoskillsConfirmDialog(result)
        assert dialog.selected_names() == ["nodejs-backend-patterns"]  # flagged skill excluded
        flagged_chk = dict((c.name, chk) for c, chk in dialog._checks)["python-executor"]
        clean_chk = dict((c.name, chk) for c, chk in dialog._checks)["nodejs-backend-patterns"]
        assert flagged_chk.isChecked() is False
        assert clean_chk.isChecked() is True
        labels = [
            w.text()
            for w in dialog.findChildren(settings_window.QLabel)
            if "security check" in w.text()
        ]
        assert labels, "flagged skill's annotation must be rendered somewhere in the dialog"
        dialog.deleteLater()
