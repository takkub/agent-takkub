"""Tests for `agent_takkub.openviking_setup_dialog`: the OpenViking Setup
Wizard (Wave 2, `07_SETUP_WIZARD.md`). No real install/spawn/subprocess
ever runs — `manager.get_manager()` and `subprocess.run` are stubbed
throughout, matching `test_openviking_manager.py`'s own convention."""

from __future__ import annotations

import json
import subprocess

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config, openviking_settings
from agent_takkub import openviking_setup_dialog as dlg_mod
from agent_takkub.openviking import installer


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    home = tmp_path / "ov-home"
    monkeypatch.setattr(installer, "OPENVIKING_HOME", home)
    monkeypatch.setattr(installer, "VENV_DIR", home / "venv")
    monkeypatch.setattr(installer, "CONFIG_DIR", home / "config")
    monkeypatch.setattr(installer, "CONFIG_FILE", home / "config" / "ov.conf")
    monkeypatch.setattr(installer, "STATE_FILE", home / "state.json")
    monkeypatch.setattr(installer, "DATA_DIR", home / "data")
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    monkeypatch.setattr(dlg_mod.ov_manager, "_instance", None)


def _wait(thread) -> None:
    assert thread is not None
    thread.wait(5000)
    QCoreApplication.processEvents()


class TestBuildOvConf:
    def test_omits_optional_fields_when_blank(self) -> None:
        conf = dlg_mod.build_ov_conf(
            embedding_provider="ollama",
            embedding_api_base="",
            embedding_api_key="",
            embedding_model="nomic-embed-text",
            vlm_provider="ollama",
            vlm_model="llava",
            workspace_dir="/tmp/data",
            port=1933,
        )
        assert conf["embedding"]["dense"] == {"provider": "ollama", "model": "nomic-embed-text"}
        assert conf["vlm"] == {"provider": "ollama", "model": "llava"}
        assert conf["storage"]["workspace"] == "/tmp/data"
        assert conf["server"] == {"host": "127.0.0.1", "port": 1933}

    def test_includes_api_base_and_key_when_present(self) -> None:
        conf = dlg_mod.build_ov_conf(
            embedding_provider="volcengine",
            embedding_api_base="https://ark.example/api/v3",
            embedding_api_key="secret-123",
            embedding_model="doubao-embedding",
            vlm_provider="volcengine",
            vlm_model="doubao-seed",
            workspace_dir="/data",
            port=1933,
        )
        assert conf["embedding"]["dense"]["api_base"] == "https://ark.example/api/v3"
        assert conf["embedding"]["dense"]["api_key"] == "secret-123"
        assert conf["vlm"]["api_base"] == "https://ark.example/api/v3"
        assert conf["vlm"]["api_key"] == "secret-123"


class TestWriteOvConf:
    def test_writes_json_to_config_file(self) -> None:
        dlg_mod.write_ov_conf({"a": 1})
        assert json.loads(installer.CONFIG_FILE.read_text(encoding="utf-8")) == {"a": 1}


class TestRunDoctor:
    def test_raises_when_not_installed(self, tmp_path) -> None:
        with pytest.raises(RuntimeError, match="ยังไม่ได้ติดตั้ง"):
            dlg_mod.run_doctor(tmp_path / "ov.conf")

    def test_returns_stdout_when_installed(self, monkeypatch, tmp_path) -> None:
        exe = installer.server_executable()
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n"),
        )
        result = dlg_mod.run_doctor(tmp_path / "ov.conf")
        assert result == "ok\n"


class TestDialogWidget:
    def test_constructs_headless(self) -> None:
        dlg = dlg_mod.OpenVikingSetupDialog()
        assert dlg._embed_provider_combo.count() == len(dlg_mod.EMBEDDING_PROVIDERS)
        assert dlg._vlm_provider_combo.count() == len(dlg_mod.VLM_PROVIDERS)
        dlg.deleteLater()

    def test_test_configuration_writes_config_and_runs_doctor(self, monkeypatch) -> None:
        doctor_calls = []
        monkeypatch.setattr(
            dlg_mod, "run_doctor", lambda path: doctor_calls.append(path) or "healthy"
        )
        dlg = dlg_mod.OpenVikingSetupDialog()
        dlg._embed_model_edit.setText("my-embed-model")
        dlg._on_test_clicked()
        _wait(dlg._doctor_thread)

        assert doctor_calls == [installer.CONFIG_FILE]
        assert "healthy" in dlg._result_box.toPlainText()
        written = json.loads(installer.CONFIG_FILE.read_text(encoding="utf-8"))
        assert written["embedding"]["dense"]["model"] == "my-embed-model"
        dlg.deleteLater()

    def test_install_start_success_marks_enabled_and_saves_settings(self, monkeypatch) -> None:
        class _FakeManager:
            def ensure_installed(self):
                return True

            def start(self):
                return dlg_mod.ov_manager.ManagerStatus(
                    True, True, True, "http://127.0.0.1:1933", True
                )

        monkeypatch.setattr(dlg_mod.ov_manager, "get_manager", lambda: _FakeManager())
        monkeypatch.delenv(dlg_mod.openviking_adapter._ENV_ENABLED, raising=False)

        dlg = dlg_mod.OpenVikingSetupDialog()
        assert dlg._enable_check.isChecked() is True
        dlg._on_install_clicked()
        _wait(dlg._install_thread)

        assert "127.0.0.1:1933" in dlg._result_box.toPlainText()
        cfg = openviking_settings.load()
        assert cfg.enabled is True
        assert cfg.start_automatically is True
        dlg.deleteLater()

    def test_install_start_unchecked_enable_does_not_persist(self, monkeypatch) -> None:
        class _FakeManager:
            def ensure_installed(self):
                return True

            def start(self):
                return dlg_mod.ov_manager.ManagerStatus(
                    True, True, True, "http://127.0.0.1:1933", True
                )

        monkeypatch.setattr(dlg_mod.ov_manager, "get_manager", lambda: _FakeManager())

        dlg = dlg_mod.OpenVikingSetupDialog()
        dlg._enable_check.setChecked(False)
        dlg._on_install_clicked()
        _wait(dlg._install_thread)

        cfg = openviking_settings.load()
        assert cfg.enabled is False
        assert cfg.start_automatically is False
        dlg.deleteLater()

    def test_install_start_failure_surfaces_error(self, monkeypatch) -> None:
        class _FakeManager:
            def ensure_installed(self):
                return False

        monkeypatch.setattr(dlg_mod.ov_manager, "get_manager", lambda: _FakeManager())

        dlg = dlg_mod.OpenVikingSetupDialog()
        dlg._on_install_clicked()
        _wait(dlg._install_thread)

        assert "ไม่สำเร็จ" in dlg._progress_lbl.text()
        dlg.deleteLater()
