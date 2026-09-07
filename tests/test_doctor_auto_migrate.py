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


class TestAutoMigratePendingFindings:
    """#362: `apply_pending()`'s two mixed-state-only outcomes each get
    their own WARN finding, independent of the base auto-migrate finding
    above."""

    def test_stale_applied_steps_reports_warn(self) -> None:
        (config.SETTINGS_HOME / "auto-migrate-state.json").write_text(
            json.dumps({"applied_version": "1.1.0", "stale_applied_steps": {"state": "mismatch"}}),
            encoding="utf-8",
        )
        f = _finding(name="auto-migrate-stale")
        assert f.status == doctor.Status.WARN
        assert "state" in f.detail

    def test_no_stale_steps_omits_the_finding(self) -> None:
        findings = doctor.check_storage_layout_state()
        assert not [f for f in findings if f.name == "auto-migrate-stale"]

    def test_pending_rollback_guard_reports_warn(self) -> None:
        (config.SETTINGS_HOME / "auto-migrate-state.json").write_text(
            json.dumps(
                {"applied_version": "1.1.0", "rolled_back_steps": {"core-internal-store": "1.1.0"}}
            ),
            encoding="utf-8",
        )
        f = _finding(name="auto-migrate-pending-rollback")
        assert f.status == doctor.Status.WARN
        assert "core-internal-store" in f.detail

    def test_no_guarded_steps_omits_the_finding(self) -> None:
        findings = doctor.check_storage_layout_state()
        assert not [f for f in findings if f.name == "auto-migrate-pending-rollback"]


class TestV1OnlyWriteFinding:
    """#502 — a V1 file written more recently than its dual-write mirror
    means some writer skipped `core.storage.dual_write`; exit criteria for
    #504's V1 removal is this at 0 alongside `model_pin_v2_drift`."""

    def test_not_migrated_reports_ok_zero(self) -> None:
        f = _finding(name="v1-only-write")
        assert f.status == doctor.Status.OK
        assert "0" in f.detail

    def test_stale_v1_source_reports_warn(self) -> None:
        import os
        import time

        from agent_takkub.core.migration.steps_v1 import build_readonly_registries_step

        (config.DATA_HOME / "v2").mkdir(parents=True, exist_ok=True)
        mapping = build_readonly_registries_step(data_home=config.DATA_HOME).mappings
        source = next(m.source for m in mapping if m.name == "provider-models")
        target = next(m.target for m in mapping if m.name == "provider-models")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"schema": 1, "data": {}}', encoding="utf-8")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('{"claude": "sonnet"}', encoding="utf-8")
        future = time.time() + 100
        os.utime(source, (future, future))

        f = _finding(name="v1-only-write")
        assert f.status == doctor.Status.WARN
        assert "provider-models" in f.detail

        # `_log_event` resolves its target through the orchestrator façade
        # (`orchestrator_text._orch_attr`), which `conftest.py`'s own
        # autouse isolation already redirects to a per-test tmp path — read
        # back through the same module rather than `config.RUNTIME_DIR`
        # (a different tmp dir this file's own `_isolate_paths` fixture
        # points at, only for the DATA_HOME/SETTINGS_HOME scan itself).
        from agent_takkub import orchestrator_text

        assert orchestrator_text.EVENTS_LOG.exists()
        assert "v1_only_write" in orchestrator_text.EVENTS_LOG.read_text(encoding="utf-8")
