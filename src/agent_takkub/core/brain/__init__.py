"""Second Brain (Phase 7a/7b, epic #309): candidate pipeline + retrieval.
Context Builder + Reflection hook (7c) wire this into the orchestrator —
see `facade.py` for the two hook entry points.
"""

from __future__ import annotations

from .adapter import NativeBrainAdapter
from .candidate import MemoryCandidate
from .flag import v2_brain_enabled, v2_context_enabled
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
    "v2_context_enabled",
]
