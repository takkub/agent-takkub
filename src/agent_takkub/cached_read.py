"""Stat-validated read cache for small config/state files that the Qt main
thread re-reads on a timer (#386).

Why this exists: the cockpit polls a handful of tiny JSON/JSONL files every
few seconds from the main thread — the task ledger (`project_nav`'s pending
list, 6s), `role-models` / `role-providers` mirrors (`effective_provider_for`
on every idle-check tick and status-header refresh), the projects registry
(`config.active_project`), `role_messages` (`_reap_role_messages`). Each read
is 3-4 syscalls (`exists` + `open` + `read` + `close`) plus a JSON parse. On a
healthy disk that is microseconds; on this machine's real stall traces
(2026-08-25, `boot.log` watchdog dumps over 06:32-06:45) the SAME reads sat
in `pathlib.open` / `read_text` for 1-10s each while the disk was wedged
(AV/OneDrive/WSL page-cache pressure — nothing the cockpit controls) and
stacked into 45 `main_thread_stall` events in 6h.

The files change rarely, so the fix is to not re-read them when they have
not changed: one `os.stat` per poll, and the parsed value is reused while
the ``(mtime_ns, size, inode)`` signature is unchanged. The stat is still a
syscall — but one instead of four, and it is the cheapest of them (no file
handle, no data transfer, no parse).

Freshness guard: a file whose mtime is within ``_RECENT_WRITE_S`` of "now"
is always re-read, so a rewrite that lands inside the filesystem's mtime
resolution (1s on some FS) with the same byte length can never serve a
stale parse. Everything else is exact — if the signature moved, we re-read.

Stdlib-only leaf: imported by ``core.storage.legacy_reader`` (Core V2 is the
bottom layer and must stay PyQt/engine-free) as well as by ``task_ledger``
and ``role_messages``.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Files modified this recently are never served from cache (see module doc).
_RECENT_WRITE_S = 2.0

# Bounded so a long-running cockpit that touches many per-project files can't
# grow this without limit. Way above the real working set (a few dozen).
_MAX_ENTRIES = 512

_lock = threading.Lock()
# path -> (signature, parsed value)
_cache: dict[str, tuple[tuple[int, int, int], Any]] = {}


def read_cached(
    path: Path | str,
    parse: Callable[[str], Any],
    *,
    missing: Any = None,
    encoding: str = "utf-8",
) -> Any:
    """Return ``parse(text_of(path))``, reusing the last parsed value while
    the file's stat signature is unchanged.

    *missing* is returned (and nothing cached) when the file does not exist.
    ``OSError`` from the read and any exception from *parse* propagate — the
    callers already have their own fail-open handling and expectations, this
    helper only removes the redundant I/O.
    """
    key = os.fspath(path)
    try:
        st = os.stat(key)
    except FileNotFoundError:
        with _lock:
            _cache.pop(key, None)
        return missing
    sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    recent = (time.time() - st.st_mtime) < _RECENT_WRITE_S
    if not recent:
        with _lock:
            hit = _cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
    with open(key, encoding=encoding) as fh:
        text = fh.read()
    value = parse(text)
    with _lock:
        if len(_cache) >= _MAX_ENTRIES:
            _cache.clear()
        _cache[key] = (sig, value)
    return value


def invalidate(path: Path | str | None = None) -> None:
    """Drop one path's entry (or everything when *path* is ``None``)."""
    with _lock:
        if path is None:
            _cache.clear()
        else:
            _cache.pop(os.fspath(path), None)
