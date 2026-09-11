"""Copy-verify helper for #504's V1-archive/V2-promote moves — every move in
that ladder step is "copy into the new spot, byte-verify every file landed
correctly (count + sha256), THEN remove the original", never a raw
``shutil.move`` (issue #504 "ความปลอดภัยตอนย้าย": a same-volume rename can't
be assumed when DATA_HOME's target may sit on a different drive/volume, so
this never takes the atomic-rename shortcut `shutil.move` would use when it
happens to be available).

Merges into an existing destination (``dirs_exist_ok=True``) rather than
replacing it wholesale — the promote half of #504 must land ``v2/providers/*``
inside a top-level ``providers/`` directory that may already hold a live
provider's credentials (kimi, #504 design note) without clobbering anything
*not* also present in the source.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# #574 round11 item 3: how often a large directory entry's copy/verify
# loop below fires its optional `on_file` progress callback — every N
# files OR every T seconds, whichever comes first (plus always on the
# very last file, so a caller always sees a final, complete count). A
# 30-minute pre-migrate-backup rehearsal on real prod data sat on the SAME
# entry for 17+ minutes with zero signal on screen — `on_entry` (the
# existing per-ENTRY callback) fires once per whole top-level item, which
# for a directory with tens of thousands of files is not fine-grained
# enough to prove the process hasn't hung.
_PROGRESS_EVERY_N_FILES = 200
_PROGRESS_EVERY_N_SECONDS = 2.0


class _FileProgressThrottle:
    """Wraps an optional `on_file(done, total, current_path)` callback so
    every caller in this module gets the identical every-N-files-or-every-
    T-seconds throttle, plus a GUARANTEED final call at `done == total`
    (never left mid-count if the last file happened to land between
    throttle windows)."""

    def __init__(self, on_file: Callable[[int, int, str], None] | None, total: int) -> None:
        self._on_file = on_file
        self._total = total
        self._last_done = 0
        self._last_t = time.monotonic()

    def maybe_emit(self, done: int, current_path: str) -> None:
        if self._on_file is None:
            return
        now = time.monotonic()
        due = (
            done - self._last_done >= _PROGRESS_EVERY_N_FILES
            or now - self._last_t >= _PROGRESS_EVERY_N_SECONDS
            or done >= self._total
        )
        if not due:
            return
        self._last_done, self._last_t = done, now
        try:
            self._on_file(done, self._total, current_path)
        except Exception:
            return  # swallow-ok: pure progress notification, never a phase input.


class VerifyMismatchError(RuntimeError):
    """A file copied during a #504 archive/promote move didn't come out the
    other side byte-identical (or didn't land at all) — the caller must
    treat this exactly like an ``OSError``: undo whatever this move already
    did and never remove the original."""


@dataclass(frozen=True, slots=True)
class CopyVerification:
    file_count: int
    total_bytes: int
    # #504 H9: relative-posix-path -> sha256, for every file this call just
    # verified — callers persist this into their own manifests so a LATER
    # `validate()` can recompute and compare against real on-disk archive
    # content instead of only checking "the source is gone" (#504 acceptance
    # review finding H9: a green migration validate previously proved
    # nothing about target integrity).
    digests: dict[str, str] = field(default_factory=dict)
    # #574 round6 R6-M2: relative-posix paths of any pre-existing, foreign
    # (not part of this transaction's own source) file `copy_only` found
    # colliding with a same-named target it needed to write — moved aside
    # as a `.duplicate-<ts>` sibling rather than left blocking every retry
    # forever. Empty on the overwhelmingly common no-collision path.
    duplicates: tuple[str, ...] = ()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file_with_hash(src: Path, dest: Path) -> str:
    """Copy *src* to *dest* one read pass, hashing *src*'s bytes as they
    stream through — #574 round11: the one caller that actually needs this
    (`copy_only`'s directory branch) used to copy via `shutil.copytree`
    (no hash) and then `verify_only` re-opened and re-read every SOURCE
    file a second time just to compute the digest `copy_verified` compares
    against the target's own hash. A py-spy profile of a stalled
    pre-migrate-backup rehearsal (real prod data, ~200k files) showed 85%
    of wall time inside this module's own re-hashing — half of it this
    exact redundant second source read. Returns *src*'s sha256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with src.open("rb") as fsrc, dest.open("wb") as fdst:
        for chunk in iter(lambda: fsrc.read(1024 * 1024), b""):
            digest.update(chunk)
            fdst.write(chunk)
    shutil.copystat(src, dest)
    return digest.hexdigest()


def _source_files(src: Path) -> list[Path]:
    if src.is_file():
        return [src]
    return sorted(p for p in src.rglob("*") if p.is_file())


def copy_only(
    src: Path,
    dest: Path,
    *,
    on_file: Callable[[int, int, str], None] | None = None,
    files: list[Path] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Just the physical copy half of `copy_verified` — no checksum pass.
    Split out so a WAL-aware caller (`promote_v1._copy_phase`) can record a
    durable `COPIED` checkpoint between the copy landing on disk and its
    checksum being verified, rather than treating "copied" and "verified"
    as one indivisible moment (#504 round4 T2/T3).

    Returns the relative-posix path of every pre-existing, foreign file it
    had to move aside (see below) — empty on the normal, no-collision path.

    #504 round4 R4-H5 / #574 round6 R6-M2: a directory merge
    (`dirs_exist_ok=True`) never silently OVERWRITES a file that ALREADY
    exists at *dest* with DIFFERENT content — that file was never part of
    *src*'s own tree before this copy started, so it belongs to whoever put
    it there (a live provider home sharing a parent directory with what's
    being promoted/archived, #504 design note). R4-H5 originally made this
    raise `VerifyMismatchError` and refuse outright; round 6 review found
    that a live writer re-creating the same colliding file between retries
    (nothing here ever removes it) then blocks every retry identically,
    forever. Instead: rename the foreign file aside to a `.duplicate-<ts>`
    sibling (never deleted — same "copy/keep, never lose" contract as
    everything else in this package) and let this transaction's own copy
    proceed into the now-clear spot; the caller reports every path this
    returns rather than the collision going unnoticed.

    #574 round9 (`never_touch_promote_collision`): a top-level name that is
    itself a home for genuinely LIVE, externally-owned content (`providers/`,
    #504 R3-B1's "a Kimi credential directory") must never even get this far
    — `promote_v1._copy_phase` refuses a collision under one of those names
    OUTRIGHT before ever calling this function, so this rename-aside rescue
    only ever runs for a name only migration itself writes (`models/`,
    `state/`, ...).

    Returns ``(duplicated, source_digests)`` — *source_digests* (#574
    round11) maps each copied file's dest-relative-posix path (its own
    basename for a `file`-kind *src*) to the sha256 `_copy_file_with_hash`
    computed from the ONE read pass it made of that file while writing it,
    so `verify_only` below never has to open *src* a second time.

    *on_file* (#574 round11 item 3): best-effort progress observer for a
    large directory entry — throttled to roughly every
    `_PROGRESS_EVERY_N_FILES` files or `_PROGRESS_EVERY_N_SECONDS`
    seconds (see `_FileProgressThrottle`), plus always once more on the
    final file. Never called for a `file`-kind *src* (one file, nothing
    to show progress within).

    *files* (#504/#574 R8-M3): the entry's own file list, when the caller
    already enumerated it (`copy_verified` below) — skips a second
    `_source_files(src)` walk. `None` (the default) re-enumerates, exactly
    the prior behavior for every other direct caller."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    duplicated: list[str] = []
    source_digests: dict[str, str] = {}
    if src.is_dir():
        files = files if files is not None else _source_files(src)
        throttle = _FileProgressThrottle(on_file, len(files))
        for i, f in enumerate(files, start=1):
            rel = f.relative_to(src)
            target = dest / rel
            if target.is_file() and _sha256(target) != _sha256(f):
                aside = target.with_name(f"{target.name}.duplicate-{time.time():.6f}")
                target.rename(aside)
                duplicated.append(rel.as_posix())
            source_digests[rel.as_posix()] = _copy_file_with_hash(f, target)
            throttle.maybe_emit(i, rel.as_posix())
    else:
        # Single-file entries are the rare, small case in this package (a
        # top-level registry file, never a directory tree) — left on plain
        # `shutil.copy2` rather than folded into `_copy_file_with_hash`;
        # `verify_only` below simply re-hashes the one file for this
        # branch, a cost not worth the extra code path for one file.
        shutil.copy2(src, dest)
    return duplicated, source_digests


def verify_only(
    src: Path,
    dest: Path,
    source_digests: dict[str, str] | None = None,
    *,
    on_file: Callable[[int, int, str], None] | None = None,
    files: list[Path] | None = None,
) -> CopyVerification:
    """Just the checksum half of `copy_verified` — assumes `copy_only(src,
    dest)` already ran. Raises `VerifyMismatchError` (never returns) on any
    mismatch or missing file.

    *source_digests* (#574 round11): the per-file sha256 map `copy_only`
    returns, computed while `src`'s bytes were streaming into `dest` — a
    file whose relative path is a key here is checked against THAT digest
    instead of `_sha256(f)` re-opening and re-reading `src`. Optional (and
    simply not consulted for a name it doesn't cover), so a caller with no
    such map — every pre-existing direct caller of this function — gets
    the exact prior behavior.

    *on_file* (#574 round11 item 3): same throttled progress observer as
    `copy_only`'s own — called with `src`-relative paths as this pass
    reads each TARGET file's content back.

    *files* (#504/#574 R8-M3): reuse `copy_only`'s own already-enumerated
    file list instead of walking *src* a second time — on a 5,000-file
    entry that second `_source_files(src)` walk alone cost ~0.9s with zero
    progress signal, landing right at the copy-to-verify pass boundary
    where `_FileProgressThrottle`'s own fresh-throttle silence (up to
    `_PROGRESS_EVERY_N_SECONDS`) already stacked on top of it — together, a
    gap past the throttle's own 2s ceiling. `None` (the default) re-
    enumerates, exactly the prior behavior for every other direct caller."""
    total_bytes = 0
    digests: dict[str, str] = {}
    source_files = files if files is not None else _source_files(src)
    throttle = _FileProgressThrottle(on_file, len(source_files))
    for i, f in enumerate(source_files, start=1):
        target = (dest / f.relative_to(src)) if src.is_dir() else dest
        if not target.is_file():
            raise VerifyMismatchError(f"missing after copy: {target}")
        digest = _sha256(target)
        rel = f.relative_to(src).as_posix() if src.is_dir() else src.name
        src_digest = (source_digests or {}).get(rel)
        if src_digest is None:
            src_digest = _sha256(f)
        if digest != src_digest:
            raise VerifyMismatchError(f"checksum mismatch: {target}")
        total_bytes += target.stat().st_size
        digests[rel] = digest
        throttle.maybe_emit(i, rel)
    return CopyVerification(file_count=len(source_files), total_bytes=total_bytes, digests=digests)


def copy_verified(
    src: Path, dest: Path, *, on_file: Callable[[int, int, str], None] | None = None
) -> CopyVerification:
    """Copy *src* (file or directory) into *dest*, then verify every file
    *src* contains landed at *dest* with a matching sha256 — raises
    `VerifyMismatchError` (never returns) on any mismatch or missing file.
    Never touches *src* — the caller removes the original only after this
    returns without raising, per the module's own copy-verify-then-remove
    contract.

    *on_file* (#574 round11 item 3): forwarded to both the copy and
    verify passes — a caller sees progress covering the whole entry, copy
    phase then verify phase, not just one half of it."""
    # #504/#574 R8-M3: enumerate ONCE, share the list with both passes —
    # see `verify_only`'s own docstring for why the second walk mattered.
    files = _source_files(src) if src.is_dir() else None
    duplicated, source_digests = copy_only(src, dest, on_file=on_file, files=files)
    if on_file is not None and files is not None:
        # An explicit, unthrottled check-in exactly at the copy-to-verify
        # boundary — never left to `verify_only`'s own fresh throttle to
        # eventually cover on its own timing, which is what let the gap
        # exceed 2s in the first place. #574 round14 R9-M1: `done` here
        # must be the COMPLETED count (every file the copy pass just
        # finished), not 0 — a caller forwarding this straight through to
        # a UI counter (`boot_flow.on_file_progress`) used to see it snap
        # back to zero once per entry, four times on a real 1,000-file
        # run. `len(files)` still proves the process hasn't stalled
        # (the whole point of this check-in) without lying about progress
        # having been lost.
        # Propagates like every other `on_file` call in this module (the
        # throttle at `_FileProgressThrottle.__call__` never swallows either):
        # a broken progress observer is a caller bug, not something to hide.
        on_file(len(files), len(files), "")
    result = verify_only(src, dest, source_digests, on_file=on_file, files=files)
    if duplicated:
        return CopyVerification(
            result.file_count, result.total_bytes, result.digests, tuple(duplicated)
        )
    return result
