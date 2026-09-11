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

import itertools
import time

import pytest

from agent_takkub.core.migration.backup import BackupManager
from agent_takkub.core.migration.promote_v1 import (
    TransferEntry,
    _copy_phase,
    _prune_phase,
    _restore_fsync_batch,
)
from agent_takkub.core.migration.registry_copy_step import write_json_atomic
from agent_takkub.core.migration.wal import STATE_SOURCE_PRUNED, STATE_VERIFIED, TransferLedger


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

    from agent_takkub.core.migration.wal import STATE_PENDING

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


# ---------------------------------------------------------------------------
# #574 round11 item 3 — WITHIN-entry progress (`on_file_progress`): a
# directory entry holding tens of thousands of files used to go completely
# silent between `on_entry` fires (once per whole entry) — a real
# pre-migrate-backup rehearsal sat on ONE entry for 17+ minutes with
# nothing on screen.
# ---------------------------------------------------------------------------


def test_copy_phase_on_file_progress_never_leaves_a_5000_file_entry_silent(tmp_path):
    """Asserted precisely (not by wall-clock, same reasoning as this
    file's other tests): on a 5,000-file directory entry, the gap between
    consecutive `files_done` values `on_file_progress` reports must never
    exceed the 200-file throttle window — real test I/O comfortably
    finishes 200 file-copies in well under the 2-second time-based
    fallback, so a count gap this small is the binding guarantee; the
    time-based fallback itself is covered directly (via a fake clock) in
    `test_core_migration_promote_v1.py`."""
    n = 5000
    src = tmp_path / "src"
    _make_files(src, n)
    dest = tmp_path / "dest"
    backups = BackupManager(tmp_path / "backups")
    entry = TransferEntry("bigdir", "dir", src, dest, paths=tuple(f"f{i}.txt" for i in range(n)))
    ledger = TransferLedger(tmp_path / "wal.json")

    seen: list[tuple[str, int, int, str]] = []

    def on_file_progress(entry_name, files_done, files_total, current_path):
        seen.append((entry_name, files_done, files_total, current_path))

    outcome = _copy_phase([entry], backups, "apply-step", ledger, on_file_progress=on_file_progress)

    assert outcome.ok, outcome.error
    assert seen, "on_file_progress never fired at all"
    assert all(name == "bigdir" and total == n for name, _done, total, _path in seen)
    # Guaranteed final call at done == total (copy phase AND verify phase
    # each fire their own final call).
    assert seen[-1][1] == n
    done_values = [d for _n, d, _t, _p in seen]
    gaps = [b - a for a, b in itertools.pairwise(done_values) if b > a]
    assert max(gaps, default=0) <= 200


# ---------------------------------------------------------------------------
# #504 round10 (R8-P2): the identical O(n^2)-bytes-written shape as above,
# but on the PRUNE side of a restore — `PromoteV2RootStep.rollback()`'s own
# `_prune_phase` call durably rewrote the whole manifest (`write_committed`)
# AND the whole WAL after every SINGLE file's removal. Production rehearsal
# with the copy side already fixed still measured ~3.5 files/s here (WAL
# after a 1800s timeout: {PRUNED 5,208, VERIFIED 10,539}).
# ---------------------------------------------------------------------------


def _write_committed_counter(manifest_path):
    calls = {"n": 0}

    def write_committed(committed_now, failed):
        calls["n"] += 1
        write_json_atomic(
            manifest_path, {"promoted": [e.to_ledger(state="PRUNED") for e in committed_now]}
        )

    return write_committed, calls


def test_prune_phase_batches_manifest_and_wal_writes_not_once_per_file(tmp_path, monkeypatch):
    """The prune-side fix, asserted precisely (not by wall-clock): the
    number of durable `write_committed`/`TransferLedger.write` calls for a
    per-file restore's prune phase must stay a small constant, never track
    file count."""
    n = 1000
    src = tmp_path / "src"
    _make_files(src, n)
    dest = tmp_path / "dest"
    backups = BackupManager(tmp_path / "backups")
    entries = [
        TransferEntry(f"models/f{i}.txt", "file", src / f"f{i}.txt", dest / f"f{i}.txt")
        for i in range(n)
    ]
    ledger = TransferLedger(tmp_path / "wal.json")
    copied = _copy_phase(
        entries, backups, "restore-step", ledger, fsync_every=_restore_fsync_batch(n)
    )
    assert copied.ok, copied.error

    write_committed, write_calls = _write_committed_counter(tmp_path / "manifest.json")
    ledger_write_calls = {"n": 0}
    real_write = TransferLedger.write

    def _counting_write(self, *a, **k):
        ledger_write_calls["n"] += 1
        return real_write(self, *a, **k)

    monkeypatch.setattr(TransferLedger, "write", _counting_write)

    outcome = _prune_phase(entries, write_committed, ledger, fsync_every=_restore_fsync_batch(n))

    assert outcome.ok, outcome.error
    # 1 initial write per copy phase's own checkpoints already happened above;
    # this call's own batched checkpoints (~60) are what's asserted here.
    assert write_calls["n"] < 100, write_calls["n"]
    assert ledger_write_calls["n"] < 100, ledger_write_calls["n"]
    for i in range(n):
        assert not (src / f"f{i}.txt").exists()
        assert (dest / f"f{i}.txt").read_text(encoding="utf-8") == "x"


def test_prune_phase_batch_write_failure_never_falsely_marks_siblings_pruned(tmp_path, monkeypatch):
    """Regression: a batch's own `ledger.write` failing must never leave
    OTHER entries in that same batch looking `SOURCE_PRUNED` in memory —
    a later, successful write (this call's own end-of-run DUPLICATE-
    recording retry, or a resumed call) must not durably persist that
    false claim for files whose removal was never even attempted."""
    n = 20
    src = tmp_path / "src"
    _make_files(src, n)
    dest = tmp_path / "dest"
    backups = BackupManager(tmp_path / "backups")
    entries = [
        TransferEntry(f"models/f{i}.txt", "file", src / f"f{i}.txt", dest / f"f{i}.txt")
        for i in range(n)
    ]
    ledger = TransferLedger(tmp_path / "wal.json")
    copied = _copy_phase(entries, backups, "restore-step", ledger, fsync_every=5)
    assert copied.ok, copied.error

    write_committed, _ = _write_committed_counter(tmp_path / "manifest.json")

    real_write = TransferLedger.write
    calls = {"n": 0}

    def _fail_first_batch_write(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:  # the prune phase's own first batch (5 entries)
            raise OSError("injected batch write failure")
        return real_write(self, *a, **k)

    monkeypatch.setattr(TransferLedger, "write", _fail_first_batch_write)

    outcome = _prune_phase(entries, write_committed, ledger, fsync_every=5)

    assert not outcome.ok
    # #574 round11 R7-M5: a batch-level ledger.write failure touches NO
    # entry in the batch more than any other — `failed_name` still names
    # a representative entry, but `unknown_batch` is what a caller must
    # actually check before treating this as "f0 specifically is a
    # DUPLICATE" (it isn't — none of the 5 are).
    assert outcome.unknown_batch
    assert "models/f0.txt" in outcome.failed_name
    for i in range(5):
        assert (src / f"f{i}.txt").exists(), f"f{i}.txt should not have been removed"
    states = ledger.read()
    for i in range(1, 5):
        rec = states.get(f"models/f{i}.txt", {})
        assert rec.get("state") != "PRUNED", rec


def test_prune_phase_write_committed_batch_failure_reports_unknown_not_duplicate(
    tmp_path, monkeypatch
):
    """#574 round11 R7-M5: a batch-level `write_committed` failure (an I/O
    fault, not tied to any one entry) used to blame `batch[0]` specifically
    as `DUPLICATE` — misleading, since NOTHING in the batch was actually
    touched (removal never even started). Must report `unknown_batch`
    instead, and must never call `write_committed` again claiming any
    entry as DUPLICATE — a `manifest.json` never even gets a second write
    attempt for this failure."""
    n = 5
    src = tmp_path / "src"
    _make_files(src, n)
    dest = tmp_path / "dest"
    backups = BackupManager(tmp_path / "backups")
    entries = [
        TransferEntry(f"models/f{i}.txt", "file", src / f"f{i}.txt", dest / f"f{i}.txt")
        for i in range(n)
    ]
    ledger = TransferLedger(tmp_path / "wal.json")
    copied = _copy_phase(entries, backups, "restore-step", ledger, fsync_every=n)
    assert copied.ok, copied.error

    write_committed_calls: list[list[str]] = []

    def _failing_write_committed(committed_now, failed):
        write_committed_calls.append([e.name for e in committed_now])
        raise OSError("injected write_committed failure")

    outcome = _prune_phase(entries, _failing_write_committed, ledger, fsync_every=n)

    assert not outcome.ok
    assert outcome.unknown_batch
    assert "models/f0.txt" in outcome.failed_name
    # Never a second `write_committed` call trying to record a DUPLICATE —
    # the one call above (which itself failed) is the only attempt.
    assert len(write_committed_calls) == 1
    for i in range(n):
        assert (src / f"f{i}.txt").exists()
    states = ledger.read()
    for i in range(n):
        assert states.get(f"models/f{i}.txt", {}).get("state") != STATE_SOURCE_PRUNED


def test_prune_phase_partial_batch_removal_failure_reverts_untouched_siblings(
    tmp_path, monkeypatch
):
    """#574 round11 item 6 (R7-H1, acceptance review round 7): once a
    batch's own `ledger.write(SOURCE_PRUNED)` succeeds, files are removed
    one at a time — if `_remove_entry_source` then fails partway through
    that SAME batch, every sibling AFTER the failing entry never had its
    own removal attempted at all (source fully present, untouched). Their
    WAL record must never be left claiming `SOURCE_PRUNED` — reverted to
    whatever it was before this batch started, matching this module's own
    "a WAL claim is not proof" contract (R6-B1) rather than lying about
    it. At a real production batch size (~4,078 entries per
    `_restore_fsync_batch`), the OLD behavior meant a single mid-batch
    fault falsely marked thousands of still-fully-present files pruned."""
    n = 5
    src = tmp_path / "src"
    _make_files(src, n)
    dest = tmp_path / "dest"
    backups = BackupManager(tmp_path / "backups")
    entries = [
        TransferEntry(f"models/f{i}.txt", "file", src / f"f{i}.txt", dest / f"f{i}.txt")
        for i in range(n)
    ]
    ledger = TransferLedger(tmp_path / "wal.json")
    copied = _copy_phase(entries, backups, "restore-step", ledger, fsync_every=n)
    assert copied.ok, copied.error

    write_committed, _ = _write_committed_counter(tmp_path / "manifest.json")

    from agent_takkub.core.migration import promote_v1 as promote_mod

    real_remove_entry_source = promote_mod._remove_entry_source

    def _fail_on_f2(entry, digests):
        if entry.name == "models/f2.txt":
            raise OSError("injected mid-batch removal failure")
        return real_remove_entry_source(entry, digests)

    monkeypatch.setattr(promote_mod, "_remove_entry_source", _fail_on_f2)

    # The whole 5-entry list as ONE batch — the exact shape that made the
    # old code claim f3/f4 pruned before ever attempting their removal.
    outcome = _prune_phase(entries, write_committed, ledger, fsync_every=n)

    assert not outcome.ok
    assert outcome.failed_name == "models/f2.txt"

    # f0/f1 were removed before the failure — genuinely, correctly pruned.
    for i in (0, 1):
        assert not (src / f"f{i}.txt").exists()
    # f2 (the failure itself) plus f3/f4 (never attempted) all still have
    # their source fully, physically present.
    for i in (2, 3, 4):
        assert (src / f"f{i}.txt").exists()

    states = ledger.read()
    for i in (3, 4):
        rec = states.get(f"models/f{i}.txt", {})
        assert rec.get("state") != STATE_SOURCE_PRUNED, rec


def test_prune_phase_batched_result_matches_unbatched_result(tmp_path):
    """Batching the manifest/WAL flush must never change the outcome — same
    files removed, same digests, same ok — only how often the durable write
    happens."""
    n = 200
    backups = BackupManager(tmp_path / "backups")

    def _prep(tag):
        src = tmp_path / f"src_{tag}"
        _make_files(src, n)
        dest = tmp_path / f"dest_{tag}"
        entries = [
            TransferEntry(f"models/f{i}.txt", "file", src / f"f{i}.txt", dest / f"f{i}.txt")
            for i in range(n)
        ]
        ledger = TransferLedger(tmp_path / f"wal_{tag}.json")
        copied = _copy_phase(entries, backups, f"step-{tag}", ledger)
        assert copied.ok, copied.error
        write_committed, _ = _write_committed_counter(tmp_path / f"manifest_{tag}.json")
        return entries, ledger, write_committed

    entries_a, ledger_a, wc_a = _prep("a")
    unbatched = _prune_phase(entries_a, wc_a, ledger_a)

    entries_b, ledger_b, wc_b = _prep("b")
    batched = _prune_phase(entries_b, wc_b, ledger_b, fsync_every=_restore_fsync_batch(n))

    assert unbatched.ok and batched.ok
    assert {e.name for e in unbatched.pruned} == {e.name for e in batched.pruned}
    assert unbatched.digests == batched.digests


def test_restore_copy_plus_prune_stays_within_budget_relative_to_apply(tmp_path):
    """#504 round10 (R8-P2) sanity timing check (generous, non-flaky bound):
    a per-file restore's TOTAL copy+prune time through `_copy_phase` +
    `_prune_phase` must land within a small multiple of an equivalent
    single-entry apply's own copy+prune over the SAME files — the copy side
    alone was already covered by round8's own budget test above; this one
    covers the prune side the production rehearsal still found slow after
    that fix landed."""
    n = 10_000
    backups = BackupManager(tmp_path / "backups")

    apply_src = tmp_path / "apply_src"
    _make_files(apply_src, n)
    apply_dest = tmp_path / "apply_dest"
    apply_entry = TransferEntry(
        "models", "dir", apply_src, apply_dest, paths=tuple(f"f{i}.txt" for i in range(n))
    )
    apply_ledger = TransferLedger(tmp_path / "apply_wal.json")
    apply_write_committed, _ = _write_committed_counter(tmp_path / "apply_manifest.json")

    t0 = time.monotonic()
    apply_copy = _copy_phase([apply_entry], backups, "apply-step", apply_ledger)
    assert apply_copy.ok, apply_copy.error
    apply_prune = _prune_phase([apply_entry], apply_write_committed, apply_ledger)
    apply_elapsed = time.monotonic() - t0
    assert apply_prune.ok, apply_prune.error

    restore_src = tmp_path / "restore_src"
    _make_files(restore_src, n)
    restore_dest = tmp_path / "restore_dest"
    restore_entries = [
        TransferEntry(
            f"models/f{i}.txt", "file", restore_src / f"f{i}.txt", restore_dest / f"f{i}.txt"
        )
        for i in range(n)
    ]
    restore_ledger = TransferLedger(tmp_path / "restore_wal.json")
    restore_write_committed, _ = _write_committed_counter(tmp_path / "restore_manifest.json")

    t0 = time.monotonic()
    restore_copy = _copy_phase(
        restore_entries,
        backups,
        "restore-step",
        restore_ledger,
        fsync_every=_restore_fsync_batch(n),
    )
    assert restore_copy.ok, restore_copy.error
    restore_prune = _prune_phase(
        restore_entries,
        restore_write_committed,
        restore_ledger,
        fsync_every=_restore_fsync_batch(n),
    )
    restore_elapsed = time.monotonic() - t0
    assert restore_prune.ok, restore_prune.error

    # Floor avoids false failures when both finish in well under a second;
    # multiplier is generous on purpose — this is a shape check (no
    # per-file quadratic blowup across copy+prune together), not a strict
    # perf gate.
    assert restore_elapsed < max(apply_elapsed * 3, 5.0)
