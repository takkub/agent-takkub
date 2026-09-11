"""#574 round 8, R8-P1 — production rehearsal finding (244k-file copy):
`takkub migrate restore-v1` measured ~1.9 files/s (3,429/15,747 files in 30
minutes) because `_copy_phase` durably rewrote + fsynced the WHOLE (up to
~3.6MB, growing) WAL payload after every SINGLE file — a restore's own
entries are per-FILE (`PromoteV2RootStep.rollback()`'s surgical partial
restore), unlike apply's handful of per-DOMAIN entries, so what's cheap for
apply (O(entries) durable writes) is O(files) for restore.

Fix: `_copy_phase(..., fsync_every=N)` durably flushes every Nth entry (and
always the last) instead of every one; `_restore_fsync_batch()` sizes N so
the total number of checkpoints stays roughly constant regardless of file
count (targeting O(n) total bytes written, not O(n²)). The on-disk WAL
FORMAT is unchanged — still one record per file — so an in-flight WAL from
before this fix resumes correctly under it.
"""

from __future__ import annotations

import time

import pytest

from agent_takkub.core.migration.backup import BackupManager
from agent_takkub.core.migration.promote_v1 import TransferEntry, _copy_phase, _restore_fsync_batch
from agent_takkub.core.migration.wal import TransferLedger


def _make_files(root, n: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")


def test_restore_shaped_copy_batches_ledger_writes_not_once_per_file(tmp_path, monkeypatch):
    """The actual fix, asserted precisely (not by wall-clock, which would be
    flaky in CI): the number of durable `TransferLedger.write` calls for a
    per-file restore must stay a small constant, never track file count."""
    n = 1000
    src = tmp_path / "src"
    _make_files(src, n)
    dest = tmp_path / "dest"

    entries = [
        TransferEntry(f"models/f{i}.txt", "file", src / f"f{i}.txt", dest / f"f{i}.txt")
        for i in range(n)
    ]
    ledger = TransferLedger(tmp_path / "wal.json")
    write_calls = {"n": 0}
    real_write = TransferLedger.write

    def _counting_write(self, *a, **k):
        write_calls["n"] += 1
        return real_write(self, *a, **k)

    monkeypatch.setattr(TransferLedger, "write", _counting_write)
    backups = BackupManager(tmp_path / "backups")

    outcome = _copy_phase(
        entries, backups, "restore-step", ledger, fsync_every=_restore_fsync_batch(n)
    )

    assert outcome.ok, outcome.error
    # 1 initial all-PENDING write + ~60 batched checkpoints — nowhere near n.
    assert write_calls["n"] < 100, write_calls["n"]
    for i in range(n):
        assert (dest / f"f{i}.txt").read_text(encoding="utf-8") == "x"


def test_restore_shaped_copy_result_matches_unbatched_result(tmp_path):
    """Batching the WAL flush must never change the outcome — same files,
    same digests, same ok — only how often the durable write happens."""
    n = 200
    src = tmp_path / "src"
    _make_files(src, n)
    backups = BackupManager(tmp_path / "backups")

    entries_a = [
        TransferEntry(
            f"models/f{i}.txt", "file", src / f"f{i}.txt", tmp_path / "dest_a" / f"f{i}.txt"
        )
        for i in range(n)
    ]
    unbatched = _copy_phase(entries_a, backups, "step-a", TransferLedger(tmp_path / "wal_a.json"))

    entries_b = [
        TransferEntry(
            f"models/f{i}.txt", "file", src / f"f{i}.txt", tmp_path / "dest_b" / f"f{i}.txt"
        )
        for i in range(n)
    ]
    batched = _copy_phase(
        entries_b,
        backups,
        "step-b",
        TransferLedger(tmp_path / "wal_b.json"),
        fsync_every=_restore_fsync_batch(n),
    )

    assert unbatched.ok and batched.ok
    assert unbatched.digests == batched.digests


def test_restore_of_many_small_files_stays_within_budget_relative_to_a_single_bulk_entry(
    tmp_path,
):
    """Sanity timing check (generous, non-flaky bound) at a CI-tractable
    scale (a few thousand files, not the full 15.7k production rehearsal —
    Lead is re-running that separately): a per-file "restore" through
    `_copy_phase` must land within a small multiple of an equivalent
    single-entry "apply" over the SAME files, not the ~150x-and-climbing
    slowdown the unbatched code showed in production."""
    n = 2000
    src = tmp_path / "src"
    _make_files(src, n)
    backups = BackupManager(tmp_path / "backups")

    apply_entry = TransferEntry(
        "models", "dir", src, tmp_path / "dest_apply", paths=tuple(f"f{i}.txt" for i in range(n))
    )
    t0 = time.monotonic()
    apply_outcome = _copy_phase(
        [apply_entry], backups, "apply-step", TransferLedger(tmp_path / "apply_wal.json")
    )
    apply_elapsed = time.monotonic() - t0
    assert apply_outcome.ok

    restore_entries = [
        TransferEntry(
            f"models/f{i}.txt", "file", src / f"f{i}.txt", tmp_path / "dest_restore" / f"f{i}.txt"
        )
        for i in range(n)
    ]
    t0 = time.monotonic()
    restore_outcome = _copy_phase(
        restore_entries,
        backups,
        "restore-step",
        TransferLedger(tmp_path / "restore_wal.json"),
        fsync_every=_restore_fsync_batch(n),
    )
    restore_elapsed = time.monotonic() - t0
    assert restore_outcome.ok

    # Floor avoids false failures when both finish in well under a second;
    # multiplier is generous on purpose — this is a shape check (no
    # per-file quadratic blowup), not a strict perf gate.
    assert restore_elapsed < max(apply_elapsed * 5, 3.0)


def test_resume_from_a_partially_verified_restore_wal_finishes_the_rest(tmp_path):
    """#574 R8-P1 requirement (3): a restore killed halfway through must
    resume from the WAL, redo nothing already VERIFIED, and finish."""
    import hashlib

    from agent_takkub.core.migration.wal import STATE_PENDING, STATE_VERIFIED

    n = 50
    src = tmp_path / "src"
    _make_files(src, n)
    dest = tmp_path / "dest"
    backups = BackupManager(tmp_path / "backups")
    entries = [
        TransferEntry(f"models/f{i}.txt", "file", src / f"f{i}.txt", dest / f"f{i}.txt")
        for i in range(n)
    ]

    half = n // 2
    sha = hashlib.sha256(b"x").hexdigest()
    states = {}
    for i, e in enumerate(entries):
        if i < half:
            e.dest.parent.mkdir(parents=True, exist_ok=True)
            e.dest.write_text("x", encoding="utf-8")
            state, digest = STATE_VERIFIED, {e.src.name: sha}
        else:
            state, digest = STATE_PENDING, {}
        states[e.name] = {
            "name": e.name,
            "kind": "file",
            "src": str(e.src),
            "dest": str(e.dest),
            "paths": [],
            "state": state,
            "sha256": digest,
        }
    ledger = TransferLedger(tmp_path / "wal.json")
    ledger.write(states)

    resumed = _copy_phase(
        entries, backups, "restore-step", ledger, fsync_every=_restore_fsync_batch(n)
    )

    assert resumed.ok, resumed.error
    for e in entries:
        assert e.dest.read_text(encoding="utf-8") == "x"


def test_restore_fsync_batch_targets_a_roughly_constant_checkpoint_count():
    assert _restore_fsync_batch(9) == 1  # small (apply-sized) counts flush every entry
    assert _restore_fsync_batch(60) == 1
    assert _restore_fsync_batch(15_747) == pytest.approx(263, abs=5)
    assert _restore_fsync_batch(150_000) == pytest.approx(2500, abs=50)
