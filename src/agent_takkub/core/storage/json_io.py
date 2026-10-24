"""Atomic single-JSON-object writer — the storage layer's own primitive.

It used to live in `core.migration.registry_copy_step`, so storage,
custom_roles, skill_policy, provider_config, pane_tools_policy and
auto_issue_capture all imported a *migration* module just to save a file
(layer inversion, 2026-10-24 review D). Migration code re-exports it from
there unchanged, so `patch.object(<migration module>, "write_json_atomic")`
test seams keep working.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agent_takkub.cached_read import invalidate

from .jsonl_store import unique_tmp


def write_json_atomic(
    path: Path, payload: dict[str, Any], *, durable: bool = True, sort_keys: bool = False
) -> None:
    """Write *payload* to *path* via a per-writer tmp file + `os.replace`
    (never a partial file observable at *path*).

    `durable=True` (default): fsync the tmp file's content before the rename
    and best-effort fsync the parent directory afterward — migration ledgers
    and manifests (#504 round4 T2) need the write to survive a hard crash.

    Always drops *path* from `cached_read` (#658 contract: every in-process
    writer invalidates) — readers going through `legacy_reader.read_json`'s
    TTL otherwise saw the pre-write parse on their very next read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = unique_tmp(path)
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=sort_keys), encoding="utf-8"
    )
    if durable:
        with open(tmp, "r+b") as f:
            os.fsync(f.fileno())
    os.replace(tmp, path)
    invalidate(path)
    if not durable:
        return
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        return  # swallow-ok: directory-entry fsync is an extra durability
        # margin on top of the file fsync above (which already made the
        # content itself durable) — unsupported on some platforms (e.g.
        # opening a directory this way on Windows).
