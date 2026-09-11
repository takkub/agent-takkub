"""Write-ahead ledger for #504's promote/archive transfers (round 4
crosscheck, `docs/audit/2026-09-10-504-gemini-crosscheck.md` (B)(2)/(4)).

`promote_v1.py`'s copy+prune orchestration names every file it is about to
touch, durably, as `PENDING` BEFORE its first byte is copied — not after
the fact (`write_json_atomic` alone, used everywhere else in this package,
only guarantees the write lands whole; it never forces it past the OS page
cache, so a WAL "committed" with it wasn't actually durable when a hard
crash — not just an unhandled exception — was the thing being guarded
against). Each entry then only ever advances forward through
`PENDING -> COPIED -> VERIFIED -> SOURCE_PRUNED`, one durable write per
transition, so a resumed boot can tell exactly how far a crashed attempt
got without guessing from a (possibly since-changed) directory scan.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .registry_copy_step import write_json_atomic

STATE_PENDING = "PENDING"
STATE_COPIED = "COPIED"
STATE_VERIFIED = "VERIFIED"
STATE_SOURCE_PRUNED = "PRUNED"
# #504 round4 R4-H3: an entry whose source could not be reconstructed
# after a failed removal — its only safe copy is `dest`; it stays
# recorded (never silently dropped from the final manifest) so an
# operator can see it needs attention instead of it just vanishing.
STATE_PRUNE_FAILED = "PRUNE_FAILED"


def write_json_durable(path: Path, payload: dict) -> None:
    """`registry_copy_step.write_json_atomic` already fsyncs the temp
    file's content before its atomic rename, and best-effort fsyncs the
    parent directory afterward (#504 round4 T2) — this is just that
    function, kept under the name every ledger/manifest/snapshot call
    site in this package was written against."""
    write_json_atomic(path, payload)


@dataclass
class TransferLedger:
    """One step's own WAL file — entries keyed by `TransferEntry.name`,
    each a dict carrying enough (`kind`/`src`/`dest`/`paths`/`state`/
    `sha256`) to reconstruct a `TransferEntry` and resume exactly where a
    crashed attempt left off, without rescanning `src` (a rescan after a
    partial removal would see a SMALLER file list than what was actually
    promised — #504 round4 T6). `meta` carries whatever else a caller
    needs to resume the surrounding transaction unambiguously
    (`ArchiveV1LegacyStep` stores the chosen `archive_root` there, so a
    resume reuses the SAME generation directory a crashed attempt already
    started writing into, rather than picking a fresh timestamp).

    `list_key` names the top-level JSON key the entries list is written
    under — `"entries"` by default, but a step whose OWN final manifest
    uses a different name for its committed-ownership list (`"promoted"`,
    `"archived"`) can set this to match, so every ledger write this class
    makes has the exact same shape as that step's final, fully-committed
    manifest (#504 round4 wal_contract: a durable ledger write is a
    durable ledger write regardless of which stage of the transaction
    produced it — a caller inspecting `write_json_atomic`'s own payloads
    for that step's known list key must find entries carrying a real
    `state`/`sha256` in every one, not just the last one)."""

    path: Path
    list_key: str = "entries"
    # The actual JSON-durable-write function this ledger calls — defaults
    # to this module's own `write_json_atomic` import, but a caller that
    # itself re-exports `write_json_atomic` under its OWN module namespace
    # (as `promote_v1.py` does) should pass THAT reference here instead,
    # so patching the caller's own binding (as the #504 round4 fault
    # harness does: `patch.object(promote_v1, 'write_json_atomic', ...)`)
    # actually intercepts every ledger write, not just calls made directly
    # through this module's separate binding of the same function.
    write_fn: Callable[[Path, dict], None] = write_json_atomic

    def exists(self) -> bool:
        return self.path.is_file()

    def _read_raw(self) -> dict:
        if not self.path.is_file():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def read(self) -> dict[str, dict]:
        data = self._read_raw()
        return {e["name"]: e for e in data.get(self.list_key, [])} if data else {}

    def read_meta(self) -> dict:
        return dict(self._read_raw().get("meta", {}))

    def write(self, entries: dict[str, dict], meta: dict | None = None) -> None:
        if meta is None:
            meta = self.read_meta() if self.path.is_file() else {}
        self.write_fn(
            self.path,
            {
                "schema": 1,
                "updated_at": time.time(),
                "meta": meta,
                self.list_key: list(entries.values()),
            },
        )

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
            if self.path.parent.is_dir() and not any(self.path.parent.iterdir()):
                self.path.parent.rmdir()
        except OSError as e:
            # A leftover-but-cleared WAL file (or its now-empty parent
            # directory) is inert — the step's own `_pending()` recomputes
            # from live disk state every time, and every entry this ledger
            # could still name is already `SOURCE_PRUNED` by the time
            # `clear()` is called, so a stray file/dir here is never read
            # back as a resumable attempt. Logged (never a bare swallow)
            # since this did attempt a mutation.
            _log_event("migration_wal_clear_failed", path=str(self.path), error=str(e))


def _log_event(event: str, **details: object) -> None:
    """Best-effort structured log — mirrors `promote_v1._log_event`
    (kept as a separate, tiny copy here rather than imported, to avoid a
    circular import: `promote_v1` imports FROM this module)."""
    try:
        from ...orchestrator_text import _log_event as _emit

        _emit(event, **details)
    except Exception:
        return  # swallow-ok: this IS the fallback logging path itself — no
        # further sink to report its own failure to, and it never
        # mutates anything.
