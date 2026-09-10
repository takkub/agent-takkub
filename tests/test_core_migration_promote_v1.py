"""`core.migration.promote_v1` — #504's "finish the move" pair:
`PromoteV2RootStep` (relocate a pre-existing nested v2/ root up to the
top-level layout) and `ArchiveV1LegacyStep` (archive V1 top-level leftovers,
delete #504 item 5's named junk outright, archive the emptied v2/ folder
too). Every test operates strictly under `tmp_path` — never the real
DATA_HOME/SETTINGS_HOME."""

from __future__ import annotations

import json

import pytest

from agent_takkub.core.migration.backup import BackupManager
from agent_takkub.core.migration.journal import MigrationJournal
from agent_takkub.core.migration.promote_v1 import (
    ArchiveV1LegacyStep,
    PromoteV2RootStep,
    _find_latest_manifest,
)
from agent_takkub.core.migration.verify_copy import VerifyMismatchError, copy_verified
from agent_takkub.core.storage.jsonl_store import JsonlStore


@pytest.fixture
def journal_backups(tmp_path):
    journal = MigrationJournal(JsonlStore(tmp_path / "journal.jsonl"))
    backups = BackupManager(tmp_path / "backups")
    return journal, backups


# ---------------------------------------------------------------------------
# verify_copy
# ---------------------------------------------------------------------------


def test_copy_verified_copies_file_and_leaves_source(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("hello", encoding="utf-8")
    dest = tmp_path / "dest.txt"
    result = copy_verified(src, dest)
    assert dest.read_text(encoding="utf-8") == "hello"
    assert src.exists()  # never removes the original
    assert result.file_count == 1


def test_copy_verified_merges_into_existing_directory(tmp_path):
    src = tmp_path / "src"
    (src).mkdir()
    (src / "a.json").write_text("{}", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "existing.json").write_text("{}", encoding="utf-8")
    copy_verified(src, dest)
    assert (dest / "a.json").exists()
    assert (dest / "existing.json").exists()  # untouched, not clobbered


def test_copy_verified_raises_on_missing_target(tmp_path, monkeypatch):
    src = tmp_path / "src.txt"
    src.write_text("hello", encoding="utf-8")
    dest = tmp_path / "dest.txt"

    import shutil

    monkeypatch.setattr(shutil, "copy2", lambda *a, **k: None)  # simulate a silent no-op copy
    with pytest.raises(VerifyMismatchError):
        copy_verified(src, dest)


# ---------------------------------------------------------------------------
# PromoteV2RootStep
# ---------------------------------------------------------------------------


def test_promote_no_op_when_no_legacy_root(tmp_path, journal_backups):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()
    assert report.ok
    assert "nothing to promote" in report.summary
    assert step.validate().ok


def test_promote_moves_legacy_v2_contents_up(tmp_path, journal_backups):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    legacy = data_home / "v2"
    (legacy / "models").mkdir(parents=True)
    (legacy / "models" / "registry.json").write_text('{"a":1}', encoding="utf-8")

    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()
    assert report.ok, report.summary
    assert (data_home / "models" / "registry.json").read_text(encoding="utf-8") == '{"a":1}'
    assert not legacy.is_dir() or not any(legacy.iterdir())
    assert step.validate().ok


def test_promote_merges_into_a_pre_existing_top_level_dir(tmp_path, journal_backups):
    """Regression guard for the kimi-credentials scenario (#504 design
    note): `providers/kimi/default` may already hold live credentials at
    the top level before promote runs — merging v2/providers/* in must
    never clobber it."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "providers" / "kimi" / "default").mkdir(parents=True)
    (data_home / "providers" / "kimi" / "default" / "kimi.json").write_text(
        "live-creds", encoding="utf-8"
    )
    legacy = data_home / "v2"
    (legacy / "providers" / "claude").mkdir(parents=True)
    (legacy / "providers" / "claude" / "provider.json").write_text("{}", encoding="utf-8")

    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()
    assert report.ok, report.summary
    assert (data_home / "providers" / "kimi" / "default" / "kimi.json").read_text(
        encoding="utf-8"
    ) == "live-creds"
    assert (data_home / "providers" / "claude" / "provider.json").exists()


def test_promote_undoes_partial_move_on_failure(tmp_path, journal_backups, monkeypatch):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    legacy = data_home / "v2"
    (legacy / "config").mkdir(parents=True)
    (legacy / "config" / "execution.json").write_text("{}", encoding="utf-8")
    (legacy / "models").mkdir(parents=True)
    (legacy / "models" / "registry.json").write_text("{}", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_copy_verified = promote_mod.copy_verified
    calls = {"n": 0}

    def _flaky_copy(src, dest):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk fell over")
        return real_copy_verified(src, dest)

    monkeypatch.setattr(promote_mod, "copy_verified", _flaky_copy)
    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()

    assert not report.ok
    # Original V1/V2 content is exactly where it started — nothing half-moved.
    assert (legacy / "config" / "execution.json").exists()
    assert (legacy / "models" / "registry.json").exists()
    assert not (data_home / "config").exists()
    assert not (data_home / "models").exists()


# ---------------------------------------------------------------------------
# ArchiveV1LegacyStep
# ---------------------------------------------------------------------------


def test_archive_no_op_on_a_clean_top_level(tmp_path, journal_backups):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()
    assert report.ok
    assert "nothing to archive" in report.summary
    assert not (data_home / "backups").exists()


def test_archive_moves_v1_leftovers_and_never_touches_protected_names(tmp_path, journal_backups):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "projects.json").write_text('{"projects": {}}', encoding="utf-8")
    (data_home / "custom-roles.json").write_text("{}", encoding="utf-8")

    # #504 item 8 "ไม่แตะ" + live infrastructure this must never sweep up.
    (data_home / "runtime" / "core").mkdir(parents=True)
    (data_home / "runtime" / "core" / "version.json").write_text("{}", encoding="utf-8")
    (data_home / "worktrees" / "demo-project" / "wt-1").mkdir(parents=True)
    (data_home / "worktrees" / "demo-project" / "wt-1" / "code.py").write_text(
        "print(1)", encoding="utf-8"
    )
    (data_home / "claude-config" / "auth.json").parent.mkdir(parents=True)
    (data_home / "claude-config" / "auth.json").write_text("secret", encoding="utf-8")
    # A V2 domain a domain step just created THIS SAME run — must never be
    # archived (the "projects" name collision this step must not fall for).
    (data_home / "projects" / "registry.json").parent.mkdir(parents=True)
    (data_home / "projects" / "registry.json").write_text('{"data": {}}', encoding="utf-8")

    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()
    assert report.ok, report.summary

    archive_root = data_home / "backups"
    manifest_path = _find_latest_manifest(data_home)
    assert manifest_path is not None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archived_names = {e["name"] for e in manifest["archived"]}
    assert archived_names == {"projects.json", "custom-roles.json"}

    # V1 leftovers are gone from the top level, safely under the archive.
    assert not (data_home / "projects.json").exists()
    assert not (data_home / "custom-roles.json").exists()
    assert (manifest_path.parent / "projects.json").exists()
    assert (manifest_path.parent / "custom-roles.json").exists()

    # Never-touch infra and the freshly-created V2 "projects/" dir survive untouched.
    assert (data_home / "runtime" / "core" / "version.json").exists()
    assert (data_home / "worktrees" / "demo-project" / "wt-1" / "code.py").exists()
    assert (data_home / "claude-config" / "auth.json").exists()
    assert (data_home / "projects" / "registry.json").exists()
    assert archive_root.is_dir()


def test_archive_deletes_item_5_junk_outright(tmp_path, journal_backups):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "openviking").mkdir(parents=True)
    (data_home / "openviking" / "stale.json").write_text("{}", encoding="utf-8")
    (data_home / "claude-config.partial").mkdir(parents=True)
    (data_home / ".takkub_issues.synced-2026-08-01.bak.json").write_text("[]", encoding="utf-8")
    (data_home / "projects.json").write_text("{}", encoding="utf-8")  # a real archive candidate too

    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()
    assert report.ok, report.summary

    assert not (data_home / "openviking").exists()
    assert not (data_home / "claude-config.partial").exists()
    assert not (data_home / ".takkub_issues.synced-2026-08-01.bak.json").exists()

    manifest_path = _find_latest_manifest(data_home)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["deleted"]) == {
        "openviking",
        "claude-config.partial",
        ".takkub_issues.synced-2026-08-01.bak.json",
    }
    # The deleted junk must never appear in the archive itself.
    assert not (manifest_path.parent / "openviking").exists()


def test_archive_moves_the_emptied_legacy_v2_folder_into_the_archive(tmp_path, journal_backups):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    legacy = data_home / "v2"
    legacy.mkdir(parents=True)  # already emptied by PromoteV2RootStep

    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()
    assert report.ok, report.summary
    assert not legacy.exists()
    manifest_path = _find_latest_manifest(data_home)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["legacy_v2_marker_archived"] is True
    assert (manifest_path.parent / "v2").is_dir()


def test_archive_undoes_partial_move_on_failure(tmp_path, journal_backups, monkeypatch):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "projects.json").write_text("{}", encoding="utf-8")
    (data_home / "custom-roles.json").write_text("{}", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_copy_verified = promote_mod.copy_verified
    calls = {"n": 0}

    def _flaky_copy(src, dest):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk fell over")
        return real_copy_verified(src, dest)

    monkeypatch.setattr(promote_mod, "copy_verified", _flaky_copy)
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()

    assert not report.ok
    assert (data_home / "projects.json").exists()
    assert (data_home / "custom-roles.json").exists()
    assert not (data_home / "backups").exists() or not any((data_home / "backups").iterdir())


# ---------------------------------------------------------------------------
# rollback / restore-v1 round trip
# ---------------------------------------------------------------------------


def test_full_promote_then_restore_round_trip(tmp_path, journal_backups):
    """The full #504 pair, followed by `restore-v1` (both steps'
    `rollback()`), reconstructs the exact pre-2.1.0 shape byte-for-byte —
    and the archive itself survives the restore untouched (#504 item 2:
    "archive ไม่มีวันหมดอายุ")."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    legacy = data_home / "v2"
    (legacy / "models").mkdir(parents=True)
    (legacy / "models" / "registry.json").write_text('{"pins": 1}', encoding="utf-8")
    (data_home / "projects.json").write_text('{"projects": {}}', encoding="utf-8")
    (data_home / "custom-roles.json").write_text('{"backend": {}}', encoding="utf-8")

    promote = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    archive = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)

    assert promote.apply().ok
    assert archive.apply().ok

    assert (data_home / "models" / "registry.json").exists()
    assert not (data_home / "projects.json").exists()
    assert not (data_home / "v2").exists()

    # restore-v1: archive rollback first, then promote rollback — the same
    # order the CLI's `restore-v1` command uses.
    archive_rb = archive.rollback()
    promote_rb = promote.rollback()
    assert archive_rb.ok, archive_rb.summary
    assert promote_rb.ok, promote_rb.summary

    assert (data_home / "projects.json").read_text(encoding="utf-8") == '{"projects": {}}'
    assert (data_home / "custom-roles.json").read_text(encoding="utf-8") == '{"backend": {}}'
    assert (data_home / "v2" / "models" / "registry.json").read_text(encoding="utf-8") == (
        '{"pins": 1}'
    )

    # The archive itself is still fully intact (copy-restore, never a move).
    manifest_path = _find_latest_manifest(data_home)
    assert manifest_path is not None
    assert (manifest_path.parent / "projects.json").exists()


def test_rollback_is_a_no_op_when_nothing_was_ever_archived(tmp_path, journal_backups):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    promote = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    archive = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert archive.rollback().ok
    assert promote.rollback().ok


def test_restore_does_not_recover_item_5_deleted_junk(tmp_path, journal_backups):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "openviking").mkdir(parents=True)
    (data_home / "projects.json").write_text("{}", encoding="utf-8")

    archive = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert archive.apply().ok
    report = archive.rollback()
    assert report.ok
    assert "not recoverable" in report.summary
    assert (data_home / "projects.json").exists()
    assert not (data_home / "openviking").exists()
