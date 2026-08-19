"""Per-source ingest cursor — one JSON file, keyed `"<provider>::<source_id>"`
-> cursor string. Deliberately NOT per-conversation: the same provider
transcript file must never be re-ingested from scratch just because it got
bound to a second conversation id, and a flat key keeps a corrupt/missing
file fail-open to "ingest from the start" rather than raising.
"""

from __future__ import annotations

from pathlib import Path

from agent_takkub.core.storage.legacy_reader import read_json
from agent_takkub.core.storage.paths import core_home

from .._json_io import write_json_atomic


def _cursor_path() -> Path:
    return core_home() / "conversation_ingest_cursors.json"


def _key(provider_id: str, source_id: str) -> str:
    return f"{provider_id}::{source_id}"


def get_cursor(provider_id: str, source_id: str, *, path: Path | None = None) -> str | None:
    data = read_json(path or _cursor_path())
    return data.get(_key(provider_id, source_id))


def set_cursor(provider_id: str, source_id: str, cursor: str, *, path: Path | None = None) -> None:
    p = path or _cursor_path()
    data = read_json(p)
    data[_key(provider_id, source_id)] = cursor
    write_json_atomic(p, data)
