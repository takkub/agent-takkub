"""`core.migration.promote_v1` — #504's "finish the move" pair:
`PromoteV2RootStep` (relocate a pre-existing nested v2/ root up to the
top-level layout) and `ArchiveV1LegacyStep` (archive V1 top-level leftovers,
delete #504 item 5's named junk outright, archive the emptied v2/ folder
too). Every test operates strictly under `tmp_path` — never the real
DATA_HOME/SETTINGS_HOME."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub.core.migration.backup import BackupManager
from agent_takkub.core.migration.journal import MigrationJournal
from agent_takkub.core.migration.promote_v1 import (
    ArchiveV1LegacyStep,
    PromoteV2RootStep,
    _find_latest_manifest,
    list_v1_archives,
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


# ---------------------------------------------------------------------------
# 2026-09-10 acceptance review (docs/audit/2026-09-10-504-acceptance-review.md)
# B2/B3/B4, H1/H2/H3/H7/H9 — regressions reproduced from the reviewer's own
# fixtures (nested v2/ + a live providers/kimi/default home + V1 leftovers),
# adapted to this file's tmp_path/journal_backups style.
# ---------------------------------------------------------------------------


def test_archive_refuses_to_delete_a_nonempty_legacy_v2_root(tmp_path, journal_backups):
    """#504 B2: a `promote-v2-root` that never finished (or was interrupted)
    can leave real un-promoted data under `v2/` — `ArchiveV1LegacyStep` must
    never `shutil.rmtree` it just because it exists."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "state").mkdir(parents=True)
    (data_home / "v2" / "state" / "only-copy.json").write_text("unique", encoding="utf-8")

    archive = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    report = archive.apply()

    assert not report.ok
    assert "refusing to delete" in report.summary
    assert (data_home / "v2" / "state" / "only-copy.json").read_text(encoding="utf-8") == "unique"
    assert not (data_home / "backups").exists()


def test_promote_merge_undo_preserves_a_live_sibling_never_touched_by_the_copy(
    tmp_path, journal_backups, monkeypatch
):
    """#504 B3 `merge_undo`: a live `providers/kimi/default/auth.json` sits
    beside a legacy `v2/providers/claude/...` waiting to be promoted. When a
    LATER candidate's copy fails, undoing the `providers/` merge must
    restore it to exactly its pre-merge state (the newly-merged `claude/`
    entry gone) without ever losing the kimi home that was never part of
    this transaction at all."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "providers" / "kimi" / "default").mkdir(parents=True)
    (data_home / "providers" / "kimi" / "default" / "auth.json").write_text(
        "live-secret", encoding="utf-8"
    )
    (data_home / "v2" / "providers" / "claude").mkdir(parents=True)
    (data_home / "v2" / "providers" / "claude" / "provider.json").write_text(
        "reference", encoding="utf-8"
    )
    (data_home / "v2" / "state").mkdir(parents=True)
    (data_home / "v2" / "state" / "test.json").write_text("state", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_copy_verified = promote_mod.copy_verified

    def _fail_on_state(src, dest):
        if src.name == "state":
            raise OSError("injected middle copy failure")
        return real_copy_verified(src, dest)

    monkeypatch.setattr(promote_mod, "copy_verified", _fail_on_state)
    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()

    assert not report.ok
    assert (data_home / "providers" / "kimi" / "default" / "auth.json").read_text(
        encoding="utf-8"
    ) == "live-secret"
    assert not (data_home / "providers" / "claude").exists()


def test_promote_partial_copy_junk_is_cleaned_up_on_failure(tmp_path, journal_backups, monkeypatch):
    """#504 B3 `partial_copy`: a copy that writes a partial destination and
    THEN raises must still have that partial junk cleaned up — it used to
    never make it into the undo list at all."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "models").mkdir(parents=True)
    (data_home / "v2" / "models" / "original.json").write_text("{}", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    def _partial(src, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "partial.json").write_text("half", encoding="utf-8")
        raise OSError("injected partial copy")

    monkeypatch.setattr(promote_mod, "copy_verified", _partial)
    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()

    assert not report.ok
    assert not (data_home / "models" / "partial.json").exists()
    assert not (data_home / "models").exists()


def test_restore_v1_does_not_relocate_a_live_provider_home_never_promoted(
    tmp_path, journal_backups
):
    """#504 B3 `restore_provider`: after a clean promote+archive, running
    `restore-v1` (archive rollback then promote rollback, the CLI's order)
    must never move the live Kimi home into the reconstructed `v2/` — it was
    never part of what got promoted."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "providers" / "kimi" / "default").mkdir(parents=True)
    (data_home / "providers" / "kimi" / "default" / "auth.json").write_text(
        "live-secret", encoding="utf-8"
    )
    (data_home / "v2" / "providers" / "claude").mkdir(parents=True)
    (data_home / "v2" / "providers" / "claude" / "provider.json").write_text(
        "reference", encoding="utf-8"
    )

    promote = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    archive = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert promote.apply().ok
    assert archive.apply().ok

    archive_rb = archive.rollback()
    promote_rb = promote.rollback()
    assert archive_rb.ok and promote_rb.ok

    assert (data_home / "providers" / "kimi" / "default" / "auth.json").read_text(
        encoding="utf-8"
    ) == "live-secret"
    assert not (data_home / "v2" / "providers" / "kimi").exists()


def test_archive_protects_named_provider_home_and_the_live_registry(tmp_path, journal_backups):
    """#504 B4 `named_profiles`: a named claude account
    (`claude-config-team`, #505 stage 2) shares no fixed basename the static
    skip-list enumerates, and `user-profiles.json` is still a live
    read/write target, not a retired V1 source — both must survive archive."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "claude-config-team").mkdir(parents=True)
    (data_home / "claude-config-team" / "auth.json").write_text(
        "named-account-secret", encoding="utf-8"
    )
    (data_home / "user-profiles.json").write_text(
        json.dumps([{"name": "team", "config_dir": str(data_home / "claude-config-team")}]),
        encoding="utf-8",
    )
    (data_home / "projects.json").write_text("{}", encoding="utf-8")  # a real V1 leftover too

    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()

    assert report.ok, report.summary
    assert (data_home / "claude-config-team" / "auth.json").exists()
    assert (data_home / "user-profiles.json").exists()
    assert not (data_home / "projects.json").exists()  # the real leftover still gets archived


def test_restore_v1_recovers_an_older_archive_generation_by_ts(tmp_path, journal_backups):
    """#504 H1 `latest_archive`: two archive generations exist (an older one
    holding `projects.json`/`custom-roles.json`, a newer one holding
    something else) — `rollback(archive_ts=...)` must be able to reach the
    OLDER one explicitly, not just whatever is latest."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "projects.json").write_text("projects-before", encoding="utf-8")
    (data_home / "custom-roles.json").write_text("roles-before", encoding="utf-8")
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok
    from agent_takkub.core.migration.promote_v1 import list_v1_archives

    first_ts = list_v1_archives(data_home)[0]["ts"]

    (data_home / "auto-migrate-state.json").write_text("new-state", encoding="utf-8")
    assert step.apply().ok  # a second, newer generation

    report = step.rollback(archive_ts=first_ts)
    assert report.ok, report.summary
    assert (data_home / "projects.json").read_text(encoding="utf-8") == "projects-before"
    assert (data_home / "custom-roles.json").read_text(encoding="utf-8") == "roles-before"


def test_restore_v1_list_shows_every_generation_newest_first(tmp_path, journal_backups):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "projects.json").write_text("a", encoding="utf-8")
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok
    (data_home / "custom-roles.json").write_text("b", encoding="utf-8")
    assert step.apply().ok

    from agent_takkub.core.migration.promote_v1 import list_v1_archives

    archives = list_v1_archives(data_home)
    assert len(archives) == 2
    assert archives[0]["ts"] > archives[1]["ts"]  # newest first


def test_restore_v1_survives_a_relocated_archive_tree(tmp_path, journal_backups):
    """#504 H2 `relocated_archive`: the archive manifest must record paths
    RELATIVE to the archive itself — copying `backups/` to a new DATA_HOME
    (simulating the original location becoming unavailable, e.g. a drive
    swap) must still let restore find its files."""
    import shutil as _shutil

    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "projects.json").write_text("projects-before", encoding="utf-8")
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok

    relocated = tmp_path / "relocated-data"
    _shutil.copytree(data_home / "backups", relocated / "backups")

    relocated_step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=relocated)
    report = relocated_step.rollback()

    assert report.ok, report.summary
    assert report.detail["restored"]
    assert (relocated / "projects.json").read_text(encoding="utf-8") == "projects-before"


def test_restore_v1_preserves_current_file_instead_of_silently_discarding_it(
    tmp_path, journal_backups
):
    """#504 H3 `restore_collision`: the destination was recreated with
    different content since the archive ran — restoring must not just
    silently clobber it with no way to get "new-current" back."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "projects.json").write_text("old", encoding="utf-8")
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok
    (data_home / "projects.json").write_text("new-current", encoding="utf-8")

    report = step.rollback()

    assert report.ok, report.summary
    assert (data_home / "projects.json").read_text(encoding="utf-8") == "old"
    # "new-current" survives somewhere recoverable (this step's own
    # BackupManager preimage slot), not simply gone.
    preserved = any(
        p.is_file() and p.read_text(encoding="utf-8") == "new-current"
        for p in backups.root.rglob("*")
    )
    assert preserved


def test_archive_picks_up_v1_files_shared_with_a_v2_top_level_directory(tmp_path, journal_backups):
    """#504 H7 `shared_legacy`: `agents/<role>.md` and
    `projects/<slug>/role-providers.json` are real V1 leftovers living one
    level inside a directory name V2 also owns — the whole-directory skip
    must not make them invisible to archive/`_pending()`/`validate()`
    forever."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "agents").mkdir(parents=True)
    (data_home / "agents" / "custom-role.md").write_text("role md", encoding="utf-8")
    (data_home / "projects" / "demo").mkdir(parents=True)
    (data_home / "projects" / "demo" / "role-providers.json").write_text("{}", encoding="utf-8")
    # V2's own sibling content in the same directories must survive.
    (data_home / "agents" / "custom").mkdir(parents=True)
    (data_home / "agents" / "custom" / "registry.json").write_text("{}", encoding="utf-8")
    (data_home / "projects" / "demo" / "project.json").write_text("{}", encoding="utf-8")

    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert step._pending() is True

    report = step.apply()
    assert report.ok, report.summary
    assert not (data_home / "agents" / "custom-role.md").exists()
    assert not (data_home / "projects" / "demo" / "role-providers.json").exists()
    assert (data_home / "agents" / "custom" / "registry.json").exists()
    assert (data_home / "projects" / "demo" / "project.json").exists()
    assert step.validate().ok
    assert (data_home / "backups").exists()


def test_archive_validate_detects_a_corrupted_archived_file(tmp_path, journal_backups):
    """#504 H9: a green `validate()` used to mean only "nothing left to
    archive" — it must also catch the archive itself having been corrupted
    or partially deleted after the fact."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "projects.json").write_text("{}", encoding="utf-8")
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok
    assert step.validate().ok

    archived_copy = next((data_home / "backups").rglob("projects.json"))
    archived_copy.write_text("corrupted", encoding="utf-8")

    report = step.validate()
    assert not report.ok
    assert "checksum mismatch" in report.summary


# ---------------------------------------------------------------------------
# 2026-09-10 acceptance review, ROUND 2 (docs/audit/2026-09-10-504-acceptance-
# review.md "## Round 2 — 60abb771") — R2-B1 (source-removal undo could drop
# a file to zero copies), R2-H1 (restore-v1 generation handling), R2-H2
# (nested registered provider homes), R2-H3 covered separately in
# test_auto_migrate_boot.py, and the crash/restart + older-generation H9
# gaps #568 left open.
# ---------------------------------------------------------------------------


def _every_copy(data_home: Path, name: str) -> list[Path]:
    """Every file anywhere under *data_home*'s parent (source, target,
    archive, backups — everywhere this suite's fixtures could put a copy)
    whose basename is *name* — the concrete form of the "copies >= 1"
    invariant every #504 promote/archive/rollback/restore path must hold."""
    return [p for p in data_home.parent.rglob(name) if p.is_file()]


def test_promote_delete_phase_failure_restores_every_source_no_data_lost(
    tmp_path, journal_backups, monkeypatch
):
    """#504 R2-B1: a source-removal failure on the SECOND candidate used to
    trigger `_undo_moved_entries`, which deletes the (only remaining) target
    for the FIRST candidate whose source had already been removed —
    reproducibly dropping it to zero copies. The fix restores every source
    Phase B already removed instead of ever touching a target."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "models" / "only-a.json").parent.mkdir(parents=True)
    (data_home / "v2" / "models" / "only-a.json").write_text("unique-a", encoding="utf-8")
    (data_home / "v2" / "state" / "only-b.json").parent.mkdir(parents=True)
    (data_home / "v2" / "state" / "only-b.json").write_text("unique-b", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_rmtree = promote_mod.shutil.rmtree
    target = data_home / "v2" / "state"
    count = {"n": 0}

    def _fail_second_remove(path, *a, **k):
        if Path(path) == target:
            count["n"] += 1
            if count["n"] == 1:
                raise OSError("injected failure removing second source")
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(promote_mod.shutil, "rmtree", _fail_second_remove)
    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()

    assert not report.ok
    # Both original nested sources are back (the one whose removal
    # succeeded was re-materialized from its already-verified target).
    assert (data_home / "v2" / "models" / "only-a.json").read_text(encoding="utf-8") == "unique-a"
    assert (data_home / "v2" / "state" / "only-b.json").read_text(encoding="utf-8") == "unique-b"
    assert len(_every_copy(data_home, "only-a.json")) >= 1
    assert len(_every_copy(data_home, "only-b.json")) >= 1
    # Retry succeeds cleanly once the injected failure is gone.
    assert step.apply().ok
    assert (data_home / "models" / "only-a.json").read_text(encoding="utf-8") == "unique-a"
    assert (data_home / "state" / "only-b.json").read_text(encoding="utf-8") == "unique-b"


def test_archive_delete_phase_failure_restores_every_source_no_data_lost(
    tmp_path, journal_backups, monkeypatch
):
    """#504 R2-B1, archive side: same failure class, reproduced through
    `ArchiveV1LegacyStep.apply()` instead of promote."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "a.json").write_text("unique-a", encoding="utf-8")
    (data_home / "b.json").write_text("unique-b", encoding="utf-8")

    real_unlink = Path.unlink
    target = data_home / "b.json"

    def _fail_second_remove(self, *a, **k):
        if self == target:
            raise OSError("injected failure removing second source")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", _fail_second_remove)
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()

    assert not report.ok
    assert (data_home / "a.json").read_text(encoding="utf-8") == "unique-a"
    assert (data_home / "b.json").read_text(encoding="utf-8") == "unique-b"
    assert len(_every_copy(data_home, "a.json")) >= 1
    assert len(_every_copy(data_home, "b.json")) >= 1


def test_promote_rollback_delete_phase_failure_restores_every_source(
    tmp_path, journal_backups, monkeypatch
):
    """#504 R2-B1, promote-rollback side: the third reproduction the
    reviewer named — `PromoteV2RootStep.rollback()` shares the same
    `_two_phase_move` helper as `apply()`."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "models" / "only-a.json").parent.mkdir(parents=True)
    (data_home / "v2" / "models" / "only-a.json").write_text("unique-a", encoding="utf-8")
    (data_home / "v2" / "state" / "only-b.json").parent.mkdir(parents=True)
    (data_home / "v2" / "state" / "only-b.json").write_text("unique-b", encoding="utf-8")
    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok
    assert (data_home / "models" / "only-a.json").exists()
    assert (data_home / "state" / "only-b.json").exists()

    real_unlink = Path.unlink
    target = data_home / "state" / "only-b.json"

    def _fail_second_remove(self, *a, **k):
        if self == target:
            raise OSError("injected failure removing second source")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", _fail_second_remove)
    report = step.rollback()

    assert not report.ok
    assert len(_every_copy(data_home, "only-a.json")) >= 1
    assert len(_every_copy(data_home, "only-b.json")) >= 1
    assert (data_home / "models" / "only-a.json").read_text(encoding="utf-8") == "unique-a"
    assert (data_home / "state" / "only-b.json").read_text(encoding="utf-8") == "unique-b"


def test_promote_survives_a_crash_right_after_a_real_source_removal(
    tmp_path, journal_backups, monkeypatch
):
    """#504 R2 `crash_restart_restore`: a process death (not a caught
    OSError) between a REAL source removal and this step's own bookkeeping
    used to leave that candidate permanently missing from the promote
    manifest — a later retry's batch manifest write only ever described
    ITS OWN candidates, silently dropping whatever an earlier interrupted
    run had already promoted. `on_before_remove` now merges each candidate
    into the manifest BEFORE its source is removed, so the record survives
    even when the removal itself is the last thing that ever completes."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "models" / "only-a.json").parent.mkdir(parents=True)
    (data_home / "v2" / "models" / "only-a.json").write_text("unique-a", encoding="utf-8")
    (data_home / "v2" / "state" / "only-b.json").parent.mkdir(parents=True)
    (data_home / "v2" / "state" / "only-b.json").write_text("unique-b", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_remove = promote_mod._remove

    def _crash_after_first_remove(path):
        real_remove(path)  # the real deletion DID happen
        raise RuntimeError("simulated process loss right after source removal")

    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    monkeypatch.setattr(promote_mod, "_remove", _crash_after_first_remove)
    with pytest.raises(RuntimeError):
        step.apply()
    monkeypatch.undo()  # restore the real `_remove` for the retry below

    # "models" (processed first, alphabetically) is really gone from v2/ —
    # the crash happened right after its real removal — but "state" never
    # got touched at all.
    assert not (data_home / "v2" / "models").exists()
    assert (data_home / "v2" / "state" / "only-b.json").is_file()

    # A resumed retry (a new boot) reads this step's own WAL — which still
    # names "models" as already `SOURCE_PRUNED` — rather than rescanning
    # `v2/` fresh, so the manifest still remembers it from the interrupted
    # run instead of silently dropping it.
    retry = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    assert retry.apply().ok
    assert not (data_home / "v2" / "models").exists()

    rollback_report = retry.rollback()
    assert rollback_report.ok, rollback_report.summary
    assert (data_home / "v2" / "models" / "only-a.json").read_text(encoding="utf-8") == "unique-a"
    assert (data_home / "v2" / "state" / "only-b.json").read_text(encoding="utf-8") == "unique-b"


def test_restore_v1_refuses_when_an_archived_member_is_missing(tmp_path, journal_backups):
    """#504 R2-H1/H2 residual: `ArchiveV1LegacyStep.rollback()` used to skip
    a missing archived member per-entry (`if not src.exists(): continue`)
    and still report `ok=True`, silently dropping that file from the
    restore. It must now refuse the WHOLE restore, touching nothing."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "projects.json").write_text("original", encoding="utf-8")
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok

    archived_copy = next((data_home / "backups").rglob("projects.json"))
    archived_copy.unlink()

    report = step.rollback()
    assert not report.ok
    assert not (data_home / "projects.json").exists()


def test_restore_v1_refuses_when_an_archived_member_is_corrupt(tmp_path, journal_backups):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "projects.json").write_text("original", encoding="utf-8")
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok

    archived_copy = next((data_home / "backups").rglob("projects.json"))
    archived_copy.write_text("corrupted", encoding="utf-8")

    report = step.rollback()
    assert not report.ok
    assert not (data_home / "projects.json").exists()


def test_archive_protects_a_nested_registered_provider_home(tmp_path, journal_backups):
    """#504 R2-H2: `_named_account_home_names()` used to protect only an
    EXACT top-level `config_dir` (`Path(config_dir).parent == data_home`).
    A registered home nested two levels deep must still protect its whole
    top-level ancestor directory, not just its own leaf name."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    home = data_home / "team-homes" / "nested" / "claude-config-team"
    (home / "auth.json").parent.mkdir(parents=True)
    (home / "auth.json").write_text("named-account-secret", encoding="utf-8")
    (data_home / "user-profiles.json").write_text(
        json.dumps([{"name": "team", "config_dir": str(home)}]), encoding="utf-8"
    )
    (data_home / "projects.json").write_text("{}", encoding="utf-8")  # a real leftover too

    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()

    assert report.ok, report.summary
    assert (home / "auth.json").read_text(encoding="utf-8") == "named-account-secret"
    assert not (data_home / "projects.json").exists()


def test_archive_validate_detects_corruption_in_an_older_non_latest_generation(
    tmp_path, journal_backups
):
    """#504 R2-H9 `older_archive_integrity`: `validate()` used to check
    only `_find_latest_manifest()` — corrupting an OLDER generation stayed
    invisible even though `restore-v1`'s default walks every generation,
    not just the latest."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "projects.json").write_text("old", encoding="utf-8")
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok
    older_archive = Path(list_v1_archives(data_home)[0]["path"])

    (data_home / "custom-roles.json").write_text("new-generation", encoding="utf-8")
    assert step.apply().ok  # a second, newer generation

    (older_archive / "projects.json").write_text("corrupted", encoding="utf-8")

    report = step.validate()
    assert not report.ok
    assert older_archive.name in report.summary


def test_restore_v1_rejects_an_explicit_unknown_archive_generation(tmp_path, journal_backups):
    """#504 R2-H1: `rollback(archive_ts=<unknown>)` used to return the SAME
    ok=True "no archive to restore from" report as the generic no-archives-
    at-all case — a typo'd/stale `--archive <ts>` silently did nothing
    while reporting success."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)

    report = step.rollback(archive_ts="not-a-real-generation")
    assert not report.ok


def test_list_v1_archives_surfaces_an_unreadable_generation_instead_of_hiding_it(
    tmp_path, journal_backups
):
    """#504 R2-H1: a generation whose manifest.json is missing/corrupt used
    to be silently dropped from `--list` entirely."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "projects.json").write_text("{}", encoding="utf-8")
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok

    archive_dir = Path(list_v1_archives(data_home)[0]["path"])
    (archive_dir / "manifest.json").write_text("not-json", encoding="utf-8")

    archives = list_v1_archives(data_home)
    assert len(archives) == 1
    assert archives[0]["unreadable"] is True


def test_restore_v1_cli_undoes_an_earlier_generation_when_a_later_one_fails(
    tmp_path, journal_backups
):
    """#504 R2-H1: `cli._cmd_migrate_restore_v1` walks every generation
    oldest-first when no `--archive` is given — if a LATER generation's
    restore fails, the EARLIER one this same call already restored must be
    undone too (all-or-nothing), not left half-applied."""
    from argparse import Namespace

    from agent_takkub.cli import _cmd_migrate_restore_v1
    from agent_takkub.core.migration.engine import MigrationEngine

    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    promote = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    archive = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)

    (data_home / "projects.json").write_text("gen1", encoding="utf-8")
    assert archive.apply().ok  # oldest generation

    (data_home / "custom-roles.json").write_text("gen2", encoding="utf-8")
    assert archive.apply().ok  # newest generation

    # Corrupt the NEWER generation's archived member so it fails AFTER the
    # older one has already been restored (oldest-first walk order).
    # `list_v1_archives` is newest-first, so index 0 is the newer one.
    newest_archive = Path(list_v1_archives(data_home)[0]["path"])
    (newest_archive / "custom-roles.json").write_text("corrupted", encoding="utf-8")

    engine = MigrationEngine([promote, archive], data_home=data_home, journal=journal)
    reports = _cmd_migrate_restore_v1(engine, Namespace(archive_ts=None))

    assert not all(r.ok for r in reports)
    # The older generation's restore ("projects.json") must have been
    # undone again — it never existed before this restore-v1 call started.
    assert not (data_home / "projects.json").exists()


# ---------------------------------------------------------------------------
# 2026-09-10 acceptance review, ROUND 3 (docs/audit/2026-09-10-504-acceptance-
# review.md "## Round 3 — 28a4e527") — the transaction-core rewrite that
# replaced the shared `_two_phase_move` helper with per-file `TransferEntry`
# + `_copy_phase`/`_prune_phase`, and `cli._cmd_migrate_restore_v1`'s
# command-level snapshot for multi-generation restore.
# ---------------------------------------------------------------------------


def test_promote_recovery_never_contaminates_ownership_with_a_live_sibling(
    tmp_path, journal_backups, monkeypatch
):
    """#504 R3-B1 `merged_live_home_after_retry`: the old recovery path
    reconstructed a removed source by `shutil.copytree`ing the WHOLE merged
    destination back — which pulled in a live sibling (Kimi's real
    credential home) that was never part of this transaction at all. A
    retry then recorded that sibling as promoted ownership, and a
    subsequent `rollback()` removed it from its ACTUAL home. Recovery must
    only ever reconstruct the exact files THIS entry's own copy verified."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    auth = data_home / "providers" / "kimi" / "default" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("live-secret", encoding="utf-8")
    (data_home / "v2" / "providers" / "claude").mkdir(parents=True)
    (data_home / "v2" / "providers" / "claude" / "provider.json").write_text(
        "ref", encoding="utf-8"
    )
    (data_home / "v2" / "state").mkdir(parents=True)
    (data_home / "v2" / "state" / "value.json").write_text("state", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_rmtree = promote_mod.shutil.rmtree
    target = data_home / "v2" / "state"

    def _fail_removing_state(path, *a, **k):
        if Path(path) == target:
            raise OSError("last remove blocked")
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(promote_mod.shutil, "rmtree", _fail_removing_state)
    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    initial = step.apply()
    assert not initial.ok
    # The "providers" candidate's removal succeeded before "state" failed —
    # recovery must have reconstructed it WITHOUT sweeping the live kimi
    # home in alongside it.
    assert not (data_home / "v2" / "providers" / "kimi").exists()
    assert auth.read_text(encoding="utf-8") == "live-secret"

    monkeypatch.setattr(promote_mod.shutil, "rmtree", real_rmtree)
    assert step.apply().ok  # retry, now unblocked
    rollback_report = step.rollback()
    assert rollback_report.ok, rollback_report.summary
    # The live kimi home must never appear anywhere under the reconstructed
    # v2/ tree, and must still exist at its real, untouched home.
    assert not (data_home / "v2" / "providers" / "kimi").exists()
    assert auth.read_text(encoding="utf-8") == "live-secret"


def test_promote_recovery_restores_every_file_after_a_partial_directory_removal(
    tmp_path, journal_backups, monkeypatch
):
    """#504 R3 `partial_final_directory_remove`: a directory removal that
    deletes SOME of its files before raising (a real `shutil.rmtree`
    failure mode) must not leave the un-restored ones missing — recovery
    unconditionally re-copies every file THIS entry's own manifest recorded,
    regardless of how much of a failed removal actually completed."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "models" / "a.json").parent.mkdir(parents=True)
    (data_home / "v2" / "models" / "a.json").write_text("a", encoding="utf-8")
    (data_home / "v2" / "state" / "b.json").parent.mkdir(parents=True)
    (data_home / "v2" / "state" / "b.json").write_text("b", encoding="utf-8")
    (data_home / "v2" / "state" / "c.json").write_text("c", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_rmtree = promote_mod.shutil.rmtree
    target = data_home / "v2" / "state"

    def _partially_fail_state(path, *a, **k):
        if Path(path) == target:
            # Simulate a directory removal that got partway through
            # (b.json really deleted) before failing.
            (path / "b.json").unlink()
            raise OSError("directory deletion partially completed")
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(promote_mod.shutil, "rmtree", _partially_fail_state)
    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()

    assert not report.ok
    assert (data_home / "v2" / "state" / "b.json").read_text(encoding="utf-8") == "b"
    assert (data_home / "v2" / "state" / "c.json").read_text(encoding="utf-8") == "c"


def test_promote_apply_refuses_when_existing_manifest_is_corrupt(tmp_path, journal_backups):
    """#504 R3-H1 `merge_unreadable_prior_manifest`: a corrupt existing
    promote manifest must never be silently replaced with a fresh, empty
    one — that permanently drops a previously-recorded promotion's own
    ownership record even though its source is already gone."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "models" / "first.json").parent.mkdir(parents=True)
    (data_home / "v2" / "models" / "first.json").write_text("FIRST", encoding="utf-8")
    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok

    step._manifest_path().write_text("{broken", encoding="utf-8")
    (data_home / "v2" / "state" / "second.json").parent.mkdir(parents=True)
    (data_home / "v2" / "state" / "second.json").write_text("SECOND", encoding="utf-8")

    report = step.apply()
    assert not report.ok
    assert (data_home / "models" / "first.json").read_text(encoding="utf-8") == "FIRST"


def test_promote_validate_detects_a_missing_promoted_file(tmp_path, journal_backups):
    """#504 R3 `promoted_member_inventory`: every file EVER recorded as
    promoted must still be present at its target — `_pending()` alone only
    proves the (by-then-empty) `v2/` root is gone, never that nothing
    promoted from it has since disappeared."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "models" / "unique-extra.json").parent.mkdir(parents=True)
    (data_home / "v2" / "models" / "unique-extra.json").write_text("UNIQUE", encoding="utf-8")
    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok
    assert step.validate().ok

    (data_home / "models" / "unique-extra.json").unlink()

    report = step.validate()
    assert not report.ok
    assert "unique-extra.json" in report.summary


def test_archive_validate_detects_a_generation_with_a_missing_manifest(tmp_path, journal_backups):
    """#504 R3 `archive_missing_manifest_validation`: the old
    `_find_all_manifests`-based validate loop silently FILTERED OUT any
    generation directory whose `manifest.json` was missing — a generation
    that exists but can't be read must fail validate(), not vanish from
    consideration."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "projects.json").write_text("{}", encoding="utf-8")
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok

    from agent_takkub.core.migration.promote_v1 import list_v1_archives

    archive_dir = Path(list_v1_archives(data_home)[0]["path"])
    (archive_dir / "manifest.json").unlink()

    report = step.validate()
    assert not report.ok
    assert archive_dir.name in report.summary


def test_restore_v1_cli_multi_generation_undo_reverts_to_command_entry_state(
    tmp_path, journal_backups, monkeypatch
):
    """#504 R3-B3: the old per-name "latest backup wins" undo model let a
    LATER generation's own restore shadow an EARLIER generation's backup for
    the same top-level name — so undoing after a later generation's failure
    put back that later generation's own overwrite, not the state the whole
    `restore-v1` command actually started from. A command-level snapshot,
    taken once before any generation is touched, must revert to the exact
    pre-command state regardless of how many generations already ran."""
    from argparse import Namespace

    from agent_takkub import config
    from agent_takkub.cli import _cmd_migrate_restore_v1
    from agent_takkub.core.migration.engine import MigrationEngine
    from agent_takkub.core.migration.promote_v1 import list_v1_archives

    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    monkeypatch.setattr(config, "DATA_HOME", data_home)
    promote = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    archive = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)

    # Three separate generations that all happen to archive a file at the
    # SAME top-level name ("same.json") at different points in time.
    for value in ("generation-0", "generation-1", "generation-2"):
        (data_home / "same.json").write_text(value, encoding="utf-8")
        assert archive.apply().ok

    (data_home / "same.json").write_text("CURRENT", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_copy_verified = promote_mod.copy_verified
    # Fail the SECOND generation's restore (oldest-first walk order).
    fail_root = Path(list_v1_archives(data_home)[-2]["path"])

    def _fail_second_generation(src, dest):
        if src.is_relative_to(fail_root):
            raise OSError("injected generation copy failure")
        return real_copy_verified(src, dest)

    monkeypatch.setattr(promote_mod, "copy_verified", _fail_second_generation)
    engine = MigrationEngine([promote, archive], data_home=data_home, journal=journal)
    reports = _cmd_migrate_restore_v1(engine, Namespace(archive_ts=None))

    assert not all(r.ok for r in reports)
    assert (data_home / "same.json").read_text(encoding="utf-8") == "CURRENT"


# ---------------------------------------------------------------------------
# Follow-up to 26e0820a: spec item 5 ("ห้ามกลืน error ใน migration/restore/
# ledger path") still had 4 bare `except OSError: pass` spots whose failure
# was invisible to every caller. Each must now surface into the outcome the
# caller sees (never a silent no-op) without changing what gets attempted.
# ---------------------------------------------------------------------------


def test_undo_copied_dest_reports_a_preimage_restore_failure(tmp_path, monkeypatch):
    from agent_takkub.core.migration.promote_v1 import _undo_copied_dest

    dest = tmp_path / "dest.json"
    dest.write_text("just-copied", encoding="utf-8")
    backup = tmp_path / "backup.json"
    backup.write_text("pre-existing", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    def fail_copy2(*a, **k):
        raise OSError("injected backup restore failure")

    monkeypatch.setattr(promote_mod.shutil, "copy2", fail_copy2)

    error = _undo_copied_dest(dest, backup)
    assert error is not None
    assert "could not restore preimage" in error
    assert "injected backup restore failure" in error


def test_copy_phase_reports_an_undo_failure_instead_of_swallowing_it(
    tmp_path, journal_backups, monkeypatch
):
    """The `_undo_copied_dest` failure above must actually reach the
    `StepReport` a real caller sees, not just the direct unit — a caller
    that can't put a pre-existing destination's own prior content back
    needs to know its retry starts from a destination that may still be
    wrong."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "models").mkdir(parents=True)
    (data_home / "models" / "existing.json").write_text("PRE-EXISTING", encoding="utf-8")
    (data_home / "v2" / "models" / "new.json").parent.mkdir(parents=True)
    (data_home / "v2" / "models" / "new.json").write_text("new", encoding="utf-8")
    (data_home / "v2" / "state" / "x.json").parent.mkdir(parents=True)
    (data_home / "v2" / "state" / "x.json").write_text("x", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_copy_verified = promote_mod.copy_verified

    def fail_state(src, dest):
        if src.name == "state":
            raise OSError("injected state copy failure")
        return real_copy_verified(src, dest)

    monkeypatch.setattr(promote_mod, "copy_verified", fail_state)

    # `shutil` is a single process-wide module object — patching it here
    # would ALSO hit `BackupManager.backup()`'s own `copytree` call (taking
    # the initial preimage backup, a different callsite). Only fail the
    # RESTORE-back-onto-data_home call `_undo_copied_dest` makes.
    real_copytree = promote_mod.shutil.copytree

    def fail_restoring_models(src, dst, *a, **k):
        if Path(dst) == data_home / "models":
            raise OSError("injected preimage restore failure")
        return real_copytree(src, dst, *a, **k)

    monkeypatch.setattr(promote_mod.shutil, "copytree", fail_restoring_models)

    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()

    assert not report.ok
    assert "could not restore preimage" in report.summary


def test_restore_source_from_dest_reports_a_file_kind_failure(tmp_path, monkeypatch):
    from agent_takkub.core.migration.promote_v1 import TransferEntry

    src = tmp_path / "src.json"
    dest = tmp_path / "dest.json"
    dest.write_text("verified", encoding="utf-8")
    entry = TransferEntry(name="x", kind="file", src=src, dest=dest)

    import agent_takkub.core.migration.promote_v1 as promote_mod

    def fail_copy2(*a, **k):
        raise OSError("injected copy2 failure")

    monkeypatch.setattr(promote_mod.shutil, "copy2", fail_copy2)

    errors = entry.restore_source_from_dest()
    assert errors
    assert str(src) in errors[0]
    assert not src.exists()


def test_restore_source_from_dest_reports_a_dir_kind_failure_per_file(tmp_path, monkeypatch):
    from agent_takkub.core.migration.promote_v1 import TransferEntry

    src = tmp_path / "src_dir"
    dest = tmp_path / "dest_dir"
    dest.mkdir()
    (dest / "a.json").write_text("a", encoding="utf-8")
    (dest / "b.json").write_text("b", encoding="utf-8")
    entry = TransferEntry(name="x", kind="dir", src=src, dest=dest, paths=("a.json", "b.json"))

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_copy2 = promote_mod.shutil.copy2

    def fail_b(s, d, *a, **k):
        if Path(d).name == "b.json":
            raise OSError("injected copy2 failure for b.json")
        return real_copy2(s, d, *a, **k)

    monkeypatch.setattr(promote_mod.shutil, "copy2", fail_b)

    errors = entry.restore_source_from_dest()
    assert len(errors) == 1
    assert "b.json" in errors[0]
    assert (src / "a.json").read_text(encoding="utf-8") == "a"
    assert not (src / "b.json").exists()


def test_prune_phase_reports_a_restore_failure_via_apply(tmp_path, journal_backups, monkeypatch):
    """The `restore_source_from_dest` failure above must also reach the
    `StepReport` from a real `apply()`/`_prune_phase` call, not just the
    direct `TransferEntry` unit."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "models" / "a.json").parent.mkdir(parents=True)
    (data_home / "v2" / "models" / "a.json").write_text("a", encoding="utf-8")
    (data_home / "v2" / "state" / "b.json").parent.mkdir(parents=True)
    (data_home / "v2" / "state" / "b.json").write_text("b", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_rmtree = promote_mod.shutil.rmtree
    target = data_home / "v2" / "state"

    def fail_removing_state(path, *a, **k):
        if Path(path) == target:
            raise OSError("last remove blocked")
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(promote_mod.shutil, "rmtree", fail_removing_state)

    real_copy2 = promote_mod.shutil.copy2

    def fail_restoring_models(s, d, *a, **k):
        if Path(d).name == "a.json":
            raise OSError("injected restore-back failure")
        return real_copy2(s, d, *a, **k)

    monkeypatch.setattr(promote_mod.shutil, "copy2", fail_restoring_models)

    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()

    assert not report.ok
    assert "restore incomplete" in report.summary
    assert "injected restore-back failure" in report.summary


def test_list_v1_archives_reports_a_listing_failure_instead_of_vanishing(tmp_path, monkeypatch):
    from agent_takkub.core.migration.promote_v1 import list_v1_archives

    data_home = tmp_path / "data_home"
    (data_home / "backups").mkdir(parents=True)

    real_iterdir = Path.iterdir

    def fail_iterdir(self):
        if self == data_home / "backups":
            raise OSError("injected iterdir failure")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fail_iterdir)

    archives = list_v1_archives(data_home)
    assert len(archives) == 1
    assert archives[0]["unreadable"] is True
    assert "injected iterdir failure" in archives[0]["error"]
    # `ts=""` (never a real generation timestamp), never `None` — a bare
    # `None` would be read by `ArchiveV1LegacyStep.rollback(archive_ts=
    # None)` as "restore the latest generation" instead of failing closed.
    assert archives[0]["ts"] == ""
