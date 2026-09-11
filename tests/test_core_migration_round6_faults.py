"""#574 round 6 acceptance review addendum — R6-B1 (latent BLOCKER:
`_copy_phase` trusted a WAL `SOURCE_PRUNED` record without re-verifying the
target), R6-M1 (digest dict keyed inconsistently between `_source_digests`/
`verify_only` and `_verified_target_intact`, silently defeating resume for
any nested-name file entry), and R6-M2 (a live-write collision used to
raise and block every retry identically forever).

R6-H1 (the boot-path consequence of B1 — a boot converging on a false
"pending_applied" while validate stays red forever) is covered at the
`boot_flow.MigrationOutcome` layer in test_boot_flow.py instead: that is
the layer a human/UI actually observes, and it's what makes 3 consecutive
"boots" after a stale failure converge on one stable, honest result rather
than a fake success — see
`TestRunMigrationOutcome::test_three_consecutive_stale_failures_converge_on_the_same_honest_result`.
"""

from __future__ import annotations

import hashlib

from agent_takkub.core.migration.backup import BackupManager
from agent_takkub.core.migration.promote_v1 import (
    TransferEntry,
    _copy_phase,
    _verified_target_intact,
)
from agent_takkub.core.migration.verify_copy import copy_only, copy_verified
from agent_takkub.core.migration.wal import STATE_SOURCE_PRUNED, TransferLedger


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# R6-B1 — SOURCE_PRUNED must re-verify, never trust the WAL blindly
# ---------------------------------------------------------------------------


def test_r6_b1_recopies_when_source_pruned_target_corrupted_but_source_still_exists(tmp_path):
    """A WAL claiming SOURCE_PRUNED is a claim, not proof — if the target no
    longer matches its recorded checksum AND the source turns out to still
    be there (the claim was stale), this must recover by re-copying, not
    silently report success with a corrupted target."""
    src = tmp_path / "src.txt"
    src.write_text("real content", encoding="utf-8")
    dest = tmp_path / "dest.txt"
    dest.write_text("CORRUPTED", encoding="utf-8")
    entry = TransferEntry("x", "file", src, dest)

    ledger = TransferLedger(tmp_path / "wal.json")
    ledger.write(
        {
            "x": {
                "name": "x",
                "kind": "file",
                "src": str(src),
                "dest": str(dest),
                "paths": [],
                "state": STATE_SOURCE_PRUNED,
                "sha256": {"src.txt": _sha(b"real content")},
            }
        }
    )
    backups = BackupManager(tmp_path / "backups")

    outcome = _copy_phase([entry], backups, "test-step", ledger)

    assert outcome.ok, outcome.error
    assert dest.read_text(encoding="utf-8") == "real content"


def test_r6_b1_fails_missing_both_when_source_pruned_target_corrupted_and_source_gone(tmp_path):
    """When BOTH the recorded target and the source are gone/wrong, there is
    nothing left to recover from — this must fail loudly (ok=False,
    "missing both"), never silently report a complete copy."""
    src = tmp_path / "src.txt"  # never created — genuinely gone
    dest = tmp_path / "dest.txt"
    dest.write_text("CORRUPTED", encoding="utf-8")
    entry = TransferEntry("x", "file", src, dest)

    ledger = TransferLedger(tmp_path / "wal.json")
    ledger.write(
        {
            "x": {
                "name": "x",
                "kind": "file",
                "src": str(src),
                "dest": str(dest),
                "paths": [],
                "state": STATE_SOURCE_PRUNED,
                "sha256": {"src.txt": "0" * 64},
            }
        }
    )
    backups = BackupManager(tmp_path / "backups")

    outcome = _copy_phase([entry], backups, "test-step", ledger)

    assert not outcome.ok
    assert "missing both" in outcome.error


def test_r6_b1_still_trusts_a_genuinely_intact_source_pruned_target(tmp_path):
    """The fix must not turn every resumed SOURCE_PRUNED entry into a
    re-copy — a target that DOES match its recorded checksum is still
    trusted, exactly like before."""
    src = tmp_path / "src.txt"  # already pruned for real — gone
    dest = tmp_path / "dest.txt"
    dest.write_text("real content", encoding="utf-8")
    entry = TransferEntry("x", "file", src, dest)

    ledger = TransferLedger(tmp_path / "wal.json")
    ledger.write(
        {
            "x": {
                "name": "x",
                "kind": "file",
                "src": str(src),
                "dest": str(dest),
                "paths": [],
                "state": STATE_SOURCE_PRUNED,
                "sha256": {"src.txt": _sha(b"real content")},
            }
        }
    )
    backups = BackupManager(tmp_path / "backups")

    outcome = _copy_phase([entry], backups, "test-step", ledger)

    assert outcome.ok
    assert outcome.digests["x"] == {"src.txt": _sha(b"real content")}


# ---------------------------------------------------------------------------
# R6-M1 — digest dict key must match `verify_only`'s own convention
# ---------------------------------------------------------------------------


def test_r6_m1_verified_target_intact_uses_source_basename_for_nested_named_file_entry(tmp_path):
    """An archive/shared-dir-legacy FILE entry's `name` can hold a relative
    path with "/" (e.g. "roles/custom.json") while `entry.src.name` is just
    the bare filename ("custom.json") — `verify_only()`/`_source_digests()`
    both key their digest dict by the bare filename, so this guard must
    too, or it silently never trusts a resumed target for any such entry
    (a dead no-op: `expected.get(entry.name)` was always `None`)."""
    nested_src = tmp_path / "roles" / "custom.json"
    nested_src.parent.mkdir()
    nested_src.write_text("{}", encoding="utf-8")
    dest = tmp_path / "archived" / "roles" / "custom.json"
    dest.parent.mkdir(parents=True)
    dest.write_text("{}", encoding="utf-8")

    entry = TransferEntry("roles/custom.json", "file", nested_src, dest)
    rec = {"sha256": {"custom.json": _sha(b"{}")}}  # keyed by bare filename, not entry.name

    assert _verified_target_intact(entry, rec) is True


# ---------------------------------------------------------------------------
# R6-M2 — a live-write collision is kept as a duplicate, never blocks forever
# ---------------------------------------------------------------------------


def test_r6_m2_copy_only_keeps_a_colliding_live_file_as_a_duplicate_and_proceeds(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text("real", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "a.json").write_text("LIVE-DIFFERENT-CONTENT", encoding="utf-8")

    duplicated = copy_only(src, dest)

    assert duplicated == ["a.json"]
    assert (dest / "a.json").read_text(encoding="utf-8") == "real"  # this transaction's copy landed
    dup_files = list(dest.glob("a.json.duplicate-*"))
    assert len(dup_files) == 1
    assert (
        dup_files[0].read_text(encoding="utf-8") == "LIVE-DIFFERENT-CONTENT"
    )  # preserved, not lost


def test_r6_m2_a_retry_after_a_collision_no_longer_hits_the_same_wall(tmp_path):
    """The whole point: nothing here ever removes the live file on its
    own, so a retry used to hit the identical collision every time. Two
    back-to-back `copy_only` calls (simulating a retry) must both succeed —
    never a second raise."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text("real", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "a.json").write_text("LIVE", encoding="utf-8")

    first = copy_only(src, dest)
    assert first == ["a.json"]
    # A second run against the now-clean dest finds no collision at all.
    second = copy_only(src, dest)
    assert second == []


def test_r6_m2_copy_verified_surfaces_duplicates_kept():
    pass


def test_r6_m2_copy_verified_surfaces_duplicates(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text("real", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "a.json").write_text("LIVE", encoding="utf-8")

    result = copy_verified(src, dest)

    assert result.duplicates == ("a.json",)


def test_r6_m2_copy_phase_surfaces_duplicates_kept_in_its_outcome(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text("real", encoding="utf-8")
    dest_parent = tmp_path / "dest_parent"
    dest = dest_parent / "sub"
    dest.mkdir(parents=True)
    (dest / "a.json").write_text("LIVE", encoding="utf-8")
    entry = TransferEntry("sub", "dir", src, dest, paths=("a.json",))

    ledger = TransferLedger(tmp_path / "wal.json")
    backups = BackupManager(tmp_path / "backups")

    outcome = _copy_phase([entry], backups, "test-step", ledger)

    assert outcome.ok, outcome.error
    assert outcome.duplicates_kept == {"sub": ("a.json",)}
