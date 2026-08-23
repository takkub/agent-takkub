"""Canonical note metadata for everything cockpit writes into the Obsidian
vault — issue #365 phase 8, `10_OBSIDIAN_HARDENING.md` improvement 1.

Every vault note cockpit writes carries: ``knowledge_id`` (stable across
restarts — derived from identity + content, never a random uuid, so
re-deriving it from the same inputs always names the same fact),
``project_id`` (``project_identity.resolve_project_id``, never a raw
display string — kept out of *this* module to avoid a config/leaf import
here; callers pass the already-resolved id in), ``source``, ``kind``,
``trust``, ``created_at``/``updated_at``, ``content_hash``.

Pure/stdlib-only module — no filesystem I/O, no config import — so it can
be reused by a future OpenViking adapter without pulling in anything.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime

# Trust levels this codebase's writers currently use. Not an exhaustive
# enum enforced at runtime (a future writer/adapter may need another
# value) — named constants so call sites don't scatter magic strings.
TRUST_AUTO = "auto"  # raw, unreviewed (e.g. one `takkub done` session note)
TRUST_DISTILLED = "distilled"  # auto-extracted durable fact (decision/bug/pattern)
TRUST_CURATED = "curated"  # a human touched it (backfill, manual edit)


def content_hash(text: str) -> str:
    """sha256 hex digest of *text* — the raw signal both ``knowledge_id``
    and the dedup index key off, independent of surrounding frontmatter or
    formatting so a reformatted-but-unchanged note still dedups."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_knowledge_id(project_id: str, source: str, kind: str, text: str) -> str:
    """Stable id for one durable fact:
    ``sha256(project_id|source|kind|content_hash(text))``, truncated to 16
    hex chars (64 bits — collision odds are irrelevant at this corpus
    size; short enough to stay readable in frontmatter/filenames/log
    lines). Deterministic: the same four inputs always produce the same
    id, on any machine, on any restart — that determinism IS the
    persistent-dedup primitive (`obsidian_dedup` looks records up by it).
    """
    h = content_hash(text)
    digest = hashlib.sha256(f"{project_id}|{source}|{kind}|{h}".encode()).hexdigest()
    return digest[:16]


@dataclass(frozen=True, slots=True)
class NoteMetadata:
    knowledge_id: str
    project_id: str
    source: str
    kind: str
    trust: str
    content_hash: str
    created_at: str
    updated_at: str

    @classmethod
    def new(
        cls,
        *,
        project_id: str,
        source: str,
        kind: str,
        trust: str,
        text: str,
        now: datetime | None = None,
    ) -> NoteMetadata:
        """Build metadata for a freshly-written note. ``created_at`` and
        ``updated_at`` start equal; call `.touched()` on a later rewrite of
        the SAME fact."""
        now = now or datetime.now()
        iso = now.isoformat(timespec="seconds")
        return cls(
            knowledge_id=make_knowledge_id(project_id, source, kind, text),
            project_id=project_id,
            source=source,
            kind=kind,
            trust=trust,
            content_hash=content_hash(text),
            created_at=iso,
            updated_at=iso,
        )

    def touched(self, *, now: datetime | None = None) -> NoteMetadata:
        """Same identity (`knowledge_id`/`created_at` unchanged), only
        `updated_at` bumped — used when an existing `knowledge_id` is
        written again: an update, not a new note."""
        now = now or datetime.now()
        return replace(self, updated_at=now.isoformat(timespec="seconds"))

    def frontmatter_lines(self) -> list[str]:
        """YAML ``key: value`` lines (no leading/trailing ``---``) ready
        to merge into a note's existing frontmatter block."""
        return [
            f"knowledge_id: {self.knowledge_id}",
            f"project_id: {self.project_id}",
            f"source: {self.source}",
            f"kind: {self.kind}",
            f"trust: {self.trust}",
            f"content_hash: {self.content_hash}",
            f"created_at: {self.created_at}",
            f"updated_at: {self.updated_at}",
        ]
