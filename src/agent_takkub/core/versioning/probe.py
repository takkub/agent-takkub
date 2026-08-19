"""Live-store schema-drift probe (plan §4.2 lesson 1 — codex 0.147 changed
its rollout schema while `codex exec` kept writing the old one, so CI stayed
green because nothing in the suite ever read a REAL on-disk store). This
module reads whatever store each provider already has on THIS machine and
fingerprints its shape; it skips (never fails) when there's nothing to read.

Only providers with a confirmed, already-relied-upon store location get a
resolver here — the same known-vs-unknown line doctor.py already draws
(`codex_helper.py`/`gemini_helper.py`'s own module docstrings: "the cockpit
never touches those credentials"/store internals for the unconfirmed ones).
Drift is detected relative to what THIS machine last recorded via
`versioning.store` (see `detect_drift`), never against a hardcoded guess at
the "real" schema — the project's own #309 lesson is that a guessed schema
is exactly what let codex 0.147 slip through unnoticed.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from agent_takkub import codex_helper, config, opencode_helper

_SAMPLE_LIMIT = 5


@dataclass(frozen=True, slots=True)
class StoreProbeResult:
    provider: str
    found: bool
    fingerprint: frozenset[str] = field(default_factory=frozenset)
    sampled: int = 0
    note: str = ""


@dataclass(frozen=True, slots=True)
class DriftResult:
    provider: str
    drifted: bool
    added: frozenset[str] = field(default_factory=frozenset)
    removed: frozenset[str] = field(default_factory=frozenset)
    note: str = ""


def _sample_jsonl_keys(files: list[Path], limit: int = _SAMPLE_LIMIT) -> tuple[frozenset[str], int]:
    keys: set[str] = set()
    sampled = 0
    for path in files:
        if sampled >= limit:
            break
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        keys.update(record.keys())
                    sampled += 1
                    break  # one record per file is enough for a shape fingerprint
        except OSError:
            continue
    return frozenset(keys), sampled


def _probe_claude() -> StoreProbeResult:
    root = config.default_claude_config_dir() / "projects"
    if not root.is_dir():
        return StoreProbeResult("claude", False, note="no ~/.claude/projects directory")
    try:
        files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return StoreProbeResult("claude", False, note="could not list session files")
    if not files:
        return StoreProbeResult("claude", False, note="no session transcripts yet")
    fp, sampled = _sample_jsonl_keys(files)
    return StoreProbeResult("claude", True, fp, sampled)


def _probe_codex() -> StoreProbeResult:
    root = codex_helper.codex_sessions_root()
    if not root.is_dir():
        return StoreProbeResult("codex", False, note="no codex sessions directory")
    try:
        files = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return StoreProbeResult("codex", False, note="could not list rollout files")
    if not files:
        return StoreProbeResult("codex", False, note="no rollouts yet")
    fp, sampled = _sample_jsonl_keys(files)
    return StoreProbeResult("codex", True, fp, sampled)


def _probe_opencode() -> StoreProbeResult:
    db_path = opencode_helper.opencode_db_path()
    if db_path is None or not db_path.is_file():
        return StoreProbeResult("opencode", False, note="no opencode.db found")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = frozenset(row[0] for row in cur.fetchall())
        finally:
            conn.close()
    except sqlite3.Error as e:
        return StoreProbeResult("opencode", False, note=f"could not read opencode.db: {e}")
    return StoreProbeResult("opencode", True, tables, 1)


_RESOLVERS = {
    "claude": _probe_claude,
    "codex": _probe_codex,
    "opencode": _probe_opencode,
}


def probe_store(provider: str) -> StoreProbeResult:
    resolver = _RESOLVERS.get(provider)
    if resolver is None:
        return StoreProbeResult(
            provider, False, note="no confirmed store location for this provider"
        )
    try:
        return resolver()
    except Exception as e:
        return StoreProbeResult(provider, False, note=f"probe error: {type(e).__name__}: {e}")


def probe_all() -> dict[str, StoreProbeResult]:
    from agent_takkub.provider_spec import PROVIDER_REGISTRY

    return {name: probe_store(name) for name in PROVIDER_REGISTRY}


def detect_drift(
    current: StoreProbeResult, previous_fingerprint: frozenset[str] | None
) -> DriftResult:
    """Compare *current*'s fingerprint against a previously-recorded one
    (e.g. read back from `versioning.store` via a `"store_schema:<provider>"`
    component). `previous_fingerprint=None` means no baseline exists yet —
    that is reported as "not drifted" (there is nothing to have drifted
    from), not as a warning."""
    if not current.found:
        return DriftResult(current.provider, False, note=current.note)
    if previous_fingerprint is None:
        return DriftResult(
            current.provider,
            False,
            note="no prior fingerprint recorded — baseline not yet established",
        )
    added = current.fingerprint - previous_fingerprint
    removed = previous_fingerprint - current.fingerprint
    return DriftResult(current.provider, bool(added or removed), added, removed)
