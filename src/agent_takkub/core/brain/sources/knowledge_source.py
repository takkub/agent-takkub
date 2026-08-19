"""Ingest adapter for `vault_mirror.distill_to_knowledge_base`'s
`runtime/knowledge/<project>.md` — trust=AGENT_REPORTED still (the source
text is an agent's own `takkub done` note), but higher confidence than a raw
role-memory bullet: `distill_to_knowledge_base` already filtered for
`_is_durable_fact` before writing the entry, so what lands here has passed
one curation pass already.
"""

from __future__ import annotations

import re
from datetime import datetime

from agent_takkub.core.models.memory import CandidateConfidence, MemoryKind, Scope, Trust

from ..candidate import MemoryCandidate

# Mirrors vault_mirror.py's `_KB_ENTRY_PREFIX` + entry shape:
# "- `<iso>` **<role>** — <text>"
_ENTRY_RE = re.compile(r"^- `([^`]+)` \*\*([^*]+)\*\* — (.+)$")


def _parse_epoch(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def from_knowledge_base_text(text: str, *, project: str) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    for line in text.splitlines():
        m = _ENTRY_RE.match(line.strip())
        if not m:
            continue
        iso, role, content = m.group(1), m.group(2).strip(), m.group(3).strip()
        if not content:
            continue
        kwargs = dict(
            kind=MemoryKind.PROJECT,
            content=content,
            scope=Scope.PROJECT,
            trust=Trust.AGENT_REPORTED,
            confidence=CandidateConfidence.HIGH,
            source="knowledge_base",
            project_id=project,
            agent_id=role,
        )
        epoch = _parse_epoch(iso)
        if epoch is not None:
            kwargs["created_at"] = epoch
        candidates.append(MemoryCandidate(**kwargs))
    return candidates
