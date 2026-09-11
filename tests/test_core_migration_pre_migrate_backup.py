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
    """#574 round11 item 1 narrowed this step's scope to only what a later
    ladder step might overwrite/merge/delete — seed exactly those 3
    categories `_input_entries()` now covers, plus one pure-move item
    (`_shared_dir_legacy_candidates` untouched here) so tests can assert it
    is deliberately left OUT."""
    from agent_takkub.core.storage.layout import storage_layout_v2

    data_home = tmp_path / "data_home"
    data_home.mkdir(parents=True)

    # 1) promote-v2-root MERGE collision: a `v2/models/*` candidate whose
    # top-level `models/` destination already holds different content.
    (data_home / "v2" / "models").mkdir(parents=True)
    (data_home / "v2" / "models" / "registry.json").write_text('{"from": "v2"}', encoding="utf-8")
    (data_home / "models").mkdir(parents=True)
    (data_home / "models" / "registry.json").write_text('{"pre_existing": true}', encoding="utf-8")

    # 2) a domain step's own V2 target, already holding pre-existing
    # content before this pass would write over it (state step's
    # local-issues mapping).
    layout = storage_layout_v2(data_home)
    layout.state_issues.mkdir(parents=True, exist_ok=True)
    (layout.state_issues / "local.json").write_text('{"pre_existing": true}', encoding="utf-8")

    # 3) #504 item 5 junk deleted outright by archive-v1-legacy — no other
    # copy anywhere else, must be backed up here.
    (data_home / "openviking").write_text("junk", encoding="utf-8")

    # A genuine V1 top-level leftover archive-v1-legacy will MOVE (copy-
    # verify then prune) — never backed up here (its own WAL protects it).
    (data_home / "some-old-v1-file.txt").write_text("legacy", encoding="utf-8")

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
    assert (backup_dir / "models" / "registry.json").is_file()
    assert (backup_dir / "state" / "issues" / "local.json").is_file()
    assert (backup_dir / "openviking").is_file()
    manifest = step._existing_manifest()
    assert manifest is not None
    assert len(manifest["items"]) >= 3

    # Sources are never touched — copy-only — and the pre-existing content
    # at the MERGE/target destinations is exactly what got preserved (not
    # the v2/other-source content it's about to be merged/overwritten by).
    assert (data_home / "models" / "registry.json").read_text(encoding="utf-8") == (
        '{"pre_existing": true}'
    )
    assert (data_home / "openviking").is_file()

    # #574 round11 item 1: the pure-move V1 leftover is deliberately never
    # backed up here — surfaced instead via `skipped_move_only_items()`.
    assert not (backup_dir / "some-old-v1-file.txt").exists()
    skipped_names = [n for n, _reason in step.skipped_move_only_items()]
    assert "some-old-v1-file.txt" in skipped_names

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


def test_resumed_apply_recopies_a_payload_deleted_out_from_under_the_manifest(
    tmp_path, journal_backups, monkeypatch
):
    """#504/#574 R8-H2: `_already_backed_up()` used to match manifest NAMES
    only — delete one recorded payload and `apply()` still reported
    "already backed up (resumed)" without ever re-copying it, the ladder
    then walking on with one fewer file actually protected than the
    manifest claimed."""
    journal, backups = journal_backups
    monkeypatch.setattr(
        "agent_takkub.core.migration.pre_migrate_backup.migration_home", lambda: tmp_path / "mh"
    )
    data_home = _seeded_data_home(tmp_path)
    step = PreMigrateBackupStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok

    backup_dir = step._backup_dir()
    payload = backup_dir / "openviking"
    assert payload.is_file()
    payload.unlink()

    retry = PreMigrateBackupStep(journal=journal, backups=backups, data_home=data_home)
    report = retry.apply()
    assert report.ok, report.summary
    assert "already backed up" not in report.summary
    assert payload.is_file()  # actually re-copied, not just claimed


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
    assert (backup_dir / "openviking").is_file()


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
    assert (restore_target / "openviking").is_file()
    assert (restore_target / "models" / "registry.json").read_text(encoding="utf-8") == (
        '{"pre_existing": true}'
    )
    assert (restore_target / "state" / "issues" / "local.json").is_file()


def test_restore_from_backup_dir_fails_cleanly_without_a_manifest(tmp_path):
    empty_dir = tmp_path / "no-manifest-here"
    empty_dir.mkdir()
    report = restore_from_backup_dir(empty_dir, tmp_path / "target")
    assert not report.ok


# ---------------------------------------------------------------------------
# #574 round11 item 4 — resume across a scope-narrowing code version, never
# assuming `_input_entries()`'s entry set is stable across versions.
# ---------------------------------------------------------------------------


def test_scope_narrowing_keeps_out_of_scope_items_flagged_not_dropped(
    tmp_path, journal_backups, monkeypatch
):
    """Simulates a code upgrade that narrows `_input_entries()`'s scope
    (this very round's own change) landing on a machine with an OLDER,
    wider-scope backup already on disk: the item that fell out of scope
    must stay recorded (flagged `out_of_scope`), its already-copied backup
    files must never be deleted, and a fresh `apply()` under the NEW
    (narrower) scope must not re-require the old, wider entry set to
    consider itself already backed up."""
    journal, backups = journal_backups
    monkeypatch.setattr(
        "agent_takkub.core.migration.pre_migrate_backup.migration_home", lambda: tmp_path / "mh"
    )
    data_home = _seeded_data_home(tmp_path)
    step = PreMigrateBackupStep(journal=journal, backups=backups, data_home=data_home)

    # Simulate an OLDER, wider-scope run having already recorded (and
    # copied) one extra item this version's `_input_entries()` no longer
    # produces at all.
    real_input_entries = step._input_entries
    old_scope_extra_name = "an-old-scope-only-item.txt"

    def _wider_entries():
        from agent_takkub.core.migration.promote_v1 import TransferEntry

        entries = real_input_entries()
        src = data_home / old_scope_extra_name
        src.write_text("old-scope-content", encoding="utf-8")
        entries.append(
            TransferEntry(
                old_scope_extra_name, "file", src, step._backup_dir() / old_scope_extra_name
            )
        )
        return entries

    monkeypatch.setattr(step, "_input_entries", _wider_entries)
    assert step.apply().ok
    backup_dir = step._backup_dir()
    assert (backup_dir / old_scope_extra_name).is_file()

    # Now the code "upgrades" back to the real (narrower) `_input_entries`
    # — the extra item is out of scope from here on.
    monkeypatch.setattr(step, "_input_entries", real_input_entries)

    manifest_before = step._existing_manifest()
    assert any(item["name"] == old_scope_extra_name for item in manifest_before["items"])

    fresh = PreMigrateBackupStep(journal=journal, backups=backups, data_home=data_home)
    report = fresh.apply()
    assert report.ok, report.summary
    assert "resumed" in report.summary

    # The old-scope item's backup file is untouched — never deleted.
    assert (backup_dir / old_scope_extra_name).is_file()
    manifest_after = fresh._existing_manifest()
    old_item = next(i for i in manifest_after["items"] if i["name"] == old_scope_extra_name)
    assert old_item["out_of_scope"] is True
    assert fresh.validate().ok


# ---------------------------------------------------------------------------
# #574 round11 R7-H3 — a manifest write failure must fail the step, and
# validate() must never read a backup dir with content but no manifest as
# "nothing to check".
# ---------------------------------------------------------------------------


def test_manifest_write_failure_fails_the_step_not_a_silent_ok(
    tmp_path, journal_backups, monkeypatch
):
    journal, backups = journal_backups
    monkeypatch.setattr(
        "agent_takkub.core.migration.pre_migrate_backup.migration_home", lambda: tmp_path / "mh"
    )
    data_home = _seeded_data_home(tmp_path)
    step = PreMigrateBackupStep(journal=journal, backups=backups, data_home=data_home)

    import agent_takkub.core.migration.pre_migrate_backup as backup_mod

    real_write_json_atomic = backup_mod.write_json_atomic

    def _fail_only_for_manifest(path, payload):
        if Path(path).name == "manifest.json":
            raise OSError("disk full")
        return real_write_json_atomic(path, payload)

    monkeypatch.setattr(backup_mod, "write_json_atomic", _fail_only_for_manifest)

    report = step.apply()
    assert not report.ok
    assert "manifest" in report.summary.lower()

    # Files were already durably copy-verified (never rolled back just for
    # a manifest-write failure) — but with no manifest, validate() must
    # report a real failure, never "nothing to check yet".
    validate_report = step.validate()
    assert not validate_report.ok
