"""`takkub doctor --storage-layout` reporting the boot-time auto-migrate
gate's last outcome (#361)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import config, doctor


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_home = tmp_path / "data_home"
    settings_home = tmp_path / "settings_home"
    data_home.mkdir()
    settings_home.mkdir()
    monkeypatch.setattr(config, "DATA_HOME", data_home)
    monkeypatch.setattr(config, "SETTINGS_HOME", settings_home)
    monkeypatch.setattr(config, "RUNTIME_DIR", data_home / "runtime")
    yield


def _finding(*, category: str = "storage-layout", name: str = "auto-migrate"):
    findings = doctor.check_storage_layout_state()
    return next(f for f in findings if f.category == category and f.name == name)


class TestAutoMigrateBootFinding:
    def test_never_ran_reports_info(self) -> None:
        f = _finding()
        assert f.status == doctor.Status.INFO
        assert "ยังไม่เคยรัน" in f.detail

    def test_applied_reports_ok_with_version(self) -> None:
        (config.SETTINGS_HOME / "auto-migrate-state.json").write_text(
            json.dumps({"applied_version": "1.1.0"}), encoding="utf-8"
        )
        f = _finding()
        assert f.status == doctor.Status.OK
        assert "1.1.0" in f.detail

    def test_rolled_back_reports_warn_with_version(self) -> None:
        (config.SETTINGS_HOME / "auto-migrate-state.json").write_text(
            json.dumps({"rolled_back_for_version": "1.1.0"}), encoding="utf-8"
        )
        f = _finding()
        assert f.status == doctor.Status.WARN
        assert "1.1.0" in f.detail

    def test_rolled_back_surfaces_pending_local_issue_backlog(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#361 design note: if `gh` was unavailable and the
        `auto_migrate_rolled_back` signal fell back to the local
        `.takkub_issues.json` store, doctor must say so — otherwise an
        unattended machine's rollback never reaches anyone."""
        (config.SETTINGS_HOME / "auto-migrate-state.json").write_text(
            json.dumps({"rolled_back_for_version": "1.1.0"}), encoding="utf-8"
        )
        from agent_takkub import maintenance

        monkeypatch.setattr(
            maintenance,
            "check_local_issue_backlog",
            lambda: maintenance.Check(
                "local_issues", "Issue ที่ค้างในเครื่อง", "attention", "1 issue ค้างส่ง"
            ),
        )
        f = _finding()
        assert "1 issue ค้างส่ง" in f.detail
