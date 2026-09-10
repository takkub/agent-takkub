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
