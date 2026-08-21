"""Conversation V2 + Checkpoint (epic #309 Phase 6) — Takkub-owned message
store, provider-transcript ingest adapters, rolling summary, and checkpoint
snapshots. See `docs/v2/phase6-report.md` for what's wired vs. gap.

On by default since 1.0.84 (`TAKKUB_V2_CONVERSATION=0` disables, see `.flag`) — every hook this
package exposes into the existing engine is fail-open and a no-op with the
flag off, so `orchestrator.done()` stays byte-identical (plan §0 rule 4).
"""

from __future__ import annotations
