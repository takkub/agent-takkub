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
from PyQt6.QtCore import QSettings, Qt
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
    team_preset,
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
    monkeypatch.setattr(team_preset, "_BASE_DIR", tmp_path)
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
    # The Accounts page (#505) also scans per-project selections under
    # user_profile._BASE_DIR/projects — captured at import time from
    # SETTINGS_HOME, so the config.SETTINGS_HOME patch below doesn't cover it.
    monkeypatch.setattr(user_profile, "_BASE_DIR", tmp_path)
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
    # Sidebar's ADVANCED section fold state (`_build_sidebar`) uses
    # `QSettings("agent-takkub", "cockpit")` — same org/app pair MainWindow
    # uses for window geometry and `project_nav` uses for the explorer's
    # expanded flag (see that module's own `isolated_nav_qsettings` fixture).
    # Redirect to a throwaway per-test INI so tests never read/write the
    # real machine's registry/INI store, and one test's fold-toggle can't
    # leak into another's default-collapsed assumption.
    ini_path = str(tmp_path / "cockpit_settings.ini")
    monkeypatch.setattr(
        settings_window,
        "QSettings",
        lambda *_a, **_kw: QSettings(ini_path, QSettings.Format.IniFormat),
    )
    saved = dict(roles_mod._CUSTOM)
    roles_mod._CUSTOM.clear()
    yield
    roles_mod._CUSTOM.clear()
    roles_mod._CUSTOM.update(saved)


class TestSettingsWindowStructure:
    def test_has_seventeen_stacked_slots_eight_nav_visible(self) -> None:
        # #515 settings diet: 8 nav-visible pages (ทั่วไป/ทีม & ตำแหน่ง/
        # Pipeline/Tools/Skills/Knowledge/Accounts/Usage across 4 sections —
        # GENERAL/TEAM/TOOLS/ACCOUNT, no ADVANCED) + 9 dead placeholder
        # slots for every VIEW_* constant that merged into one of those 8
        # or was dropped outright (VIEW_TEMPLATES, VIEW_PLUGINS_MATRIX,
        # VIEW_SKILL_MATRIX, VIEW_NEW_ROLE [now a QDialog, never a stack
        # page], VIEW_CORE_V2_ACCOUNTS/_ROUTING/_BRAIN/_SCHEDULER,
        # VIEW_PERFORMANCE) — `_goto_view` redirects every one of those
        # constants elsewhere (see `TestViewRedirects`), so the placeholder
        # slots exist only to keep every other VIEW_* index stable. VIEW_*
        # ints are NOT renumbered on purpose (old routes/tests/deep-links
        # keyed off them keep working) — total stack count is unchanged at
        # 17 from before this diet.
        dlg = settings_window.SettingsWindow()
        assert dlg._stack.count() == 17
        assert len(settings_window._NAV_VIEWS) == 8
        dlg.deleteLater()

    def test_initial_view_defaults_to_providers_roles(self) -> None:
        dlg = settings_window.SettingsWindow()
        assert dlg._stack.currentIndex() == settings_window.VIEW_PROVIDERS_ROLES
        dlg.deleteLater()

    def test_closing_schedules_deletion_instead_of_leaking(self) -> None:
        """H3 (cross-review 2026-09-07): a probe that opened/closed
        SettingsWindow 5 times found every instance still alive — nothing
        ever scheduled its deletion. `close()` (which `reject()`/`accept()`
        both call) must now mark it `WA_DeleteOnClose`."""
        from PyQt6.QtCore import Qt

        dlg = settings_window.SettingsWindow()
        assert dlg.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
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
        dlg._goto_view(settings_window.VIEW_KNOWLEDGE)
        assert dlg._content_title.text() == "Knowledge"
        dlg.deleteLater()

    def test_view_headers_never_leak_a_raw_github_issue_number(self) -> None:
        """2026-09-08 design review — settings descriptions used to embed
        raw issue tags like "(#512)"/"(#505)"/"(#507)" straight into
        user-facing copy."""
        import re

        for _title, subtitle in settings_window._VIEW_HEADERS.values():
            assert not re.search(r"#\d+", subtitle), subtitle

    def test_status_strip_drops_the_redundant_brand_and_version_labels(self) -> None:
        """2026-09-08 design review — the status strip repeated the OS title
        bar's own "Takkub Cockpit — Settings" text (a "takkub COCKPIT" brand
        label + a version number), adding little beyond duplication."""
        from PyQt6.QtWidgets import QLabel, QWidget

        dlg = settings_window.SettingsWindow()
        strip = dlg.findChild(QWidget, "statusStrip")
        assert strip is not None
        strip_texts = [w.text() for w in strip.findChildren(QLabel)]
        assert "takkub COCKPIT" not in strip_texts
        assert not any(t.startswith("v") and "." in t for t in strip_texts)
        dlg.deleteLater()

    def test_machine_mode_change_persists_and_requests_live_reload(self) -> None:
        """#515: replaces the old Performance page's preset dropdown — same
        machine-aware `performance_settings.preset()` underneath, but
        General write-throughs immediately (it's a `_NO_FOOTER_SAVE_VIEWS`
        page) instead of staging into `_dirty_views` for a footer Save &
        Apply click."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_GENERAL)
        dlg._machine_mode_combo.setCurrentIndex(dlg._machine_mode_combo.findData("safe"))
        assert dlg.pending_performance_reload is True
        saved = performance_settings.load()
        assert saved.mode == "safe"
        assert saved.max_heavy_global == 2
        dlg.deleteLater()


class TestViewRedirects:
    """#515 settings diet: every VIEW_* constant a merged/dropped page used
    to own now redirects to whichever page absorbed it (`settings_window.
    _VIEW_REDIRECTS`) — same "old constant still lands somewhere sane"
    contract #505 established for VIEW_CORE_V2_ACCOUNTS below. Replaces the
    removed `TestAdvancedSectionFold` (the ADVANCED sidebar section itself
    is gone — nothing left to fold)."""

    def test_accounts_pools_redirects_to_accounts(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_ACCOUNTS)
        assert dlg._stack.currentIndex() == settings_window.VIEW_USERS
        dlg.deleteLater()

    def test_templates_redirects_to_pipeline(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_TEMPLATES)
        assert dlg._stack.currentIndex() == settings_window.VIEW_PIPELINE_BUILDER
        dlg.deleteLater()

    def test_plugins_matrix_redirects_to_tools(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PLUGINS_MATRIX)
        assert dlg._stack.currentIndex() == settings_window.VIEW_MCP_MATRIX
        dlg.deleteLater()

    def test_skill_matrix_redirects_to_skills(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_SKILL_MATRIX)
        assert dlg._stack.currentIndex() == settings_window.VIEW_SKILL_CATALOG
        dlg.deleteLater()

    def test_performance_redirects_to_general(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PERFORMANCE)
        assert dlg._stack.currentIndex() == settings_window.VIEW_GENERAL
        dlg.deleteLater()

    def test_core_v2_routing_and_scheduler_redirect_to_general(self) -> None:
        for legacy in (
            settings_window.VIEW_CORE_V2_ROUTING,
            settings_window.VIEW_CORE_V2_SCHEDULER,
        ):
            dlg = settings_window.SettingsWindow(initial_view=legacy)
            assert dlg._stack.currentIndex() == settings_window.VIEW_GENERAL
            dlg.deleteLater()

    def test_core_v2_brain_redirects_to_knowledge(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_BRAIN)
        assert dlg._stack.currentIndex() == settings_window.VIEW_KNOWLEDGE
        dlg.deleteLater()

    def test_new_role_redirects_to_providers_roles(self) -> None:
        """`_goto_view(VIEW_NEW_ROLE)` must stay a fast, synchronous page-
        switch (this constructs `SettingsWindow` itself, so a `.exec()` here
        would hang the test forever) — the dialog only ever opens from its
        own button's click handler, never through `_goto_view`."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_NEW_ROLE)
        assert dlg._stack.currentIndex() == settings_window.VIEW_PROVIDERS_ROLES
        dlg.deleteLater()

    def test_nav_has_eight_visible_items_in_four_sections(self) -> None:
        dlg = settings_window.SettingsWindow()
        assert len(settings_window._NAV_VIEWS) == 8
        sections = {section for _view, _label, section in settings_window._NAV_VIEWS}
        assert sections == {"GENERAL", "TEAM", "TOOLS", "ACCOUNT"}
        assert len(dlg._nav_buttons) == 8
        dlg.deleteLater()


class TestNewRoleView:
    """#515: New Role is a QDialog opened from a button on "ทีม & ตำแหน่ง"
    now, not a stack page — `_build_new_role_view()` builds the exact same
    form/attributes (`_nr_*`) `_open_new_role_dialog` wraps in a QDialog,
    without the blocking `.exec()` a real open would need a user to close;
    tests call it directly on a plain `SettingsWindow()` instead of the old
    `initial_view=VIEW_NEW_ROLE` construction (that constant now just
    redirects to VIEW_PROVIDERS_ROLES, the page the button lives on — see
    `settings_window._VIEW_REDIRECTS`)."""

    def test_create_role_persists_and_registers_live(self) -> None:
        dlg = settings_window.SettingsWindow()
        dlg._build_new_role_view()
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
        dlg = settings_window.SettingsWindow()
        dlg._build_new_role_view()
        dlg._nr_name.setText("lead")
        dlg._on_create_role_clicked()

        assert "lead" not in custom_roles.load_custom_roles()
        assert dlg._nr_status.text().startswith("!")
        dlg.deleteLater()

    def test_new_role_fields_never_mark_the_window_dirty(self) -> None:
        """#515: New Role no longer joins the footer's Save & Apply/dirty-
        tracking transaction at all — it writes through immediately via its
        own "+ Create Role" button — so typing into its fields must NOT
        flip the underlying window's unsaved-changes indicator (that would
        be wrong regardless: those fields describe an entirely different
        dialog by the time a real user sees them)."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        dlg._build_new_role_view()
        assert dlg._dirty is False
        dlg._nr_name.setText("data-eng")
        dlg._nr_instructions.setPlainText("do data things")
        assert dlg._dirty is False
        dlg.deleteLater()

    def test_default_swatch_color_is_in_palette(self) -> None:
        """Codex/Gemini #17 — the initial swatch color must be one of the
        selectable palette colors so a swatch shows selected on first open."""
        dlg = settings_window.SettingsWindow()
        dlg._build_new_role_view()
        assert dlg._nr_color == project_nav._AVATAR_COLORS[0]
        assert dlg._nr_color in project_nav._AVATAR_COLORS
        dlg.deleteLater()

    def test_roster_panel_has_new_role_button_opening_the_dialog(self) -> None:
        """The one entry point into New Role now (#515) — a button on the
        "ทีม & ตำแหน่ง" roster card, replacing the old persistent sidebar
        button that stayed visible regardless of which page was showing."""
        from PyQt6.QtWidgets import QPushButton

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        buttons = dlg._roster_panel.findChildren(QPushButton)
        matches = [b for b in buttons if "ตำแหน่งเฉพาะโปรเจค" in b.text()]
        assert len(matches) == 1
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
        dlg = settings_window.SettingsWindow()
        dlg._build_new_role_view()
        assert [s.name for s, _chk in dlg._nr_skill_checks] == ["test-skill"]
        dlg.deleteLater()

    def test_no_skills_dir_shows_empty_list_without_crashing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dlg = settings_window.SettingsWindow()
        dlg._build_new_role_view()
        assert dlg._nr_skill_checks == []
        dlg.deleteLater()

    def test_selected_skill_embedded_into_default_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#4 in the task spec — an empty Instructions box still gets the
        skill reference embedded into the generated default template."""
        self._write_skill(tmp_path, "test-skill", "does a thing")
        dlg = settings_window.SettingsWindow()
        dlg._build_new_role_view()
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
        dlg = settings_window.SettingsWindow()
        dlg._build_new_role_view()
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
        dlg = settings_window.SettingsWindow()
        dlg._build_new_role_view()
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

        dlg = settings_window.SettingsWindow()
        dlg._build_new_role_view()
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

    def test_save_apply_hand_toggle_flips_fixed_team_preset_to_custom(self) -> None:
        """#512 acceptance: hand-toggling a role on this page while the
        project sits on a fixed team preset flips it to "custom"."""
        team_preset.set_current("solo-lead", None)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        dlg._role_toggles["backend"].setChecked(True)
        dlg._on_save_apply_clicked()

        cfg = team_preset.current(None)
        assert cfg["preset"] == "custom"
        assert cfg["roles"]["backend"] is True
        dlg.deleteLater()

    def test_save_apply_matching_preset_does_not_flip_to_custom(self) -> None:
        team_preset.set_current("full", None)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        # No role toggles changed — save with the preset's own values intact.
        dlg._role_toggles["backend"].setChecked(True)
        dlg._on_save_apply_clicked()

        assert team_preset.current_preset_id(None) == "full"
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
        """Codex Medium #6 — dirty must be tracked per-view, not globally.
        Uses Providers & Roles + Pipeline (both still footer-tracked, #515)
        — New Role no longer participates in this transaction at all (its
        own "+ Create Role" button writes through immediately), so it can't
        stand in as "the other still-dirty view" here anymore."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        dlg._role_toggles["qa"].setChecked(False)
        dlg._goto_view(settings_window.VIEW_PIPELINE_BUILDER)
        dlg._on_add_hop_clicked()
        assert dlg._dirty is True

        dlg._on_reset_clicked()  # reverts the Pipeline view only

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

        # #512: Reset rebuilds the roster panel from scratch (its rows can
        # differ per team-size pick), so the reset combo is a fresh widget —
        # re-fetch it rather than asserting on the pre-reset reference.
        assert dlg._role_effort_combos["backend"].currentData() == "medium"
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
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_MCP_MATRIX)
        view = dlg._stack.widget(settings_window.VIEW_MCP_MATRIX).widget()
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
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_MCP_MATRIX)
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
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_MCP_MATRIX)
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

    def test_token_cost_hint_and_per_column_estimate_shown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#516: the Tools/Plugins tab surfaces a boot-token cost estimate
        next to each marketplace's name, plus a one-line hint that an
        enabled plugin is loaded into every pane of that role at spawn."""
        from PyQt6.QtWidgets import QLabel

        monkeypatch.setattr(
            settings_window.pane_tools_dialog, "discover_marketplaces", lambda: ["pordee"]
        )
        monkeypatch.setattr(
            settings_window.pane_tools_dialog,
            "marketplace_token_costs",
            lambda items: {item: 1234 for item in items},
        )
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_MCP_MATRIX)
        view = dlg._stack.widget(settings_window.VIEW_MCP_MATRIX).widget()
        labels = [lbl.text() for lbl in view.findChildren(QLabel)]
        assert any("โหลดเข้าทุก pane" in t for t in labels)
        assert any("tok" in t for t in labels)
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
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_SKILL_CATALOG)
        assert set(dlg._skill_toggles.keys()) == set(skill_policy.skill_matrix_roles())
        for items in dlg._skill_toggles.values():
            assert set(items.keys()) == {"debug-mantra", "verify"}
        assert "codex" in dlg._skill_toggles
        assert "gemini" in dlg._skill_toggles
        assert "shell" not in dlg._skill_toggles
        assert dlg._skill_matrix_empty.isHidden()
        dlg.deleteLater()

    def test_matrix_header_shows_per_skill_token_cost(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """#516: each skill column's header shows an estimated boot-token
        cost, read straight from that skill's own SKILL.md."""
        from PyQt6.QtWidgets import QLabel

        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("a" * 38, encoding="utf-8")
        monkeypatch.setattr(
            settings_window.skill_scan,
            "scan_skills",
            lambda roots: [
                __import__("agent_takkub.skill_scan", fromlist=["SkillInfo"]).SkillInfo(
                    name="sized-skill", description="d", path=skill_md
                )
            ],
        )
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_SKILL_CATALOG)
        matrix_view = dlg._stack.widget(settings_window.VIEW_SKILL_CATALOG).widget().widget(1)
        labels = [lbl.text() for lbl in matrix_view.findChildren(QLabel)]
        assert any("tok" in t for t in labels)
        dlg.deleteLater()

    def test_empty_catalog_shows_empty_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings_window.skill_scan, "scan_skills", lambda roots: [])
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_SKILL_CATALOG)
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
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_SKILL_CATALOG)
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
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_SKILL_CATALOG)
        dlg._skill_toggles["backend"]["debug-mantra"].setChecked(True)
        dlg._mark_dirty()
        dlg._on_reset_clicked()
        assert dlg._skill_toggles["backend"]["debug-mantra"].isChecked() is False
        assert skill_policy.effective_skills("backend") == []
        dlg.deleteLater()


class TestSkillCatalogView:
    """The new, real skill browser backed by skill_scan (SKILL section)."""

    def test_tab_widget_has_a_minimum_height_floor(self) -> None:
        """2026-09-08 design review (critic §2.4 "Skills View Blank Canvas
        Collapse") — the Catalog/Matrix QTabWidget is swapped into its
        QScrollArea lazily and can compute a near-zero sizeHint on a
        first-paint race; a hard minimumHeight makes a full visual collapse
        structurally impossible regardless of the exact timing."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_SKILL_CATALOG)
        tabs = dlg._stack.currentWidget().widget()
        assert isinstance(tabs, settings_window.QTabWidget)
        assert tabs.minimumHeight() >= 420
        dlg.deleteLater()

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
        # #516: name is followed by an estimated boot-token cost badge
        assert dlg._catalog_name.text().startswith("cockpit-ui-style")
        assert "tok" in dlg._catalog_name.text()
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

        # New Role (#515) is a QDialog now — `_build_new_role_view()` builds
        # the same form/`_nr_*` attributes without the blocking `.exec()` a
        # real open would need a user to close.
        dlg._build_new_role_view()
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

    def test_hop_connector_is_not_a_bare_ascii_v(self) -> None:
        """2026-09-08 design review — the connector between hops used to be
        the literal text "v wait for all" (a lowercase letter doing double
        duty as a flowchart arrow); must be a real arrow glyph instead."""
        from PyQt6.QtWidgets import QLabel

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        dlg._on_palette_role_clicked("backend")  # ensure at least 2 hops exist
        connector_texts = []
        for i in range(dlg._pb_hops_lay.count()):
            w = dlg._pb_hops_lay.itemAt(i).widget()
            if isinstance(w, QLabel) and "wait for all" in w.text():
                connector_texts.append(w.text())
        assert connector_texts, "expected at least one connector label between hops"
        for text in connector_texts:
            assert not text.startswith("v "), f"bare-ascii-v connector regressed: {text!r}"
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
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        assert dlg._tpl_list.count() >= 1
        assert dlg._tpl_delete_btn.isEnabled() is False  # first row is builtin
        dlg.deleteLater()

    def test_duplicate_creates_non_builtin_copy(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        before = len(dlg._pipeline_payload["templates"])
        dlg._on_template_duplicate_clicked()

        assert len(dlg._pipeline_payload["templates"]) == before + 1
        payload = pipeline_config.load(None)
        assert len(payload["templates"]) == before + 1
        new_tpl = payload["templates"][-1]
        assert new_tpl["builtin"] is False
        dlg.deleteLater()

    def test_delete_removes_duplicated_template(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        dlg._on_template_duplicate_clicked()
        dlg._reload_templates_list()
        dlg._tpl_list.setCurrentRow(dlg._tpl_list.count() - 1)
        before = len(dlg._pipeline_payload["templates"])

        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
        dlg._on_template_delete_clicked()

        assert len(dlg._pipeline_payload["templates"]) == before - 1
        dlg.deleteLater()

    def test_selecting_a_template_loads_its_hops_into_the_builder(self) -> None:
        """#515: Pipeline merged the old Templates + Pipeline Builder pages
        into one — selecting a row in the list IS "start editing this
        template's hops" now, no separate "Edit hops ->" button/detour."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        second_id = dlg._pipeline_payload["templates"][1]["id"]
        for row in range(dlg._tpl_list.count()):
            if dlg._tpl_list.item(row).data(Qt.ItemDataRole.UserRole) == second_id:
                dlg._tpl_list.setCurrentRow(row)
                break
        assert dlg._pb_template_id == second_id
        dlg.deleteLater()

    def test_long_template_name_is_elided_not_hard_clipped(self) -> None:
        """Critic #2026-07-10 v2 regression — 'Feature (UI+API)' rendered as
        'Feature (UI+AP' (clipped mid-glyph, no ellipsis) because the
        fixed-width BUILT-IN chip left too little room for the label."""
        from PyQt6.QtGui import QFontMetrics

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        metrics = QFontMetrics(dlg._tpl_list.font())
        long_name = "A Very Long Template Name That Cannot Possibly Fit (UI+API)"
        elided = dlg._elide_template_name(metrics, long_name, avail_width=60)
        assert elided != long_name
        assert elided.endswith("…")  # real ellipsis, not a mid-word hard clip
        assert long_name.startswith(elided[:-1])
        dlg.deleteLater()

    def test_short_template_name_not_elided(self) -> None:
        from PyQt6.QtGui import QFontMetrics

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        metrics = QFontMetrics(dlg._tpl_list.font())
        short_name = "Blank"
        elided = dlg._elide_template_name(metrics, short_name, avail_width=500)
        assert elided == short_name
        dlg.deleteLater()

    def test_compact_chip_width_reserves_space_for_builtin_badge(self) -> None:
        from PyQt6.QtGui import QFontMetrics

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        metrics = QFontMetrics(dlg._tpl_list.font())
        width = dlg._compact_chip_width(metrics, "BUILT-IN")
        assert width > metrics.horizontalAdvance("BUILT-IN")
        dlg.deleteLater()

    def test_builtin_row_label_carries_full_name_as_tooltip(self) -> None:
        """Even when elided, the full name must stay reachable (tooltip) —
        eliding must not be a silent data loss."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PIPELINE_BUILDER)
        first_tpl = dlg._pipeline_payload["templates"][0]
        row_widget = dlg._tpl_list.itemWidget(dlg._tpl_list.item(0))
        name_label = row_widget.layout().itemAt(0).widget()
        assert name_label.toolTip() == first_tpl["name"]
        dlg.deleteLater()


class TestAccountsView:
    """#505 — the unified Accounts page at VIEW_USERS (replaces the old Users
    Profiles tab + the ADVANCED Accounts & Pools page). Widget-tofu checks +
    the write-through wiring via `accounts_adapter` (pure adapter behavior is
    covered separately in tests/test_accounts_adapter.py)."""

    @pytest.fixture(autouse=True)
    def _drain_accounts_refresh_pool(self):
        """B-M2 (2026-09-07): `_AccountsRefreshWorker` runs on the
        process-global `QThreadPool`, not per-dialog — a job left running
        by an earlier test (in this class or another file in the same
        batch) can still be in flight when a later test reads/writes the
        same on-disk profile store, producing the exact nondeterministic
        failure the round-2 review reproduced. Draining the pool both
        before and after every test removes that cross-test window."""
        from PyQt6.QtCore import QThreadPool
        from PyQt6.QtWidgets import QApplication

        QThreadPool.globalInstance().waitForDone(5_000)
        yield
        QThreadPool.globalInstance().waitForDone(5_000)
        app = QApplication.instance()
        if app is not None:
            for _ in range(10):
                app.processEvents()

    def test_accounts_nav_item_present_and_clickable(self) -> None:
        dlg = settings_window.SettingsWindow()
        assert settings_window.VIEW_USERS in dlg._nav_buttons
        dlg._nav_buttons[settings_window.VIEW_USERS].click()
        assert dlg._stack.currentIndex() == settings_window.VIEW_USERS
        assert dlg._content_title.text() == "Accounts"
        dlg.deleteLater()

    def test_accounts_pools_route_redirects_to_accounts(self) -> None:
        """The old ADVANCED → Accounts & Pools constant must not break —
        it lands on the unified Accounts page now."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_CORE_V2_ACCOUNTS)
        assert dlg._stack.currentIndex() == settings_window.VIEW_USERS
        assert dlg._content_title.text() == "Accounts"
        dlg.deleteLater()

    def test_accounts_pools_not_in_sidebar_nav(self) -> None:
        dlg = settings_window.SettingsWindow()
        assert settings_window.VIEW_CORE_V2_ACCOUNTS not in dlg._nav_buttons
        dlg.deleteLater()

    def test_accounts_refresh_hooks_a_bounded_wait_at_shutdown(self) -> None:
        """B-M2 (2026-09-07 round-2 review): `_AccountsRefreshWorker` runs
        on the process-global `QThreadPool`, not a per-dialog `QThread`, so
        it cannot be joined/cancelled the way `_ACTIVE_THREADS` joins
        `_CallableThread` — the fix is a bounded `aboutToQuit` wait on the
        whole pool instead, wired lazily the first time a refresh runs."""
        from PyQt6.QtWidgets import QApplication

        from agent_takkub import settings_accounts

        app = QApplication.instance()
        was_hooked = settings_accounts._accounts_refresh_shutdown_hooked
        settings_accounts._accounts_refresh_shutdown_hooked = False
        try:
            dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
            assert settings_accounts._accounts_refresh_shutdown_hooked is True
            dlg.deleteLater()
        finally:
            if not was_hooked and app is not None:
                app.aboutToQuit.disconnect(settings_accounts._wait_for_accounts_refresh_jobs)
            settings_accounts._accounts_refresh_shutdown_hooked = was_hooked

    def test_gap_reason_full_text_moved_behind_diagnostics_button(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """2026-09-08 design review — a provider isolation-gap panel used to
        render the FULL raw probe note (internal env-var names, provider
        version, sqlite table names — `accounts_adapter.PROVIDER_ISOLATION_
        GAPS`) inline as a wrapped paragraph. Now only a short, generic
        sentence shows inline; the raw note is reachable via a Diagnostics
        button that opens a themed message box."""
        from PyQt6.QtWidgets import QLabel, QPushButton

        from agent_takkub import accounts_adapter

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        row = accounts_adapter.ProviderRow(
            provider="gemini",
            display_name="Gemini",
            gap_reason="agy 1.1.27 (probed 2026-09-07): binary-string sweep found no home knob",
            accounts=[],
            can_add=False,
            add_hint="",
        )
        panel = dlg._build_provider_panel(row)
        panel_labels = [w.text() for w in panel.findChildren(QLabel)]
        assert not any(row.gap_reason in text for text in panel_labels)
        buttons = [b for b in panel.findChildren(QPushButton) if b.text() == "Diagnostics"]
        assert len(buttons) == 1

        seen: dict = {}

        def _fake_exec(self):
            seen["title"] = self.windowTitle()
            seen["informative"] = self.informativeText()
            return 0

        monkeypatch.setattr(QMessageBox, "exec", _fake_exec)
        buttons[0].click()  # real click signal, not a direct method call
        assert row.gap_reason in seen["informative"]
        dlg.deleteLater()

    def test_renders_loading_placeholder_before_the_background_refresh_lands(self) -> None:
        """#505 review M4: the account rows must never block construction —
        the box starts with a single loading row, not the real panels."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        assert dlg._accounts_rows_box.count() == 1
        dlg.deleteLater()

    def test_renders_one_panel_per_provider(self) -> None:
        from PyQt6.QtCore import QThreadPool
        from PyQt6.QtWidgets import QApplication

        from agent_takkub.provider_spec import PROVIDER_REGISTRY

        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        QThreadPool.globalInstance().waitForDone(5_000)
        app = QApplication.instance()
        for _ in range(10):
            app.processEvents()
        assert dlg._accounts_rows_box.count() == len(PROVIDER_REGISTRY)
        dlg.deleteLater()

    def test_gap_provider_panel_still_renders_its_account_cards(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#505 review M7: a gap-provider panel used to `return panel` before
        ever looping `row.accounts` — an existing account was invisible."""
        from PyQt6.QtCore import QThreadPool
        from PyQt6.QtWidgets import QApplication

        from agent_takkub import config, user_profile

        gap_provider = next(iter(config.PROVIDER_ISOLATION_GAPS))
        user_profile.add_profile(
            "gap-office", str(tmp_path / "gap-cfg"), provider=gap_provider, share_sessions=False
        )
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        try:
            QThreadPool.globalInstance().waitForDone(5_000)
            app = QApplication.instance()
            for _ in range(10):
                app.processEvents()
            texts = " ".join(w.text() for w in dlg.findChildren(settings_window.QLabel))
            assert "gap-office" in texts
        finally:
            dlg.deleteLater()

    def test_add_account_persists_and_updates_auth_combo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from PyQt6.QtCore import QThreadPool
        from PyQt6.QtWidgets import QApplication

        from agent_takkub import settings_accounts

        class _FakeDialog:
            def __init__(self, *_a, **_k) -> None:
                pass

            def exec(self) -> int:
                from PyQt6.QtWidgets import QDialog

                return QDialog.DialogCode.Accepted

            def values(self):
                return ("work", str(tmp_path / "work-cfg"), False)

        monkeypatch.setattr(settings_accounts, "_AddAccountDialog", _FakeDialog)
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        # B-M2 (2026-09-07 round-2 review) — see the sibling remove-account
        # test's comment: drain the construction-triggered background
        # refresh before writing to the same registry file it's reading.
        QThreadPool.globalInstance().waitForDone(5_000)
        app = QApplication.instance()
        if app is not None:
            for _ in range(10):
                app.processEvents()
        dlg._on_accounts_add_clicked("claude")

        assert any(p["name"] == "work" for p in user_profile.list_profiles())
        assert dlg._up_auth_combo.count() == 2
        assert "work" in dlg._up_status.text()
        dlg.deleteLater()

    def test_remove_account_persists_and_updates_auth_combo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from PyQt6.QtCore import QThreadPool
        from PyQt6.QtWidgets import QApplication

        from agent_takkub import accounts_adapter

        user_profile.add_profile("work", str(tmp_path / "work-cfg"), share_sessions=False)

        class _FakeBox:
            def setWindowTitle(self, *_a) -> None: ...
            def setText(self, *_a) -> None: ...
            def setStandardButtons(self, *_a) -> None: ...
            def exec(self):
                return QMessageBox.StandardButton.Yes

        monkeypatch.setattr(
            settings_window.cockpit_theme, "themed_message_box", lambda *_a: _FakeBox()
        )
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_USERS)
        # B-M2 (2026-09-07 round-2 review): `SettingsWindow()` construction
        # kicks off `_AccountsRefreshWorker` on the background QThreadPool,
        # which READS `user_profile`'s registry file for every provider —
        # if that job is still in flight when `remove_profile`'s own
        # atomic-write races it on Windows, `os.replace` can raise, and
        # `remove_profile` swallows that `OSError` silently, leaving "work"
        # never actually removed on disk. Draining the pool before touching
        # the registry again removes the race instead of just hoping the
        # timing works out — same pattern `test_renders_one_panel_per_provider`
        # already uses for this exact worker.
        QThreadPool.globalInstance().waitForDone(5_000)
        app = QApplication.instance()
        if app is not None:
            for _ in range(10):
                app.processEvents()
        account = accounts_adapter.AccountInfo(
            provider="claude",
            name="work",
            config_dir=str(tmp_path / "work-cfg"),
            is_default=False,
            login=accounts_adapter.LoginStatus(accounts_adapter.UNKNOWN),
        )
        dlg._on_accounts_remove_clicked(account)

        assert not any(p["name"] == "work" for p in user_profile.list_profiles())
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
        dlg = settings_window.SettingsWindow(project="demo")
        dlg._build_new_role_view()
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
        dlg = settings_window.SettingsWindow(project="demo")
        dlg._build_new_role_view()
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


class TestTeamPresetView:
    """#512 — Providers & Roles ("ทีม & ตำแหน่ง") team-size cards + the
    roster panel they drive, and the dead exec-mode/auto-resume chip stubs
    this issue's UI work retires."""

    def test_nav_renamed_and_grouped_under_team_section(self) -> None:
        entries = {label: section for _idx, label, section in settings_window._NAV_VIEWS}
        assert entries["ทีม & ตำแหน่ง"] == "TEAM"
        # #515: Pipeline Builder + Templates merged into one "Pipeline" nav
        # item under the same TEAM section.
        assert entries["Pipeline"] == "TEAM"
        assert "Pipeline Builder" not in entries
        assert "Templates" not in entries
        assert "Providers & Roles" not in entries

    def test_team_size_shows_four_cards_matching_quick_preset_ids(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        assert set(dlg._team_preset_cards.keys()) == set(team_preset.QUICK_PRESET_IDS)
        dlg.deleteLater()

    def test_default_project_selects_auto_card(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        assert dlg._selected_team_preset_id == "auto"
        assert dlg.pending_team_preset is None
        dlg.deleteLater()

    def test_clicking_a_card_stages_the_pick_and_marks_dirty(self) -> None:
        dlg = settings_window.SettingsWindow(
            project="proj-a", initial_view=settings_window.VIEW_PROVIDERS_ROLES
        )
        dlg._on_team_preset_card_clicked("full")
        assert dlg._selected_team_preset_id == "full"
        assert dlg.pending_team_preset == "full"
        assert settings_window.VIEW_PROVIDERS_ROLES in dlg._dirty_views
        # staged only — nothing written to disk yet.
        assert team_preset.current_preset_id("proj-a") == "auto"
        dlg.deleteLater()

    def test_clicking_full_enables_every_position_toggle_in_preview(self) -> None:
        dlg = settings_window.SettingsWindow(
            project="proj-a", initial_view=settings_window.VIEW_PROVIDERS_ROLES
        )
        dlg._on_team_preset_card_clicked("full")
        for role in team_preset.POSITION_ROLES:
            assert dlg._role_toggles[role].isChecked() is True
        assert dlg._role_toggles["qa"].isChecked() is True
        dlg.deleteLater()

    def test_clicking_solo_lead_disables_every_position_and_drops_checker_row(self) -> None:
        dlg = settings_window.SettingsWindow(
            project="proj-a", initial_view=settings_window.VIEW_PROVIDERS_ROLES
        )
        dlg._on_team_preset_card_clicked("solo-lead")
        for role in team_preset.POSITION_ROLES:
            assert dlg._role_toggles[role].isChecked() is False
        assert "qa" not in dlg._role_toggles
        assert "reviewer" not in dlg._role_toggles
        assert "lead" in dlg._role_toggles or "lead" in dlg._role_provider_combos
        dlg.deleteLater()

    def test_checker_row_labeled_as_the_checker(self) -> None:
        dlg = settings_window.SettingsWindow(
            project="proj-a", initial_view=settings_window.VIEW_PROVIDERS_ROLES
        )
        dlg._on_team_preset_card_clicked("pair")
        assert "reviewer" in dlg._role_toggles
        assert "qa" not in dlg._role_toggles
        row = dlg._role_toggles["reviewer"].parent()
        label_texts = [w.text() for w in row.findChildren(settings_window.QLabel)]
        assert any("ตัวตรวจ" in t for t in label_texts)
        dlg.deleteLater()

    def test_lead_row_always_present_regardless_of_preset(self) -> None:
        dlg = settings_window.SettingsWindow(
            project="proj-a", initial_view=settings_window.VIEW_PROVIDERS_ROLES
        )
        assert "lead" in dlg._role_provider_combos
        dlg._on_team_preset_card_clicked("solo-lead")
        assert "lead" in dlg._role_provider_combos
        dlg.deleteLater()

    def test_exec_mode_wording_uses_thai_not_solo_parallel(self) -> None:
        dlg = settings_window.SettingsWindow(
            project="proj-a", initial_view=settings_window.VIEW_PROVIDERS_ROLES
        )
        dlg._on_team_preset_card_clicked("full")
        assert dlg._team_preset_exec_line.text() == "โหมดทำงาน: แตกหลายคน"
        dlg._on_team_preset_card_clicked("solo-lead")
        assert dlg._team_preset_exec_line.text() == "โหมดทำงาน: 1 คน/ตำแหน่ง"
        dlg.deleteLater()

    def test_save_apply_persists_the_picked_preset(self) -> None:
        dlg = settings_window.SettingsWindow(
            project="proj-a", initial_view=settings_window.VIEW_PROVIDERS_ROLES
        )
        dlg._on_team_preset_card_clicked("full")
        dlg._on_save_apply_clicked()
        assert team_preset.current_preset_id("proj-a") == "full"
        cfg = team_preset.current("proj-a")
        assert all(cfg["roles"].values())
        assert cfg["checker"] == "qa"

    def test_hand_toggle_after_save_still_flips_a_fixed_preset_to_custom(self) -> None:
        """#512 acceptance, unchanged by the #512-UI refactor of this view:
        toggling a role by hand while a FIXED preset is saved flips the
        project to custom on the next Save & Apply."""
        team_preset.set_current("full", "proj-a")
        dlg = settings_window.SettingsWindow(
            project="proj-a", initial_view=settings_window.VIEW_PROVIDERS_ROLES
        )
        dlg._role_toggles["backend"].setChecked(False)
        dlg._on_save_apply_clicked()
        assert team_preset.current_preset_id("proj-a") == "custom"

    def test_reset_discards_staged_card_pick(self) -> None:
        team_preset.set_current("pair", "proj-a")
        dlg = settings_window.SettingsWindow(
            project="proj-a", initial_view=settings_window.VIEW_PROVIDERS_ROLES
        )
        dlg._on_team_preset_card_clicked("full")
        dlg._on_reset_clicked()
        assert dlg._selected_team_preset_id == "pair"
        assert dlg.pending_team_preset is None
        assert "reviewer" in dlg._role_toggles
        assert "qa" not in dlg._role_toggles
        dlg.deleteLater()

    def test_secondary_brains_panel_lists_non_claude_providers_as_chips(self) -> None:
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        panel = dlg._build_secondary_brains_panel(dlg)
        texts = " ".join(w.text() for w in panel.findChildren(settings_window.QLabel) if w.text())
        for provider in ("codex", "gemini", "opencode", "kimi", "cursor"):
            assert provider.capitalize() in texts
        assert "claude" not in texts.lower()
        panel.deleteLater()
        dlg.deleteLater()

    def test_team_size_cards_use_painted_dots_not_tofu_prone_glyphs(self) -> None:
        """2026-07-24 design review #4: IBM Plex Sans/Mono don't ship "○"/
        "●" — the radio indicator must be a painted `cockpit_theme.color_dot`
        widget, never that text glyph (this file's OWN role dots already
        dodge this; the team-size card radio must too)."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        for card in dlg._team_preset_cards.values():
            for label in card.findChildren(settings_window.QLabel):
                assert label.text() not in ("○", "●")
        dlg.deleteLater()

    def test_legacy_settings_route_still_lands_on_team_view(self) -> None:
        """Old route constant, new label — the #512 UI rename must not break
        any existing entry point keyed off VIEW_PROVIDERS_ROLES."""
        dlg = settings_window.SettingsWindow(initial_view=settings_window.VIEW_PROVIDERS_ROLES)
        assert dlg._stack.currentIndex() == settings_window.VIEW_PROVIDERS_ROLES
        assert dlg._nav_buttons[settings_window.VIEW_PROVIDERS_ROLES].property("active") is True
        dlg.deleteLater()
