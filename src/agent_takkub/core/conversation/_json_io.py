"""Shared atomic-JSON-write helper for `store.py`/`checkpoint.py`/`summary.py`
— the storage layer's writer, without fsync (conversation files are
rebuildable; the hot done path should not pay a flush) and with sorted keys
so diffs between checkpoints stay stable."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_takkub.core.storage.json_io import write_json_atomic as _write


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    _write(path, data, durable=False, sort_keys=True)
