"""Default-deny indexing boundary for the Obsidian vault — issue #365
phase 8 improvements 3-5, `09_KNOWLEDGE_BOUNDARIES.md` ("Obsidian owns
curated durable human-readable knowledge... OpenViking optionally
indexes/retrieves knowledge/resources; it is not a second uncontrolled
operational-memory owner") + `10_OBSIDIAN_HARDENING.md` items 4/5.

Two audiences:
- doctor's own boundary report (`check_obsidian`), so a human can see the
  policy without reading source;
- a future OpenViking adapter (#365 phase 9, not built yet) — MUST filter
  every path it would index through `is_indexable()` before touching it.

No network, no filesystem I/O, no config import — a pure allow/deny
decision over a path string, safe to embed in anything.
"""

from __future__ import annotations

import pathlib

# Curated, durable knowledge — the only trees safe to index/retrieve.
ALLOWLIST_PREFIXES: tuple[str, ...] = ("01-Projects", "02-Areas")

# Session/operational/internal — never indexed, regardless of allowlist,
# and regardless of whether *rel_path* is vault-relative (99-Logs/.obsidian
# live under the vault root) or DATA_HOME-relative (runtime/secrets live
# there) — a future adapter may see either kind of path.
DENYLIST_PREFIXES: tuple[str, ...] = (
    "99-Logs",
    ".obsidian",
    "runtime",
    "secrets",
)

# Filename suffixes marking a raw transcript/log dump, denied even inside
# an allowlisted tree (e.g. one copied by hand into 01-Projects).
_TRANSCRIPT_SUFFIXES: tuple[str, ...] = (".transcript", ".pty", ".raw", ".log")


def _parts(rel_path: str) -> tuple[str, ...]:
    return pathlib.PurePosixPath((rel_path or "").replace("\\", "/")).parts


def is_indexable(rel_path: str) -> bool:
    """``True`` iff *rel_path* is allowed to be indexed/retrieved.

    Default-deny: only a path under an ``ALLOWLIST_PREFIXES`` top segment,
    not also matching a deny rule, returns ``True``. Every dotfile/dotdir
    segment (``.obsidian``, but also any other hidden path) is denied on
    top of the explicit denylist, and a raw-transcript filename suffix is
    denied even inside an allowlisted tree.
    """
    parts = _parts(rel_path)
    if not parts:
        return False
    if parts[0] in DENYLIST_PREFIXES:
        return False
    if any(p.startswith(".") for p in parts):
        return False
    if parts[-1].lower().endswith(_TRANSCRIPT_SUFFIXES):
        return False
    return parts[0] in ALLOWLIST_PREFIXES
