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


class TestMixedLayoutStateFinding:
    """#566/M1: `layout_state() == "mixed"` means two different things —
    a dev checkout's permanent, intentional nested `v2/` root, and an
    installed machine that still has V1 leftovers (including the #566
    "a live writer resurrected projects.json" case). Doctor must tell
    them apart instead of calling both "expected, not a problem"."""

    def _seed_mixed(self) -> None:
        (config.DATA_HOME / "v2").mkdir(parents=True, exist_ok=True)

    def test_installed_mixed_reports_warn(self) -> None:
        self._seed_mixed()
        f = _finding(name="legacy-leftover")
        assert f.status == doctor.Status.WARN

    def test_dev_checkout_mixed_reports_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", config.DATA_HOME)
        self._seed_mixed()
        f = _finding(name="legacy-leftover")
        assert f.status == doctor.Status.OK
        assert "dev checkout" in f.detail


class TestPendingDuplicateFinding:
    """#504 round5 R5-M1 (`duplicate_doctor_visibility`): `_prune_failure_
    summary` tells the operator a denied prune left a named DUPLICATE and
    that `takkub doctor --storage-layout` surfaces it — before this fix
    nothing here ever read either manifest, so the only signal was the
    generic mixed-layout WARN, which names no file and is downgraded to
    OK on a dev checkout."""

    def _apply_with_denied_state_prune(self) -> None:
        from unittest.mock import patch

        import agent_takkub.core.migration.promote_v1 as promote_mod
        from agent_takkub.core.migration.backup import BackupManager
        from agent_takkub.core.migration.journal import MigrationJournal
        from agent_takkub.core.storage.jsonl_store import JsonlStore

        (config.DATA_HOME / "v2" / "models").mkdir(parents=True)
        (config.DATA_HOME / "v2" / "models" / "a.json").write_text("A", encoding="utf-8")
        (config.DATA_HOME / "v2" / "state").mkdir(parents=True)
        (config.DATA_HOME / "v2" / "state" / "b.json").write_text("B", encoding="utf-8")
        journal = MigrationJournal(JsonlStore(config.DATA_HOME.parent / "journal.jsonl"))
        backups = BackupManager(config.DATA_HOME.parent / "step-backups")
        step = promote_mod.PromoteV2RootStep(
            journal=journal, backups=backups, data_home=config.DATA_HOME
        )
        real_remove = promote_mod._remove
        denied = config.DATA_HOME / "v2" / "state"

        def _deny(path):
            if path == denied:
                raise PermissionError("prune denied")
            return real_remove(path)

        with patch.object(promote_mod, "_remove", side_effect=_deny):
            report = step.apply()
        assert not report.ok
        assert "DUPLICATE" in report.summary

    def test_pending_duplicate_reports_warn_naming_the_file(self) -> None:
        self._apply_with_denied_state_prune()
        findings = doctor.check_storage_layout_state()
        warns = [f for f in findings if f.name == "duplicate"]
        assert warns, [f.name for f in findings]
        assert warns[0].status == doctor.Status.WARN
        assert "b.json" in warns[0].detail
        assert "DUPLICATE" in warns[0].detail

    def test_pending_duplicate_reports_even_on_a_dev_checkout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dev checkout downgrades the generic mixed-layout WARN to OK
        (`TestMixedLayoutStateFinding`) — the duplicate-specific finding
        must stay a WARN regardless, since a live checkout with a pending
        DUPLICATE is just as broken as an installed one."""
        monkeypatch.setattr(config, "REPO_ROOT", config.DATA_HOME)
        self._apply_with_denied_state_prune()
        f = _finding(name="duplicate")
        assert f.status == doctor.Status.WARN

    def test_no_duplicate_omits_the_finding(self) -> None:
        findings = doctor.check_storage_layout_state()
        assert not [f for f in findings if f.name == "duplicate"]


class TestV2AuthorityRetirementFinding:
    """#504 cut half: dual-write/v1-only-write drift telemetry is gone
    (nothing left to compare — every domain reads/writes its `v2/` target
    directly). `TAKKUB_V2_AUTHORITY` is retired; this finding only flags a
    leftover env override so an operator can clean it up."""

    def test_no_env_override_reports_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_V2_AUTHORITY", raising=False)
        f = _finding(name="authority")
        assert f.status == doctor.Status.OK

    def test_leftover_env_override_reports_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_V2_AUTHORITY", "0")
        f = _finding(name="authority")
        assert f.status == doctor.Status.WARN
        assert "TAKKUB_V2_AUTHORITY" in f.detail
        assert "#504" in f.detail
