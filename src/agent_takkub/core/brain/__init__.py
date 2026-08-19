"""Second Brain (Phase 7a/7b, epic #309): candidate pipeline + retrieval.
Context Builder (7c) wires this into the orchestrator; nothing does yet.
"""

from __future__ import annotations

from .adapter import NativeBrainAdapter
from .candidate import MemoryCandidate
from .flag import v2_brain_enabled
from .pipeline import MemoryManager, SubmitResult, SubmitStatus
from .retrieval import RetrievalEngine
from .store import BrainStore

__all__ = [
    "BrainStore",
    "MemoryCandidate",
    "MemoryManager",
    "NativeBrainAdapter",
    "RetrievalEngine",
    "SubmitResult",
    "SubmitStatus",
    "v2_brain_enabled",
]
