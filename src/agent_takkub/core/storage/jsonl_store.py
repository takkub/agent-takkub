"""Append-only JSONL store: atomic write (read-modify-`os.replace`, never a
partial line visible to a concurrent reader) + corruption-tolerant read
(bad lines skipped and counted, never raised).

ponytail: append() rewrites the whole file per call (O(n)), acceptable for
Phase 1 where nothing production writes through this yet. If a real
high-frequency writer lands in a later phase, upgrade to a true append
(`open(path, "ab")`, single `write()` — POSIX guarantees atomicity up to
`PIPE_BUF`; Windows needs `FILE_APPEND_DATA`-only opens for the same
guarantee) instead of rewriting.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple


class ReadResult(NamedTuple):
    records: list[dict[str, Any]]
    corrupt_lines: int


class JsonlStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, record: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        existing = self._path.read_bytes() if self._path.exists() else b""
        line = json.dumps(dict(record), ensure_ascii=False, sort_keys=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(existing)
            f.write(line.encode("utf-8"))
            f.write(b"\n")
        os.replace(tmp, self._path)

    def read_all(self) -> ReadResult:
        if not self._path.exists():
            return ReadResult([], 0)
        records: list[dict[str, Any]] = []
        corrupt = 0
        for raw_line in self._path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                records.append(json.loads(raw_line))
            except json.JSONDecodeError:
                corrupt += 1
        return ReadResult(records, corrupt)
