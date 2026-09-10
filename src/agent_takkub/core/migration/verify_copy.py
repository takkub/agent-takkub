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


def copy_only(src: Path, dest: Path) -> None:
    """Just the physical copy half of `copy_verified` — no checksum pass.
    Split out so a WAL-aware caller (`promote_v1._copy_phase`) can record a
    durable `COPIED` checkpoint between the copy landing on disk and its
    checksum being verified, rather than treating "copied" and "verified"
    as one indivisible moment (#504 round4 T2/T3).

    #504 round4 R4-H5: a directory merge (`dirs_exist_ok=True`) never
    silently overwrites a file that ALREADY exists at *dest* with
    DIFFERENT content — that file was never part of *src*'s own tree
    before this copy started, so it belongs to whoever put it there (a
    live provider home sharing a parent directory with what's being
    promoted/archived, #504 design note). Raises `VerifyMismatchError`
    BEFORE touching anything if such a collision is found, so the
    caller's own backup-restore undo (already taken before this call,
    #504 B3) puts the untouched live file straight back with nothing
    lost — never a race where the collision is detected only after
    `shutil.copytree` has already clobbered it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        for f in _source_files(src):
            target = dest / f.relative_to(src)
            if target.is_file() and _sha256(target) != _sha256(f):
                raise VerifyMismatchError(
                    f"collision: {target} already exists with different content — "
                    "never part of this transaction's own source, refusing to overwrite it"
                )
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dest)


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
    copy_only(src, dest)
    return verify_only(src, dest)
