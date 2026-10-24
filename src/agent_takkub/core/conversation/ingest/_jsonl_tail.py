"""Incremental JSONL tail shared by every provider adapter's `read_new`.

The cursor is a byte offset that only ever advances past complete
newline-terminated records: a record the provider is still writing stays
unread until its newline lands (it used to be skipped forever), and bytes
appended after the size was sampled are left for the next call (they used
to be read now and again next time — duplicate messages).
"""

from __future__ import annotations

from pathlib import Path


def read_complete_lines(path: Path, cursor: str | None) -> tuple[list[str], str] | None:
    """``(lines, next_cursor)`` for the records after *cursor*, or ``None``
    when the file can't be stat'ed (caller keeps its cursor)."""
    offset = int(cursor) if cursor else 0
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if offset >= size:
        return [], str(size)
    with open(path, "rb") as f:
        f.seek(max(0, offset))
        raw = f.read(size - offset)
    end = raw.rfind(b"\n")
    if end < 0:
        return [], str(offset)
    lines = [
        line.strip()
        for line in raw[: end + 1].decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    return lines, str(offset + end + 1)
