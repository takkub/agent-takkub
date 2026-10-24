"""Shared atomic-JSON-write helper for `store.py`/`checkpoint.py`/`summary.py`
— same read-modify-`os.replace` discipline as `JsonlStore`, just for a single
JSON object instead of an append-only line stream."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agent_takkub.core.storage.jsonl_store import unique_tmp


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = unique_tmp(path)
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(tmp, path)
