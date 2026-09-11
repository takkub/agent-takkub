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
from dataclasses import dataclass, field
from pathlib import Path


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


def _source_files(src: Path) -> list[Path]:
    if src.is_file():
        return [src]
    return sorted(p for p in src.rglob("*") if p.is_file())


def copy_only(src: Path, dest: Path) -> list[str]:
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
    `state/`, ...)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    duplicated: list[str] = []
    if src.is_dir():
        for f in _source_files(src):
            rel = f.relative_to(src)
            target = dest / rel
            if target.is_file() and _sha256(target) != _sha256(f):
                aside = target.with_name(f"{target.name}.duplicate-{time.time():.6f}")
                target.rename(aside)
                duplicated.append(rel.as_posix())
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dest)
    return duplicated


def verify_only(src: Path, dest: Path) -> CopyVerification:
    """Just the checksum half of `copy_verified` — assumes `copy_only(src,
    dest)` already ran. Raises `VerifyMismatchError` (never returns) on any
    mismatch or missing file."""
    total_bytes = 0
    digests: dict[str, str] = {}
    source_files = _source_files(src)
    for f in source_files:
        target = (dest / f.relative_to(src)) if src.is_dir() else dest
        if not target.is_file():
            raise VerifyMismatchError(f"missing after copy: {target}")
        digest = _sha256(target)
        if digest != _sha256(f):
            raise VerifyMismatchError(f"checksum mismatch: {target}")
        total_bytes += target.stat().st_size
        rel = f.relative_to(src).as_posix() if src.is_dir() else src.name
        digests[rel] = digest
    return CopyVerification(file_count=len(source_files), total_bytes=total_bytes, digests=digests)


def copy_verified(src: Path, dest: Path) -> CopyVerification:
    """Copy *src* (file or directory) into *dest*, then verify every file
    *src* contains landed at *dest* with a matching sha256 — raises
    `VerifyMismatchError` (never returns) on any mismatch or missing file.
    Never touches *src* — the caller removes the original only after this
    returns without raising, per the module's own copy-verify-then-remove
    contract."""
    duplicated = copy_only(src, dest)
    result = verify_only(src, dest)
    if duplicated:
        return CopyVerification(
            result.file_count, result.total_bytes, result.digests, tuple(duplicated)
        )
    return result
