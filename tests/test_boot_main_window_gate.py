"""Tests for app._boot_main_window — the TAKKUB_BOOT_UPDATE on/off switch
that decides whether MainWindow is gated behind the boot-flow wizard (#574,
`boot_flow_window.py` — replaced the old provider-update-only splash in
`boot_update_window.py`).

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
        monkeypatch.setattr("agent_takkub.boot_flow_window.run_boot_flow_gate", gate)
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
        monkeypatch.setattr("agent_takkub.boot_flow_window.run_boot_flow_gate", MagicMock())

        def _boom(progress_cb=None):
            raise RuntimeError("boom")

        monkeypatch.setattr("agent_takkub.auto_migrate_boot.run_boot_stage", _boom)
        result = app_mod._boot_main_window()
        assert result is sentinel

    def test_default_routes_through_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_BOOT_UPDATE", raising=False)
        sentinel = object()
        gate = MagicMock(return_value=sentinel)
        monkeypatch.setattr("agent_takkub.boot_flow_window.run_boot_flow_gate", gate)
        result = app_mod._boot_main_window()
        assert result is sentinel
        # #631: the gate must be given the boot-phase quit-request predicate so
        # a Ctrl+C during the wizard aborts boot instead of being swallowed.
        assert gate.call_args.args == (app_mod.MainWindow,)
        assert callable(gate.call_args.kwargs["quit_requested"])

    def test_boot_phase_quit_request_disarms_boot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#631: a Ctrl+C during the boot-phase signal-window (before
        `_install_signal_handlers`) must set `_quit_requested` and the
        predicate handed to the boot-flow gate must report it, so the wizard
        aborts the boot instead of surfacing a swallowed KeyboardInterrupt."""
        monkeypatch.delenv("TAKKUB_BOOT_UPDATE", raising=False)
        monkeypatch.setattr(app_mod, "_quit_requested", False)
        monkeypatch.setattr(
            app_mod, "QApplication", type("QApp", (), {"instance": staticmethod(lambda: None)})
        )
        captured: dict = {}

        def _fake_gate(factory, quit_requested=None):
            captured["factory"] = factory
            captured["quit_requested"] = quit_requested

        monkeypatch.setattr("agent_takkub.boot_flow_window.run_boot_flow_gate", _fake_gate)
        monkeypatch.setattr(app_mod, "MainWindow", object)
        app_mod._boot_main_window()
        assert captured["quit_requested"]() is False
        app_mod._request_quit()
        assert app_mod._quit_requested is True
        assert captured["quit_requested"]() is True
