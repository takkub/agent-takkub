"""Tests for app._check_cli_bin_present (installed-build CLI binary breadcrumb).

Import order is intentional: agent_takkub.app is imported at module level so
QtWebEngineWidgets is loaded before any QCoreApplication is created (mirrors
tests/test_single_instance_watchdog.py).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import agent_takkub.app as app_mod
from agent_takkub import config


class TestCheckCliBinPresent:
    def test_dev_checkout_skips_check_entirely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "is_installed_package", lambda: False)
        logged = MagicMock()
        monkeypatch.setattr("agent_takkub.orchestrator._log_event", logged, raising=False)
        app_mod._check_cli_bin_present()
        logged.assert_not_called()

    def test_installed_with_binary_present_logs_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "is_installed_package", lambda: True)
        monkeypatch.setattr(config, "CLI_BIN_DIR", tmp_path)
        monkeypatch.setattr(sys, "platform", "win32")
        (tmp_path / "takkub.exe").write_text("", encoding="utf-8")
        logged = MagicMock()
        monkeypatch.setattr("agent_takkub.orchestrator._log_event", logged, raising=False)
        app_mod._check_cli_bin_present()
        logged.assert_not_called()

    def test_installed_without_binary_logs_cli_bin_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "is_installed_package", lambda: True)
        empty_dir = tmp_path / "Scripts"
        monkeypatch.setattr(config, "CLI_BIN_DIR", empty_dir)
        monkeypatch.setattr(sys, "platform", "win32")
        logged = MagicMock()
        monkeypatch.setattr("agent_takkub.orchestrator._log_event", logged, raising=False)
        app_mod._check_cli_bin_present()
        logged.assert_called_once_with("cli_bin_missing", cli_bin_dir=str(empty_dir))

    def test_installed_posix_checks_extensionless_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "is_installed_package", lambda: True)
        monkeypatch.setattr(config, "CLI_BIN_DIR", tmp_path)
        monkeypatch.setattr(sys, "platform", "darwin")
        (tmp_path / "takkub").write_text("", encoding="utf-8")
        logged = MagicMock()
        monkeypatch.setattr("agent_takkub.orchestrator._log_event", logged, raising=False)
        app_mod._check_cli_bin_present()
        logged.assert_not_called()

    def test_never_raises_if_log_event_import_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "is_installed_package", lambda: True)
        monkeypatch.setattr(config, "CLI_BIN_DIR", tmp_path / "missing")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "agent_takkub.orchestrator", None)
        app_mod._check_cli_bin_present()  # must not raise
