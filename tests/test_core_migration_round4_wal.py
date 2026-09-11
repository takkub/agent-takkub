"""#504 round 4 acceptance review (docs/audit/2026-09-10-504-acceptance-review.md
"## Round 4 — eda919ea") + round 5 task spec T1-T7 — mechanical regression tests
for the WAL-based promote/archive transaction core in
`agent_takkub.core.migration.promote_v1`/`wal.py`.

Each test is named after the finding it closes (T1-T7 from the round-5 task,
R4-* from the acceptance review addendum). Run with:

    PYTHONPATH=src pytest tests/test_core_migration_round4_wal.py -q
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

from agent_takkub.core.migration.backup import BackupManager
from agent_takkub.core.migration.journal import MigrationJournal
from agent_takkub.core.migration.promote_v1 import (
    ArchiveV1LegacyStep,
    CommandSnapshotError,
    PromoteV2RootStep,
    TransferEntry,
    _begin_command_snapshot,
    _revert_to_command_snapshot,
    list_v1_archives,
)
from agent_takkub.core.migration.wal import (
    STATE_PENDING,
    STATE_VERIFIED,
    TransferLedger,
    write_json_durable,
)


@pytest.fixture
def journal_backups(tmp_path):
    store_dir = tmp_path / "migration_home"
    from agent_takkub.core.storage.jsonl_store import JsonlStore

    journal = MigrationJournal(store=JsonlStore(store_dir / "journal.jsonl"))
    backups = BackupManager(root=store_dir / "migration_backups")
    return journal, backups


# ---------------------------------------------------------------------------
# T1 — WAL-before-first-byte
# ---------------------------------------------------------------------------


def test_t1_wal_ledger_exists_with_every_entry_pending_before_first_copy(
    tmp_path, journal_backups, monkeypatch
):
    """Inject a failure on the FIRST file's copy — the ledger must already
    be on disk, durably, naming EVERY entry PENDING, before that first copy
    ever ran (not written after the fact once the whole batch finishes)."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "config").mkdir(parents=True)
    (data_home / "v2" / "config" / "x.json").write_text("x", encoding="utf-8")
    (data_home / "v2" / "models").mkdir(parents=True)
    (data_home / "v2" / "models" / "y.json").write_text("y", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    ledger_snapshot: dict = {}

    def _fail_first(src, dest):
        # At the moment of the very first copy attempt, the WAL must
        # already exist with every candidate recorded PENDING.
        step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
        ledger = TransferLedger(step._wal_path(), list_key="promoted")
        assert ledger.exists(), "WAL must exist before the first copy runs"
        states = ledger.read()
        ledger_snapshot.update(states)
        raise OSError("injected first-copy failure")

    monkeypatch.setattr(promote_mod, "copy_verified", _fail_first)
    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    report = step.apply()

    assert not report.ok
    assert set(ledger_snapshot) == {"config", "models"}
    assert all(rec["state"] == STATE_PENDING for rec in ledger_snapshot.values())


# ---------------------------------------------------------------------------
# T2 — durable fsync on every ledger write, before the next state transition
# ---------------------------------------------------------------------------


def test_t2_ledger_writes_fsync_before_the_next_transition(tmp_path, monkeypatch):
    """Spy on `os.fsync` — it must be called at least once per durable
    ledger write, and every ledger write must land on disk (readable back)
    before the NEXT transition happens."""
    calls = {"n": 0}
    real_fsync = os.fsync

    def spy_fsync(fd):
        calls["n"] += 1
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)

    ledger_path = tmp_path / "wal.json"
    ledger = TransferLedger(ledger_path)
    entry = TransferEntry("x", "file", tmp_path / "src" / "x.json", tmp_path / "dest" / "x.json")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.json").write_text("hello", encoding="utf-8")

    before = calls["n"]
    ledger.write({entry.name: {"name": "x", "kind": "file", "state": STATE_PENDING}})
    assert calls["n"] > before
    assert ledger.read()["x"]["state"] == STATE_PENDING

    before = calls["n"]
    ledger.write({entry.name: {"name": "x", "kind": "file", "state": STATE_VERIFIED}})
    assert calls["n"] > before
    assert ledger.read()["x"]["state"] == STATE_VERIFIED


# ---------------------------------------------------------------------------
# T3 — per-entry state machine + idempotent replay at every transition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("crash_after", [STATE_PENDING, STATE_VERIFIED])
def test_t3_replay_after_crash_at_each_transition_is_idempotent(
    tmp_path, journal_backups, monkeypatch, crash_after
):
    """Simulate a crash right after the ledger records each of PENDING,
    VERIFIED for one entry (a second, unaffected entry sanity-checks the
    rest of the batch) — a resumed `apply()` must finish cleanly, own
    every file, and never re-derive a smaller file list from a live
    rescan."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "state").mkdir(parents=True)
    (data_home / "v2" / "state" / "a.json").write_text("a", encoding="utf-8")
    (data_home / "v2" / "state" / "b.json").write_text("b", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    ledger = TransferLedger(step._wal_path())

    real_copy_verified = promote_mod.copy_verified
    real_remove = promote_mod._remove

    class _Crash(Exception):
        pass

    def _copy_then_crash(src, dest):
        if crash_after == STATE_PENDING:
            # crash BEFORE the copy even lands anything at dest
            raise _Crash()
        return real_copy_verified(src, dest)

    def _remove_then_crash(path):
        real_remove(path)  # STATE_VERIFIED: copy+verify finished normally
        raise _Crash("simulated crash right after a real source removal")

    if crash_after == STATE_PENDING:
        monkeypatch.setattr(promote_mod, "copy_verified", _copy_then_crash)
    else:
        monkeypatch.setattr(promote_mod, "_remove", _remove_then_crash)

    with pytest.raises(_Crash):
        step.apply()
    monkeypatch.undo()

    assert ledger.exists(), "a crashed run must leave its WAL behind"

    # Resumed retry: must finish, own both files, no data lost.
    retry = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    report = retry.apply()
    assert report.ok, report.summary
    assert (data_home / "state" / "a.json").read_text(encoding="utf-8") == "a"
    assert (data_home / "state" / "b.json").read_text(encoding="utf-8") == "b"
    assert not ledger.exists()

    rollback_report = retry.rollback()
    assert rollback_report.ok, rollback_report.summary
    assert (data_home / "v2" / "state" / "a.json").read_text(encoding="utf-8") == "a"
    assert (data_home / "v2" / "state" / "b.json").read_text(encoding="utf-8") == "b"


# ---------------------------------------------------------------------------
# T4 — AST guard: no bare except-swallow in a delete/write path anywhere in
# core/migration/, and every OTHER bare swallow must carry a documented
# reason.
# ---------------------------------------------------------------------------

_MIGRATION_DIR = Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "core" / "migration"

_MUTATING_NAMES = {
    "unlink",
    "rmtree",
    "rmdir",
    "remove",
    "replace",
    "write_text",
    "write_bytes",
    "copy2",
    "copytree",
    "_remove",
    "_remove_entry_source",
    "_remove_verified_file",
    "write_json_atomic",
    "write_json_durable",
}


def _call_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                names.add(f.attr)
            elif isinstance(f, ast.Name):
                names.add(f.id)
    return names


def _is_bare_swallow(handler: ast.ExceptHandler) -> bool:
    if len(handler.body) != 1:
        return False
    stmt = handler.body[0]
    if isinstance(stmt, (ast.Pass, ast.Continue)):
        return True
    if isinstance(stmt, ast.Return) and (stmt.value is None or _is_none_const(stmt.value)):
        return True
    return False


def _is_none_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _comment_lines(path: Path) -> dict[int, str]:
    out: dict[int, str] = {}
    with open(path, "rb") as f:
        for tok in tokenize.tokenize(f.readline):
            if tok.type == tokenize.COMMENT:
                out[tok.start[0]] = tok.string
    return out


def test_t4_no_unexplained_or_delete_write_path_bare_except_swallow():
    violations: list[str] = []
    for py_file in sorted(_MIGRATION_DIR.glob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        comments = _comment_lines(py_file)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            mutating = bool(_call_names(node) & _MUTATING_NAMES)
            for handler in node.handlers:
                if not _is_bare_swallow(handler):
                    continue
                if mutating:
                    violations.append(
                        f"{py_file.name}:{handler.lineno}: bare except-swallow in a "
                        f"delete/write path (calls {_call_names(node) & _MUTATING_NAMES})"
                    )
                    continue
                # Read-only swallow: must carry a documented reason,
                # somewhere between the `except` line and the end of its
                # single statement.
                stmt_line = handler.body[0].lineno
                has_reason = any(
                    "swallow-ok:" in comments.get(ln, "")
                    for ln in range(handler.lineno, stmt_line + 3)
                )
                if not has_reason:
                    violations.append(
                        f"{py_file.name}:{handler.lineno}: bare except-swallow with no "
                        f"'# swallow-ok:' reason"
                    )
    assert not violations, "\n".join(violations)


# ---------------------------------------------------------------------------
# T5 / R4-B1 — restore-v1 command snapshot: fail closed, verified revert
# ---------------------------------------------------------------------------


def test_t5_begin_command_snapshot_enospc_aborts_before_touching_any_target(tmp_path, monkeypatch):
    data_home = tmp_path / "data_home"
    (data_home / "a.json").parent.mkdir(parents=True)
    (data_home / "a.json").write_text("A", encoding="utf-8")
    (data_home / "b.json").write_text("B", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_copy2 = promote_mod.shutil.copy2

    def _enospc_on_b(src, dst, *a, **k):
        if Path(src).name == "b.json":
            raise OSError("[Errno 28] No space left on device")
        return real_copy2(src, dst, *a, **k)

    monkeypatch.setattr(promote_mod.shutil, "copy2", _enospc_on_b)

    with pytest.raises(CommandSnapshotError):
        _begin_command_snapshot(data_home, ["a.json", "b.json"])

    # Nothing at data_home was touched — snapshot only ever writes under
    # migration_home(), never data_home itself.
    assert (data_home / "a.json").read_text(encoding="utf-8") == "A"
    assert (data_home / "b.json").read_text(encoding="utf-8") == "B"


def test_t5_revert_refuses_to_delete_current_when_preimage_is_unverifiable(tmp_path):
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "same.json").write_text("ORIGINAL", encoding="utf-8")

    snapshot = _begin_command_snapshot(data_home, ["same.json"])
    # Corrupt the just-taken preimage so it no longer verifies.
    saved = snapshot.root / "same.json"
    saved.write_text("CORRUPTED", encoding="utf-8")

    (data_home / "same.json").write_text("CURRENT", encoding="utf-8")

    errors = _revert_to_command_snapshot(data_home, snapshot)

    assert errors, "an unverifiable preimage must be reported, not silently reverted"
    # CURRENT was never deleted — there was no verified preimage to put
    # back in its place ("0 copies" BLOCKER from round 4).
    assert (data_home / "same.json").read_text(encoding="utf-8") == "CURRENT"


def test_t5_revert_succeeds_when_preimage_is_intact(tmp_path):
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "same.json").write_text("ORIGINAL", encoding="utf-8")

    snapshot = _begin_command_snapshot(data_home, ["same.json"])
    (data_home / "same.json").write_text("CURRENT", encoding="utf-8")

    errors = _revert_to_command_snapshot(data_home, snapshot)

    assert not errors
    assert (data_home / "same.json").read_text(encoding="utf-8") == "ORIGINAL"


# ---------------------------------------------------------------------------
# T6 — real process kill mid-rmtree/prune, x3 boot replay, full recovery
# ---------------------------------------------------------------------------


_CRASH_SCRIPT = """
import sys
sys.path.insert(0, {src!r})
from agent_takkub.core.migration.promote_v1 import PromoteV2RootStep
from agent_takkub.core.migration.journal import MigrationJournal
from agent_takkub.core.migration.backup import BackupManager
from agent_takkub.core.storage.jsonl_store import JsonlStore
from pathlib import Path
import os

data_home = Path({data_home!r})
store_dir = Path({store_dir!r})
journal = MigrationJournal(store=JsonlStore(store_dir / "journal.jsonl"))
backups = BackupManager(root=store_dir / "migration_backups")
step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)

import agent_takkub.core.migration.promote_v1 as promote_mod

target_dir = data_home / "v2" / "state"

def killing_remove(path):
    if path == target_dir:
        # Simulate a real process death partway through this directory's
        # removal: one of its files really gets deleted, then the process
        # dies before the rest (or the WAL update after it) completes.
        next(iter(sorted(path.glob("*.json")))).unlink()
        os._exit(17)
    return real_remove(path)

real_remove = promote_mod._remove
promote_mod._remove = killing_remove
step.apply()
"""


def test_t6_process_kill_mid_prune_then_three_boot_replays_recovers_fully(tmp_path):
    data_home = tmp_path / "data_home"
    store_dir = tmp_path / "migration_home"
    (data_home / "v2" / "models" / "a.json").parent.mkdir(parents=True)
    (data_home / "v2" / "models" / "a.json").write_text("a", encoding="utf-8")
    (data_home / "v2" / "state").mkdir(parents=True)
    (data_home / "v2" / "state" / "b.json").write_text("b", encoding="utf-8")
    (data_home / "v2" / "state" / "c.json").write_text("c", encoding="utf-8")

    src_root = str(Path(__file__).resolve().parents[1] / "src")
    script = _CRASH_SCRIPT.format(src=src_root, data_home=str(data_home), store_dir=str(store_dir))
    script_path = tmp_path / "crash_mid_prune.py"
    script_path.write_text(script, encoding="utf-8")

    result = subprocess.run([sys.executable, str(script_path)], capture_output=True, timeout=60)
    assert result.returncode == 17, result.stderr.decode("utf-8", "replace")

    journal = MigrationJournal(
        store=__import__(
            "agent_takkub.core.storage.jsonl_store", fromlist=["JsonlStore"]
        ).JsonlStore(store_dir / "journal.jsonl")
    )
    backups = BackupManager(root=store_dir / "migration_backups")

    # Boot replay x3 — every time must be safe/idempotent.
    for _ in range(3):
        step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
        report = step.apply()
        assert report.ok, report.summary

    assert (data_home / "models" / "a.json").read_text(encoding="utf-8") == "a"
    assert (data_home / "state" / "b.json").read_text(encoding="utf-8") == "b"
    assert (data_home / "state" / "c.json").read_text(encoding="utf-8") == "c"

    # restore-v1 (rollback) must recover everything, including the entry
    # whose directory removal was killed mid-flight.
    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    rollback_report = step.rollback()
    assert rollback_report.ok, rollback_report.summary
    assert (data_home / "v2" / "models" / "a.json").read_text(encoding="utf-8") == "a"
    assert (data_home / "v2" / "state" / "b.json").read_text(encoding="utf-8") == "b"
    assert (data_home / "v2" / "state" / "c.json").read_text(encoding="utf-8") == "c"


# ---------------------------------------------------------------------------
# T7 — copies >= 1 property across every injection point above
# ---------------------------------------------------------------------------


def _every_copy(root: Path, name: str) -> list[Path]:
    return [p for p in root.rglob(name) if p.is_file()]


@pytest.mark.parametrize(
    "inject",
    ["copy_only_fail", "verify_fail", "remove_fail", "prune_ledger_fail"],
)
def test_t7_copies_never_drop_to_zero_across_injection_points(
    tmp_path, journal_backups, monkeypatch, inject
):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "models").mkdir(parents=True)
    (data_home / "v2" / "models" / "only.json").write_text("unique-content", encoding="utf-8")
    (data_home / "v2" / "state").mkdir(parents=True)
    (data_home / "v2" / "state" / "other.json").write_text("other-content", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    if inject == "copy_only_fail":
        real = promote_mod.copy_verified

        def hook(src, dest):
            if src.name == "state":
                raise OSError("injected")
            return real(src, dest)

        monkeypatch.setattr(promote_mod, "copy_verified", hook)
    elif inject == "verify_fail":
        real = promote_mod.copy_verified

        def hook(src, dest):
            if src.name == "state":
                raise promote_mod.VerifyMismatchError("injected mismatch")
            return real(src, dest)

        monkeypatch.setattr(promote_mod, "copy_verified", hook)
    elif inject == "remove_fail":
        real_rmtree = promote_mod.shutil.rmtree
        target = data_home / "v2" / "state"

        def hook(path, *a, **k):
            if Path(path) == target:
                raise OSError("injected")
            return real_rmtree(path, *a, **k)

        monkeypatch.setattr(promote_mod.shutil, "rmtree", hook)
    elif inject == "prune_ledger_fail":
        real_write = TransferLedger.write
        state = {"n": 0}

        def hook(self, entries, meta=None):
            state["n"] += 1
            if state["n"] == 3:  # deep enough to hit a prune-phase write
                raise OSError("injected ledger write failure")
            return real_write(self, entries, meta)

        monkeypatch.setattr(TransferLedger, "write", hook)

    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
    step.apply()  # ok or not — irrelevant to the invariant below

    assert len(_every_copy(tmp_path, "only.json")) >= 1
    assert len(_every_copy(tmp_path, "other.json")) >= 1


# ---------------------------------------------------------------------------
# R4-B2 — per-file removal with hash-recheck; a late write is kept, not lost
# ---------------------------------------------------------------------------


def test_r4b2_prune_never_removes_a_file_that_changed_since_verify(tmp_path, journal_backups):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "state").mkdir(parents=True)
    (data_home / "v2" / "state" / "a.json").write_text("a-original", encoding="utf-8")
    (data_home / "v2" / "state" / "b.json").write_text("b", encoding="utf-8")

    step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_copy_verified = promote_mod.copy_verified
    written_late = {"done": False}

    def verify_then_late_write(src, dest):
        result = real_copy_verified(src, dest)
        if src.name == "state" and not written_late["done"]:
            written_late["done"] = True
            # A live writer touches one file under the V1 source AFTER
            # this transaction's own verify already ran, but BEFORE prune
            # gets to remove it.
            (data_home / "v2" / "state" / "a.json").write_text("a-LATE-WRITE", encoding="utf-8")
        return result

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(promote_mod, "copy_verified", verify_then_late_write)
        report = step.apply()
    finally:
        monkeypatch.undo()

    assert report.ok, report.summary
    # The late-written file must be KEPT at its original location — never
    # deleted unverified — and its late content preserved (not clobbered).
    assert (data_home / "v2" / "state" / "a.json").read_text(encoding="utf-8") == "a-LATE-WRITE"
    # The other, untouched file was pruned normally.
    assert not (data_home / "v2" / "state" / "b.json").exists()
    assert (data_home / "state" / "b.json").read_text(encoding="utf-8") == "b"

    # A later, unaffected apply() call finishes the job — the kept file is
    # picked up and promoted too, once it stops changing underneath it.
    follow_up = step.apply()
    assert follow_up.ok, follow_up.summary
    assert not (data_home / "v2" / "state").exists()
    assert (data_home / "state" / "a.json").read_text(encoding="utf-8") == "a-LATE-WRITE"


# ---------------------------------------------------------------------------
# R4-B3 — archive manifest header written PENDING before first copy;
# incomplete generations are listed as such and skipped by "latest".
# ---------------------------------------------------------------------------


def test_r4b3_manifest_header_written_pending_before_first_copy(
    tmp_path, journal_backups, monkeypatch
):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "a.json").write_text("a", encoding="utf-8")
    (data_home / "b.json").write_text("b", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    seen_state = {}

    def hook(src, dest):
        # At the very first copy attempt, the generation's manifest.json
        # must already exist on disk with state PENDING.
        gens = list_v1_archives(data_home)
        if gens:
            manifest_path = Path(gens[0]["path"]) / "manifest.json"
            seen_state["state"] = json.loads(manifest_path.read_text(encoding="utf-8")).get("state")
        raise OSError("injected — we only care about pre-copy state")

    monkeypatch.setattr(promote_mod, "copy_verified", hook)
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    step.apply()

    assert seen_state.get("state") == "PENDING"


def test_r4b3_incomplete_generation_listed_and_skipped_by_latest(tmp_path, journal_backups):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    (data_home / "a.json").write_text("a", encoding="utf-8")
    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    assert step.apply().ok

    complete_gen = list_v1_archives(data_home)[0]
    assert complete_gen["incomplete"] is False

    # Hand-write a second, PENDING-only generation (as if a crash happened
    # right after the header write, before any copy completed).
    pending_root = data_home / "backups" / "v1-archive-9999999999.000000"
    write_json_durable(
        pending_root / "manifest.json",
        {"schema": 2, "created_at": 0, "state": "PENDING", "archived": [], "deleted": []},
    )

    archives = list_v1_archives(data_home)
    by_ts = {a["ts"]: a for a in archives}
    assert by_ts["9999999999.000000"]["incomplete"] is True
    assert by_ts[complete_gen["ts"]]["incomplete"] is False

    from agent_takkub.core.migration.promote_v1 import _find_latest_manifest

    latest = _find_latest_manifest(data_home)
    assert latest is not None
    assert latest.parent.name != "v1-archive-9999999999.000000"


# ---------------------------------------------------------------------------
# R4-H3 — reconstruction failure keeps the entry committed, never dropped
# ---------------------------------------------------------------------------


def test_r4h3_entry_stays_committed_when_source_reconstruction_fails(tmp_path, journal_backups):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "models" / "a.json").parent.mkdir(parents=True)
    (data_home / "v2" / "models" / "a.json").write_text("a", encoding="utf-8")
    (data_home / "v2" / "state" / "b.json").parent.mkdir(parents=True)
    (data_home / "v2" / "state" / "b.json").write_text("b", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_rmtree = promote_mod.shutil.rmtree
    target = data_home / "v2" / "state"

    def fail_second(path, *a, **k):
        if Path(path) == target:
            raise OSError("last remove blocked")
        return real_rmtree(path, *a, **k)

    real_copy2 = promote_mod.shutil.copy2

    def fail_reconstruction(s, d, *a, **k):
        # The reconstruction of "models" (the entry that succeeded before
        # "state" failed) itself fails too.
        if Path(d).name == "a.json":
            raise OSError("reconstruction also failed")
        return real_copy2(s, d, *a, **k)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(promote_mod.shutil, "rmtree", fail_second)
        monkeypatch.setattr(promote_mod.shutil, "copy2", fail_reconstruction)
        step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
        report = step.apply()
    finally:
        monkeypatch.undo()

    assert not report.ok
    # "models"'s only safe copy is at `dest` now (source reconstruction
    # failed) — it must STILL be recorded in the manifest, never dropped.
    manifest = json.loads(step._manifest_path().read_text(encoding="utf-8"))
    names = {e["name"] for e in manifest["promoted"]}
    assert "models" in names, "an entry whose reconstruction failed must stay committed"
    assert (data_home / "models" / "a.json").read_text(encoding="utf-8") == "a"


# ---------------------------------------------------------------------------
# R4-L1 — summary wording must not claim "nothing lost" when restore failed
# ---------------------------------------------------------------------------


def test_r4l1_summary_never_claims_nothing_lost_when_a_duplicate_is_left(tmp_path, journal_backups):
    """#504 round4 B1 (superseding the older R2-B1-era "restore incomplete"
    contract this test used to cover, see the `promote_v1.py` twin test's
    docstring): a denied removal's summary must still be honest that
    something needs an operator's attention — a DUPLICATE, not silently
    "nothing lost"."""
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "v2" / "models" / "a.json").parent.mkdir(parents=True)
    (data_home / "v2" / "models" / "a.json").write_text("a", encoding="utf-8")
    (data_home / "v2" / "state" / "b.json").parent.mkdir(parents=True)
    (data_home / "v2" / "state" / "b.json").write_text("b", encoding="utf-8")

    import agent_takkub.core.migration.promote_v1 as promote_mod

    real_rmtree = promote_mod.shutil.rmtree
    target = data_home / "v2" / "state"

    def fail_second(path, *a, **k):
        if Path(path) == target:
            raise OSError("last remove blocked")
        return real_rmtree(path, *a, **k)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(promote_mod.shutil, "rmtree", fail_second)
        step = PromoteV2RootStep(journal=journal, backups=backups, data_home=data_home)
        report = step.apply()
    finally:
        monkeypatch.undo()

    assert not report.ok
    assert "DUPLICATE" in report.summary
    assert "nothing lost" not in report.summary


# ---------------------------------------------------------------------------
# R4-H4 — shared-dir-legacy candidates respect the registered-account-home
# check too, not just top-level candidates.
# ---------------------------------------------------------------------------


def test_r4h4_shared_dir_legacy_candidate_protected_by_registered_home(tmp_path, journal_backups):
    journal, backups = journal_backups
    data_home = tmp_path / "data_home"
    (data_home / "agents").mkdir(parents=True)
    (data_home / "agents" / "backend.md").write_text("role file", encoding="utf-8")
    # Register "agents" itself as a live account home — an edge case, but
    # the check must still hold: never sweep a file under a registered
    # config_dir's own top-level segment.
    (data_home / "user-profiles.json").write_text(
        json.dumps([{"config_dir": str(data_home / "agents")}]), encoding="utf-8"
    )

    step = ArchiveV1LegacyStep(journal=journal, backups=backups, data_home=data_home)
    candidates = step._shared_dir_legacy_candidates()
    assert candidates == []
