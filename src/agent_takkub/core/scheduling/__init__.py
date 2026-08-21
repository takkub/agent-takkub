"""Scheduler / Resource / Runtime V2 (epic #309 Phase 8a). Pure priority,
slot-policy, backpressure, process-registry and runtime-control vocabulary
that EXTENDS `resource_governor.py` (docs/v2/REUSE_VS_REWRITE_MATRIX.md §5)
rather than replacing it — see `docs/v2/07_SCHEDULER_RESOURCE_RUNTIME.md`.

On by default since 1.0.84 (`TAKKUB_V2_SCHEDULER=0` disables, see `.flag`) — `.facade` is the ONE
entry point `resource_governor.py` calls into, and every function there is a
no-op with the flag off, so `resource_governor` stays byte-identical (plan
§0 rule 4).
"""

from __future__ import annotations
