"""Ingest adapter for `role_memory.py`'s per-(role × project) learned-notes
markdown — trust=AGENT_REPORTED (self-typed by the agent, never cockpit-
verified, `digest_facts.py`'s own measured-vs-reported split). WRAPs the
existing reader (`role_memory.ensure_role_memory`/`role_memory_path`) —
this module only adds the "turn the bullets into candidates" step that
reader has no public API for.
"""

from __future__ import annotations

import re

from agent_takkub.core.models.memory import CandidateConfidence, MemoryKind, Scope, Trust

from ..candidate import MemoryCandidate

_BULLET_RE = re.compile(r"^\s*[-*]\s+(\S.*)$")
# The one seed placeholder `_BASE_SECTIONS` in role_memory.py ships with
# actual bullet-shaped text — never a real learned fact, must not become a
# candidate.
_SEED_PLACEHOLDER = "(ว่าง — เติมเมื่อเรียนรู้)"


def parse_bullets(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        m = _BULLET_RE.match(line)
        if not m:
            continue
        bullet = m.group(1).strip()
        if bullet and bullet != _SEED_PLACEHOLDER:
            out.append(bullet)
    return out


def from_role_memory_text(text: str, *, project: str, role: str) -> list[MemoryCandidate]:
    return [
        MemoryCandidate(
            kind=MemoryKind.PROJECT,
            content=bullet,
            scope=Scope.AGENT,
            trust=Trust.AGENT_REPORTED,
            confidence=CandidateConfidence.INFERRED,
            source="role_memory",
            project_id=project,
            agent_id=role,
        )
        for bullet in parse_bullets(text)
    ]
