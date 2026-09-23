"""Regression tests for the 2026-09-23 system-review findings in the Settings
window (`settings_window.py` / `settings_knowledge_design.py`):

* Save & Apply under solo-lead/pair wrote `rolesEnabled=False` for every
  position it merely RENDERED off (the preset excludes them), poisoning
  pipelines.json for the next preset switch.
* The autoskills scan/install QThreads were parented to the
  WA_DeleteOnClose SettingsWindow — closing Settings mid-scan destroyed a
  running QThread (Qt6 qFatal, #688 class).
* Knowledge/Design Refresh + Test dereferenced a `_CallableThread` that
  `_cleanup` had already `deleteLater()`-ed, so the second click raised
  RuntimeError instead of starting a new run.

Offscreen QPA (session-scoped QApplication from tests/conftest.py); every
on-disk store is redirected to tmp_path exactly like test_settings_window.py's
own autouse fixture, and no test here shells out or touches the network.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QSettings

from agent_takkub import (
    config,
    custom_roles,
    performance_settings,
    pipeline_config,
    settings_knowledge_design,
    settings_window,
    shared_dev_tools,
    team_preset,
    user_profile,
)
from agent_takkub import roles as roles_mod


@pytest.fixture(autouse=True)
def _isolate_settings_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Same redirects as test_settings_window.py's autouse fixture — every
    store SettingsWindow reads/writes lands under tmp_path."""
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(pipeline_config, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(pipeline_config, "_PATH", tmp_path / "pipelines.json")
    monkeypatch.setattr(team_preset, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(shared_dev_tools, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
    monkeypatch.setattr(user_profile, "_REGISTRY_PATH", tmp_path / "user-profiles.json")
    monkeypatch.setattr(user_profile, "_DEFAULT_CONFIG_DIR", tmp_path / "default-claude-config")
    monkeypatch.setattr(user_profile, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(performance_settings, "path", lambda: tmp_path / "performance.json")
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.delenv("TAKKUB_CONTEXT_STRATEGY", raising=False)
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


def _flush_deferred_deletes() -> None:
    """Run queued slots (a worker's `finished` → `_cleanup`) and then the
    DeferredDelete events `deleteLater()` posted — outside `exec()` the
    latter only drain through an explicit `sendPostedEvents`."""
    QCoreApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


# ─────────────────────────────────────────────────────────────────────
# settings_window.py:1641 — Save & Apply must not persist preset-rendered OFF
# ─────────────────────────────────────────────────────────────────────


class TestSaveApplyPersistsOnlyFlippedRoleToggles:
    def _open_roles_page(self, project: str) -> settings_window.SettingsWindow:
        return settings_window.SettingsWindow(
            project=project, initial_view=settings_window.VIEW_PROVIDERS_ROLES
        )

    @pytest.mark.parametrize("preset_id", ["solo-lead", "pair"])
    def test_provider_only_save_leaves_roles_enabled_untouched(self, preset_id: str) -> None:
        """The verifiers' repro: project on ทำเอง/คู่, change one provider,
        Save & Apply — pipelines.json must not gain rolesEnabled=False for
        positions the user never touched (they only RENDERED off because the
        preset excludes them)."""
        project = "proj-a"
        team_preset.set_current(preset_id, project)
        dlg = self._open_roles_page(project)
        # Sanity: the switches really do render OFF under this preset.
        assert all(
            dlg._role_toggles[r].isChecked() is False for r in team_preset.CORE_POSITION_ROLES
        )
        combo = dlg._role_provider_combos["backend"]
        combo.setCurrentIndex(combo.findData("codex"))

        dlg._on_save_apply_clicked()

        # `load()` normalizes rolesEnabled to every valid role (True unless
        # explicitly saved False) — the poison is any explicit False.
        assert pipeline_config.disabled_roles(project) == []
        for role in (*team_preset.CORE_POSITION_ROLES, "reviewer", "qa", "critic"):
            assert pipeline_config.is_role_enabled(role, project) is True, role
        # The preset itself is untouched — no false drift to "custom".
        assert team_preset.current_preset_id(project) == preset_id
        dlg.deleteLater()

    def test_switching_solo_lead_to_full_after_a_save_renders_positions_on(self) -> None:
        """Step 5b of the repro: after a provider-only save on solo-lead,
        picking ทีมเต็ม must render every core position ON and a Save must
        keep the project on "full" (not silently flip it to an all-off
        custom)."""
        project = "proj-b"
        team_preset.set_current("solo-lead", project)
        dlg = self._open_roles_page(project)
        combo = dlg._role_provider_combos["frontend"]
        combo.setCurrentIndex(combo.findData("codex"))
        dlg._on_save_apply_clicked()
        dlg.deleteLater()

        dlg2 = self._open_roles_page(project)
        dlg2._on_team_preset_card_clicked("full")
        for role in team_preset.CORE_POSITION_ROLES:
            assert dlg2._role_toggles[role].isChecked() is True, role
        dlg2._on_save_apply_clicked()

        assert team_preset.current_preset_id(project) == "full"
        for role in team_preset.CORE_POSITION_ROLES:
            assert team_preset.can_spawn(role, project)[0] is True, role
            assert pipeline_config.is_role_enabled(role, project) is True, role
        dlg2.deleteLater()

    def test_switching_solo_lead_to_auto_after_a_save_keeps_assign_allowed(self) -> None:
        """Step 5a of the repro: the chip says อัตโนมัติ, so `is_role_enabled`
        (what orchestrator/cli_server gate `takkub assign` on) must still be
        True for every position after an earlier solo-lead save."""
        project = "proj-c"
        team_preset.set_current("solo-lead", project)
        dlg = self._open_roles_page(project)
        combo = dlg._role_provider_combos["frontend"]
        combo.setCurrentIndex(combo.findData("codex"))
        dlg._on_save_apply_clicked()
        dlg.deleteLater()

        team_preset.set_current("auto", project)
        for role in team_preset.CORE_POSITION_ROLES:
            assert pipeline_config.is_role_enabled(role, project) is True, role
            assert team_preset.can_spawn(role, project)[0] is True, role

    def test_full_preset_save_does_not_persist_extras_as_disabled(self) -> None:
        """Same bug class one preset over: under ทีมเต็ม the extra positions
        (tester/analyst/…) render OFF because the preset excludes them —
        an untouched Save must not write rolesEnabled=False for them, or a
        later custom preset enabling one still reads OFF."""
        project = "proj-d"
        team_preset.set_current("full", project)
        dlg = self._open_roles_page(project)
        combo = dlg._role_provider_combos["backend"]
        combo.setCurrentIndex(combo.findData("codex"))
        dlg._on_save_apply_clicked()

        assert pipeline_config.disabled_roles(project) == []
        assert team_preset.current_preset_id(project) == "full"
        dlg.deleteLater()

    def test_hand_flipped_switch_is_still_persisted_and_flips_to_custom(self) -> None:
        """#512 acceptance must survive the fix: a switch the user really
        flipped (solo-lead → backend ON) still lands in rolesEnabled AND
        flips the project to custom."""
        project = "proj-e"
        team_preset.set_current("solo-lead", project)
        dlg = self._open_roles_page(project)
        dlg._role_toggles["backend"].setChecked(True)
        dlg._on_save_apply_clicked()

        assert pipeline_config.disabled_roles(project) == []
        assert pipeline_config.is_role_enabled("backend", project) is True
        cfg = team_preset.current(project)
        assert cfg["preset"] == "custom"
        assert cfg["roles"]["backend"] is True
        assert team_preset.can_spawn("backend", project)[0] is True
        dlg.deleteLater()

    def test_hand_disabled_switch_under_full_is_persisted_off(self) -> None:
        project = "proj-f"
        team_preset.set_current("full", project)
        dlg = self._open_roles_page(project)
        dlg._role_toggles["reviewer"].setChecked(False)
        dlg._on_save_apply_clicked()

        assert pipeline_config.disabled_roles(project) == ["reviewer"]
        assert pipeline_config.is_role_enabled("reviewer", project) is False
        dlg.deleteLater()

    def test_picking_full_heals_a_project_poisoned_before_the_fix(self) -> None:
        """2026-09-23 live test: the fix above only stops NEW poison. A real
        project saved under solo-lead before it already had every position +
        checker stored False — clicking ทีมเต็ม rendered all of them OFF and
        Save flipped the project to an all-off custom. Picking a card must let
        the preset decide and clear the stale OFFs it turns ON."""
        project = "proj-h"
        team_preset.set_current("solo-lead", project)
        poisoned = {
            r: False for r in (*team_preset.CORE_POSITION_ROLES, "reviewer", "qa", "critic")
        }
        payload = pipeline_config.load(project)
        payload["rolesEnabled"] = {**payload.get("rolesEnabled", {}), **poisoned}
        pipeline_config.save(payload, project)
        assert pipeline_config.is_role_enabled("backend", project) is False

        dlg = self._open_roles_page(project)
        dlg._on_team_preset_card_clicked("full")
        for role in team_preset.CORE_POSITION_ROLES:
            assert dlg._role_toggles[role].isChecked() is True, role
        dlg._on_save_apply_clicked()

        assert team_preset.current_preset_id(project) == "full"
        for role in team_preset.CORE_POSITION_ROLES:
            assert pipeline_config.is_role_enabled(role, project) is True, role
            assert team_preset.can_spawn(role, project)[0] is True, role
        dlg.deleteLater()

    def test_flip_and_flip_back_writes_nothing(self) -> None:
        project = "proj-g"
        team_preset.set_current("full", project)
        dlg = self._open_roles_page(project)
        dlg._role_toggles["backend"].setChecked(False)
        dlg._role_toggles["backend"].setChecked(True)
        dlg._on_save_apply_clicked()

        assert pipeline_config.disabled_roles(project) == []
        assert team_preset.current_preset_id(project) == "full"
        dlg.deleteLater()

    def test_baseline_follows_a_card_preview_rebuild(self) -> None:
        """Clicking a team-size card rebuilds the roster for the previewed
        preset; the baseline must track THAT render, so a solo-lead → full
        preview saved untouched writes no rolesEnabled either."""
        project = "proj-h"
        team_preset.set_current("solo-lead", project)
        dlg = self._open_roles_page(project)
        dlg._on_team_preset_card_clicked("full")
        assert dlg._role_toggle_baseline["backend"] is True
        dlg._on_save_apply_clicked()

        assert pipeline_config.disabled_roles(project) == []
        assert team_preset.current_preset_id(project) == "full"
        dlg.deleteLater()


# ─────────────────────────────────────────────────────────────────────
# settings_window.py:4376/4420 — autoskills threads must outlive the dialog
# ─────────────────────────────────────────────────────────────────────


class TestAutoskillsThreadsNotParentedToDialog:
    @pytest.fixture(autouse=True)
    def _isolate_skill_roots(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "no-bundle-here")
        monkeypatch.setattr(config, "ASSETS_ROOT", tmp_path / "no-bundle-here")
        monkeypatch.setattr(settings_window, "_allowed_project_roots", lambda _project: [tmp_path])

    def test_preview_thread_has_no_qt_parent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The #688 rule: a worker QThread must never be a child of the
        WA_DeleteOnClose SettingsWindow."""
        monkeypatch.setattr(settings_window._AutoskillsPreviewThread, "start", lambda self: None)
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._on_autoskills_scan_clicked()

        thread = dlg._as_preview_thread
        assert thread.parent() is None
        assert thread in settings_window._AUTOSKILLS_THREADS
        settings_window._AUTOSKILLS_THREADS.discard(thread)
        dlg.deleteLater()

    def test_install_thread_has_no_qt_parent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings_window._AutoskillsInstallThread, "start", lambda self: None)
        monkeypatch.setattr(
            settings_window._AutoskillsConfirmDialog,
            "exec",
            lambda self_dlg: settings_window.QDialog.DialogCode.Accepted,
        )
        candidate = settings_window.autoskills_installer.SkillCandidate(name="react-testing")
        result = settings_window.autoskills_installer.PreviewResult(ok=True, skills=[candidate])
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._on_autoskills_preview_ready(result)

        thread = dlg._as_install_thread
        assert thread.parent() is None
        assert thread in settings_window._AUTOSKILLS_THREADS
        settings_window._AUTOSKILLS_THREADS.discard(thread)
        dlg.deleteLater()

    def test_closing_settings_mid_scan_leaves_the_thread_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The verifiers' repro, hermetic: the scan blocks (a gated fake
        stands in for `npx autoskills`), the dialog is destroyed while it
        runs, and the process must survive — the thread keeps running
        detached, then cleans itself up once released."""
        release = threading.Event()

        def _blocking_preview(_root: Path):
            release.wait(10)
            return settings_window.autoskills_installer.PreviewResult(ok=True, skills=[])

        monkeypatch.setattr(settings_window.autoskills_installer, "preview", _blocking_preview)
        dlg = settings_window.SettingsWindow(
            project="demo", initial_view=settings_window.VIEW_SKILL_CATALOG
        )
        dlg._on_autoskills_scan_clicked()
        thread = dlg._as_preview_thread
        assert thread.isRunning()

        # What Cancel/Esc/X does to a WA_DeleteOnClose dialog, immediately.
        sip.delete(dlg)
        assert sip.isdeleted(dlg)
        assert not sip.isdeleted(thread)
        assert thread.isRunning()

        release.set()
        assert thread.wait(5000)
        _flush_deferred_deletes()
        assert thread not in settings_window._AUTOSKILLS_THREADS
        assert sip.isdeleted(thread)


# ─────────────────────────────────────────────────────────────────────
# settings_knowledge_design.py:295/422/473 — second click on Refresh/Test
# ─────────────────────────────────────────────────────────────────────


class TestKnowledgeDesignSecondClickSurvivesDeletedThread:
    @pytest.fixture(autouse=True)
    def _fake_collectors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            settings_knowledge_design,
            "_collect_knowledge_status",
            lambda _project: {
                "Brain": (True, "ok"),
                "Obsidian": (None, "-"),
                "Graft": (True, "ok"),
            },
        )
        monkeypatch.setattr(
            settings_knowledge_design,
            "_collect_design_tools_status",
            lambda _project: {
                "Storybook": (True, "ok"),
                "21st.dev": (None, "-"),
                "Figma": (None, "-"),
                "Penpot": (None, "-"),
            },
        )
        monkeypatch.setattr(settings_knowledge_design, "_run_design_tools_test", lambda: ["ok"])

    @staticmethod
    def _finish_and_delete(thread) -> None:
        """Drive the real lifecycle: worker finishes → queued `_cleanup`
        runs `deleteLater()` → DeferredDelete drains → wrapper is dead."""
        assert thread.wait(5000)
        _flush_deferred_deletes()
        assert sip.isdeleted(thread)

    def test_second_knowledge_refresh_starts_a_new_run(self) -> None:
        dlg = settings_window.SettingsWindow(
            project="proj", initial_view=settings_window.VIEW_KNOWLEDGE
        )
        dlg._on_kd_knowledge_refresh_clicked()
        first = dlg._kd_knowledge_thread
        self._finish_and_delete(first)

        dlg._on_kd_knowledge_refresh_clicked()  # used to raise RuntimeError here

        assert dlg._kd_knowledge_thread is not first
        assert dlg._kd_knowledge_thread.wait(5000)
        QCoreApplication.processEvents()
        assert dlg._kd_knowledge_rows["Brain"][1].text() == "ok"
        dlg.deleteLater()

    def test_second_design_refresh_starts_a_new_run(self) -> None:
        dlg = settings_window.SettingsWindow(
            project="proj", initial_view=settings_window.VIEW_KNOWLEDGE
        )
        dlg._on_kd_design_refresh_clicked()
        first = dlg._kd_design_thread
        self._finish_and_delete(first)

        dlg._on_kd_design_refresh_clicked()

        assert dlg._kd_design_thread is not first
        assert dlg._kd_design_thread.wait(5000)
        QCoreApplication.processEvents()
        assert dlg._kd_design_rows["Storybook"][1].text() == "ok"
        dlg.deleteLater()

    def test_test_button_after_a_finished_refresh_starts_a_new_run(self) -> None:
        """Refresh and Test share `_kd_design_thread`, so a finished Refresh
        used to kill the Test button too."""
        dlg = settings_window.SettingsWindow(
            project="proj", initial_view=settings_window.VIEW_KNOWLEDGE
        )
        dlg._on_kd_design_refresh_clicked()
        first = dlg._kd_design_thread
        self._finish_and_delete(first)

        dlg._on_kd_design_test_clicked()

        assert dlg._kd_design_thread is not first
        assert dlg._kd_design_thread.wait(5000)
        QCoreApplication.processEvents()
        assert dlg._kd_design_result.toPlainText() == "ok"
        assert dlg._kd_design_test_btn.isEnabled() is True
        dlg.deleteLater()

    def test_is_running_guard_reads_deleted_wrapper_as_idle(self) -> None:
        thread = settings_knowledge_design._CallableThread(lambda: None)
        thread.start()
        self._finish_and_delete(thread)
        assert settings_knowledge_design._is_running(thread) is False
        assert settings_knowledge_design._is_running(None) is False
