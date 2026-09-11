"""`core.migration.pre_migrate_backup` (#574) — the copy-only,
copy-verified pre-migration snapshot that runs first in the ladder, before
anything else is touched."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub.core.migration.backup import BackupManager
from agent_takkub.core.migration.journal import MigrationJournal
from agent_takkub.core.migration.pre_migrate_backup import (
    PreMigrateBackupStep,
    resolve_backup_dir,
    restore_from_backup_dir,
)
from agent_takkub.core.storage.jsonl_store import JsonlStore


@pytest.fixture
def journal_backups(tmp_path):
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    backups = BackupManager(tmp_path / "migration_backups")
    return journal, backups


def _seeded_data_home(tmp_path: Path) -> Path:
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "models").mkdir(parents=True)
    (data_home / "v2" / "models" / "registry.json").write_text("{}", encoding="utf-8")
    (data_home / "projects.json").write_text('{"a": {}}', encoding="utf-8")
    (data_home / "projects").mkdir(parents=True)
    (data_home / "projects" / "a" / "project.json").parent.mkdir(parents=True)
    (data_home / "projects" / "a" / "project.json").write_text("{}", encoding="utf-8")
    return data_home


def test_backs_up_every_input_category_at_least_once(tmp_path, journal_backups, monkeypatch):
    journal, backups = journal_backups
    monkeypatch.setattr(
        "agent_takkub.core.migration.pre_migrate_backup.migration_home", lambda: tmp_path / "mh"
    )
    data_home = _seeded_data_home(tmp_path)
    step = PreMigrateBackupStep(journal=journal, backups=backups, data_home=data_home)

    report = step.apply()
    assert report.ok, report.summary

    backup_dir = step._backup_dir()
    assert (backup_dir / "v2" / "models" / "registry.json").is_file()
    assert (backup_dir / "projects.json").is_file()
    assert (backup_dir / "projects" / "a" / "project.json").is_file()
    manifest = step._existing_manifest()
    assert manifest is not None
    assert len(manifest["items"]) >= 3

    # Sources are never touched — copy-only.
    assert (data_home / "v2" / "models" / "registry.json").is_file()
    assert (data_home / "projects.json").is_file()

    assert step.validate().ok


def test_nothing_to_back_up_is_a_clean_no_op(tmp_path, journal_backups, monkeypatch):
    journal, backups = journal_backups
    monkeypatch.setattr(
        "agent_takkub.core.migration.pre_migrate_backup.migration_home", lambda: tmp_path / "mh"
    )
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    step = PreMigrateBackupStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()
    assert report.ok
    assert step.validate().ok


def test_a_failed_backup_never_touches_anything_else_in_the_ladder(tmp_path, monkeypatch):
    """The whole ladder aborts on this step's own failure, before any other
    step runs — achieved purely by ladder POSITION (first), since
    `MigrationEngine.apply()`/`apply_pending()` already stop at the first
    non-ok step."""
    monkeypatch.setattr(
        "agent_takkub.core.migration.pre_migrate_backup.migration_home", lambda: tmp_path / "mh"
    )
    data_home = _seeded_data_home(tmp_path)
    # VersionMarkerStep (ladder position 2) has no `data_home` field of its
    # own — it resolves via `config.DATA_HOME` directly, so a full-ladder
    # `MigrationEngine` test must redirect that globally too, never just
    # pass `data_home=` to the constructor (that alone leaves this step
    # pointed at the REAL machine's data).
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)
    monkeypatch.setattr("agent_takkub.config.SETTINGS_HOME", data_home)
    monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", data_home / "runtime")

    import agent_takkub.core.migration.pre_migrate_backup as backup_mod

    def _boom(*a, **k):
        from agent_takkub.core.migration.promote_v1 import CopyOutcome

        return CopyOutcome(ok=False, error="simulated disk-full mid-backup")

    monkeypatch.setattr(backup_mod, "_copy_phase", _boom)

    from agent_takkub.core.migration.engine import MigrationEngine

    engine = MigrationEngine(data_home=data_home)
    reports = engine.apply()

    assert len(reports) == 1
    assert reports[0].step_id == "pre-migrate-backup"
    assert not reports[0].ok
    # `version-marker` (ladder position 2) never ran at all.
    from agent_takkub.core.versioning.store import version_doc_path

    assert not version_doc_path().exists()
    # The legacy v2/ root is untouched — promote-v2-root never ran either.
    assert (data_home / "v2" / "models" / "registry.json").is_file()


def test_resumed_apply_skips_real_copy_once_manifest_is_already_complete(
    tmp_path, journal_backups, monkeypatch
):
    journal, backups = journal_backups
    monkeypatch.setattr(
        "agent_takkub.core.migration.pre_migrate_backup.migration_home", lambda: tmp_path / "mh"
    )
    data_home = _seeded_data_home(tmp_path)
    step = PreMigrateBackupStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok

    import agent_takkub.core.migration.pre_migrate_backup as backup_mod

    def _fail_if_called(*a, **k):
        raise AssertionError("_copy_phase must not run again once the manifest is complete")

    monkeypatch.setattr(backup_mod, "_copy_phase", _fail_if_called)

    retry = PreMigrateBackupStep(journal=journal, backups=backups, data_home=data_home)
    report = retry.apply()
    assert report.ok
    assert "resumed" in report.summary


def test_rollback_never_deletes_the_backup(tmp_path, journal_backups, monkeypatch):
    journal, backups = journal_backups
    monkeypatch.setattr(
        "agent_takkub.core.migration.pre_migrate_backup.migration_home", lambda: tmp_path / "mh"
    )
    data_home = _seeded_data_home(tmp_path)
    step = PreMigrateBackupStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok
    backup_dir = step._backup_dir()

    report = step.rollback()
    assert report.ok
    assert backup_dir.is_dir()
    assert (backup_dir / "projects.json").is_file()


def test_resolve_backup_dir_is_stable_across_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_takkub.core.migration.pre_migrate_backup.migration_home", lambda: tmp_path / "mh"
    )
    data_home = tmp_path / "data_home"
    first = resolve_backup_dir(data_home)
    second = resolve_backup_dir(data_home)
    assert first == second


def test_restore_from_backup_dir_copies_items_back(tmp_path, journal_backups, monkeypatch):
    journal, backups = journal_backups
    monkeypatch.setattr(
        "agent_takkub.core.migration.pre_migrate_backup.migration_home", lambda: tmp_path / "mh"
    )
    data_home = _seeded_data_home(tmp_path)
    step = PreMigrateBackupStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok
    backup_dir = step._backup_dir()

    restore_target = tmp_path / "restored_data_home"
    restore_target.mkdir()
    report = restore_from_backup_dir(backup_dir, restore_target, backups=backups)
    assert report.ok, report.summary
    assert (restore_target / "projects.json").is_file()
    assert (restore_target / "v2" / "models" / "registry.json").is_file()


def test_restore_from_backup_dir_fails_cleanly_without_a_manifest(tmp_path):
    empty_dir = tmp_path / "no-manifest-here"
    empty_dir.mkdir()
    report = restore_from_backup_dir(empty_dir, tmp_path / "target")
    assert not report.ok
