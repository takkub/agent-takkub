"""Tests for app._boot_main_window — the TAKKUB_BOOT_UPDATE on/off switch
that decides whether MainWindow is gated behind the provider-update splash.

Import order note: agent_takkub.app is imported at module level so
QtWebEngineWidgets loads before any QCoreApplication is created (mirrors
tests/test_cli_bin_check.py).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import agent_takkub.app as app_mod


class TestBootMainWindowGate:
    def test_boot_update_zero_skips_the_gate_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_BOOT_UPDATE", "0")
        sentinel = object()
        monkeypatch.setattr(app_mod, "MainWindow", lambda: sentinel)
        gate = MagicMock()
        monkeypatch.setattr("agent_takkub.boot_update_window.run_boot_update_gate", gate)
        # #361: no provider-update splash to piggyback progress on, but the
        # storage-layout auto-migrate stage must still run headlessly (never
        # skipped just because the splash is off) — stub it so this test
        # never touches this machine's real DATA_HOME/SETTINGS_HOME.
        auto_migrate = MagicMock()
        monkeypatch.setattr("agent_takkub.auto_migrate_boot.run_boot_stage", auto_migrate)
        result = app_mod._boot_main_window()
        assert result is sentinel
        gate.assert_not_called()
        auto_migrate.assert_called_once()

    def test_boot_update_zero_never_lets_a_stage_error_block_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_BOOT_UPDATE", "0")
        sentinel = object()
        monkeypatch.setattr(app_mod, "MainWindow", lambda: sentinel)
        monkeypatch.setattr("agent_takkub.boot_update_window.run_boot_update_gate", MagicMock())

        def _boom(progress_cb=None):
            raise RuntimeError("boom")

        monkeypatch.setattr("agent_takkub.auto_migrate_boot.run_boot_stage", _boom)
        result = app_mod._boot_main_window()
        assert result is sentinel

    def test_default_routes_through_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_BOOT_UPDATE", raising=False)
        sentinel = object()
        gate = MagicMock(return_value=sentinel)
        monkeypatch.setattr("agent_takkub.boot_update_window.run_boot_update_gate", gate)
        result = app_mod._boot_main_window()
        assert result is sentinel
        gate.assert_called_once_with(app_mod.MainWindow)
