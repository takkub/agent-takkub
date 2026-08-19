"""Ingest adapter for the per-role session decision notes
`orchestrator._save_decision_note` writes under
`runtime/sessions/<date>/<project>/<role>-<time>.md`, rendered by
`vault_mirror._render_decision_note` (YAML frontmatter + a `## Note` body).
Parses that fixed shape only — does not touch the writer.
"""

from __future__ import annotations

import re
from datetime import datetime

from agent_takkub.core.models.memory import CandidateConfidence, MemoryKind, Scope, Trust

from ..candidate import MemoryCandidate

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_NOTE_RE = re.compile(r"^## Note\n\n(.*?)(?:\n## |\Z)", re.DOTALL | re.MULTILINE)


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _parse_epoch(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def from_decision_note_text(text: str) -> MemoryCandidate | None:
    fields = _parse_frontmatter(text)
    note_m = _NOTE_RE.search(text)
    if note_m is None:
        return None
    content = note_m.group(1).strip()
    if not content:
        return None
    kwargs = dict(
        kind=MemoryKind.PROJECT,
        content=content,
        scope=Scope.PROJECT,
        trust=Trust.AGENT_REPORTED,
        confidence=CandidateConfidence.INFERRED,
        source="decision_note",
        project_id=fields.get("project"),
        agent_id=fields.get("role"),
    )
    date = fields.get("date")
    if date:
        epoch = _parse_epoch(date)
        if epoch is not None:
            kwargs["created_at"] = epoch
    return MemoryCandidate(**kwargs)
