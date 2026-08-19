"""Read-only ingest adapters: each turns an EXISTING cockpit text source
into `MemoryCandidate`s for `MemoryManager.submit_candidate` — WRAP, not
REPLACE (REUSE_VS_REWRITE_MATRIX.md §4): none of these re-implement or
modify the reader/writer they source from.
"""

from __future__ import annotations
