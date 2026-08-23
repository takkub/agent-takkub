"""Append-only JSONL store: atomic write (read-modify-`os.replace`, never a
partial line visible to a concurrent reader) + corruption-tolerant read
(bad lines skipped and counted, never raised).

ponytail: the default mode rewrites the whole file per call (O(n)),
acceptable while nothing writes through this at high frequency. Phase 6's
conversation-transcript writer is the first high-frequency caller, so it
opts into `true_append=True` — plain `open(path, "ab")` + one `write()`.
POSIX guarantees that single write is atomic up to `PIPE_BUF`; Windows'
CRT append mode reseeks to EOF before each write rather than holding a
`FILE_APPEND_DATA`-only handle, so two *processes* racing a write on
Windows could still interleave — acceptable here because every true-append
writer in this codebase runs single-process (the Qt cockpit), same ceiling
already documented on `ConversationStore`. A genuine multi-process need
would require a `FILE_APPEND_DATA` handle via `ctypes`/`pywin32` — not
built since nothing needs it yet.
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


class OffsetReadResult(NamedTuple):
    records: list[dict[str, Any]]
    corrupt_lines: int
    next_offset: int


class JsonlStore:
    def __init__(self, path: Path, *, true_append: bool = False) -> None:
        self._path = path
        self._true_append = true_append

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(record), ensure_ascii=False, sort_keys=True)
        if self._true_append:
            with open(self._path, "ab") as f:
                f.write(line.encode("utf-8"))
                f.write(b"\n")
            return
        existing = self._path.read_bytes() if self._path.exists() else b""
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

    def read_from(self, offset: int) -> OffsetReadResult:
        """Records appended after byte `offset` — the idempotent-ingest
        primitive: a caller persists `next_offset` and passes it back in to
        resume exactly where it left off, never re-parsing the whole file
        and never double-counting a line already seen. A `offset` at or
        past the current size (nothing new since last read, or file was
        rotated/truncated) returns no records and clamps `next_offset` to
        the file's actual size rather than the stale input.
        """
        if not self._path.exists():
            return OffsetReadResult([], 0, 0)
        size = self._path.stat().st_size
        if offset >= size:
            return OffsetReadResult([], 0, size)
        with open(self._path, "rb") as f:
            f.seek(max(0, offset))
            raw = f.read()
        records: list[dict[str, Any]] = []
        corrupt = 0
        for raw_line in raw.decode("utf-8", errors="replace").splitlines():
            if not raw_line.strip():
                continue
            try:
                records.append(json.loads(raw_line))
            except json.JSONDecodeError:
                corrupt += 1
        return OffsetReadResult(records, corrupt, size)
