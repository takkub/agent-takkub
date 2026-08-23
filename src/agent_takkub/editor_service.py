"""Editor read/save service — pure Python, no Qt (#365 phase 3).

`editor_widget.py` (frontend-owned, off-limits for this task) already reads
file text for display in phase 2. This module adds the missing piece for
Ctrl+S: a versioned snapshot (`EditorFileState`, mirrors
`schemas/editor_file_state.schema.json`) and a conflict-checked atomic write.

Conflict rule (04_MONACO_EDITOR_SPEC.md): track mtime_ns + size + SHA-256.
If the disk version moved since the caller's `expected` snapshot, never
overwrite — return a `Conflict` instead so the caller can show
`[Compare] [Reload disk] [Keep mine / overwrite]`.

Write path: same-directory temp file + `os.replace`, long-path-safe on
Windows via `worktree_manager._win_long_path` (same helper
`skill_scan.py`/`user_profile.py` already import cross-module) plus a short
bounded retry on `PermissionError` — the same shape `config.py`'s
`_write_json_atomic` uses for the same reason (a transient AV/indexer lock).

Every path here goes through `project_file_index.resolve_and_contain` — the
same containment gate the Explorer and `editor_widget.py` use.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .project_file_index import PathEscapesRootsError, resolve_and_contain
from .worktree_manager import _win_long_path

logger = logging.getLogger(__name__)

# Matches editor_widget.MAX_EDITOR_FILE_BYTES — kept as its own constant
# rather than an import so this module stays free of editor_widget.py's Qt
# WebEngine dependency chain (it must stay "no Qt", see module docstring).
MAX_EDIT_FILE_BYTES = 2_000_000
_BINARY_SNIFF_BYTES = 8192
_UTF8_BOM = b"\xef\xbb\xbf"
_REPLACE_RETRY_DELAYS_S = (0.0, 0.02, 0.05, 0.1)


def _log_event(event: str, **details) -> None:
    """Proxy to orchestrator._log_event (lazy import to dodge a cycle — same
    pattern as pipeline_executor.py/lead_context.py's own proxies)."""
    _orch = sys.modules.get("agent_takkub.orchestrator")
    if _orch is not None:
        _orch._log_event(event, **details)


@dataclass(frozen=True)
class EditorFileState:
    path: Path
    mtime_ns: int
    size: int
    sha256: str | None  # None for binary/too-large files (never hashed)
    language: str | None
    binary: bool
    too_large: bool
    newline: str | None  # "\n" | "\r\n" | "\r" | None (empty file / binary)
    bom: bool


class SaveTooLargeError(ValueError):
    """The text to save exceeds `max_bytes` — same shape as
    `PathEscapesRootsError`: raised internally, caught by `save_atomic`, and
    surfaced as a `SaveResult(error=...)` rather than propagated."""


@dataclass(frozen=True)
class Conflict:
    disk: EditorFileState | None  # None when the file was deleted since `expected` was read
    ours: EditorFileState | None  # the caller's `expected` snapshot, echoed back for a [Compare] UI


@dataclass(frozen=True)
class SaveResult:
    ok: bool
    state: EditorFileState | None  # new state after a successful write
    conflict: Conflict | None
    error: str | None


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def _detect_newline(text: str) -> str | None:
    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    if "\r" in text:
        return "\r"
    return None


def stat_snapshot(resolved: Path, max_bytes: int = MAX_EDIT_FILE_BYTES) -> EditorFileState:
    """Build an `EditorFileState` for an already-contained, existing path.
    No containment check and no event log — the two callers (`read_for_edit`
    below, and `file_watch_service.py`'s disk-change poller) each have their
    own reason to skip one or the other."""
    st = resolved.stat()
    size = st.st_size
    with resolved.open("rb") as fh:
        sample = fh.read(_BINARY_SNIFF_BYTES)
    binary = _looks_binary(sample)
    too_large = size > max_bytes
    sha256 = None
    newline = None
    bom = False
    if not binary and not too_large:
        raw = resolved.read_bytes()
        bom = raw.startswith(_UTF8_BOM)
        text = (
            raw[len(_UTF8_BOM) :].decode("utf-8", errors="replace")
            if bom
            else raw.decode("utf-8", errors="replace")
        )
        newline = _detect_newline(text)
        sha256 = hashlib.sha256(raw).hexdigest()
    return EditorFileState(
        path=resolved,
        mtime_ns=st.st_mtime_ns,
        size=size,
        sha256=sha256,
        language=resolved.suffix.lstrip(".").lower() or None,
        binary=binary,
        too_large=too_large,
        newline=newline,
        bom=bom,
    )


def read_for_edit(
    path: Path, roots: Sequence[Path], max_bytes: int = MAX_EDIT_FILE_BYTES
) -> EditorFileState:
    """Containment-checked version snapshot for opening `path` in the
    editor. Raises `PathEscapesRootsError`/`OSError` on failure — callers
    dispatch this from a worker thread, same convention as
    `editor_widget.read_file_for_editor`."""
    resolved = resolve_and_contain(Path(path), roots)
    state = stat_snapshot(resolved, max_bytes)
    _log_event("workspace.file.opened", path=str(resolved))
    return state


def _has_conflict(current: EditorFileState, expected: EditorFileState) -> bool:
    if current.mtime_ns != expected.mtime_ns or current.size != expected.size:
        return True
    if expected.sha256 is not None and current.sha256 is not None:
        return current.sha256 != expected.sha256
    return False


def _encode_for_write(text: str, expected: EditorFileState | None) -> bytes:
    """Preserve the original file's BOM/newline convention (recorded in
    `expected` by `read_for_edit`) rather than whatever line endings the
    caller's `text` happens to use. A brand-new file (`expected is None`)
    is written exactly as given — utf-8, no BOM, caller's own newlines."""
    if expected is not None and expected.newline == "\r\n" and "\r\n" not in text:
        text = text.replace("\n", "\r\n")
    data = text.encode("utf-8")
    if expected is not None and expected.bom:
        data = _UTF8_BOM + data
    return data


def _write_atomic_text(resolved: Path, data: bytes) -> None:
    """Same-directory temp file + `os.replace`. `_win_long_path` keeps both
    sides of the replace valid past Windows' MAX_PATH (worktree_manager's
    own convention); the bounded retry absorbs a transient AV/indexer lock
    on the destination the way `config.py`'s atomic writer does."""
    tmp = resolved.with_name(f".{resolved.name}.takkub-tmp-{os.getpid()}-{time.time_ns()}")
    tmp_str = _win_long_path(tmp) if sys.platform == "win32" else str(tmp)
    dst_str = _win_long_path(resolved) if sys.platform == "win32" else str(resolved)
    with open(tmp_str, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    last_exc: OSError | None = None
    for delay in _REPLACE_RETRY_DELAYS_S:
        if delay:
            time.sleep(delay)
        try:
            os.replace(tmp_str, dst_str)
            return
        except PermissionError as exc:
            last_exc = exc
            continue
    try:
        os.unlink(tmp_str)
    except OSError:
        pass
    raise last_exc or OSError(f"could not replace {resolved}")


def save_atomic(
    path: Path,
    text: str,
    expected: EditorFileState | None,
    roots: Sequence[Path],
    max_bytes: int = MAX_EDIT_FILE_BYTES,
) -> SaveResult:
    """Conflict-checked atomic write.

    `expected=None` means "no prior read" (a brand-new file) — that's a
    conflict only if something already exists on disk. Otherwise a missing
    disk file (deleted since load) or a mismatched mtime_ns/size/sha256 is
    a conflict too. Never writes on a conflict.

    Also rejects (via `SaveResult.error`, never writing) when `text` encodes
    to more than `max_bytes` — the same cap `stat_snapshot` uses on the read
    side, checked here on the encoded UTF-8 length so a save can never
    produce a file `read_for_edit` would come back flagging `too_large`.
    """
    try:
        resolved = resolve_and_contain(Path(path), roots)
    except PathEscapesRootsError as exc:
        return SaveResult(ok=False, state=None, conflict=None, error=str(exc))

    current = stat_snapshot(resolved, max_bytes) if resolved.exists() else None

    if expected is None:
        conflict = current is not None
    elif current is None:
        conflict = True
    else:
        conflict = _has_conflict(current, expected)

    if conflict:
        _log_event("workspace.file.conflict", path=str(resolved))
        return SaveResult(
            ok=False, state=None, conflict=Conflict(disk=current, ours=expected), error=None
        )

    try:
        encoded_len = len(text.encode("utf-8"))
        if encoded_len > max_bytes:
            raise SaveTooLargeError(f"save exceeds max size: {encoded_len} > {max_bytes} bytes")
    except SaveTooLargeError as exc:
        return SaveResult(ok=False, state=None, conflict=None, error=str(exc))

    try:
        _write_atomic_text(resolved, _encode_for_write(text, expected))
    except OSError as exc:
        logger.debug("editor_service: save failed for %s: %s", resolved, exc)
        return SaveResult(ok=False, state=None, conflict=None, error=str(exc))

    new_state = stat_snapshot(resolved, max_bytes)
    _log_event("workspace.file.saved", path=str(resolved))
    return SaveResult(ok=True, state=new_state, conflict=None, error=None)
