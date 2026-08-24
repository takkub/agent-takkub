"""core.resilience — v2-hardening D/F: a central fail-open policy (service ->
label, `docs/plans/v2-hardening-2026-08-24/10_FAIL_OPEN_MATRIX.md`) and a
generic per-service circuit breaker (`11_CIRCUIT_BREAKER.md`) every optional
integration can share instead of each module growing its own ad-hoc
timeout/retry bookkeeping.

Two siblings, not one file, for the same reason `core.context_sources`
already splits `trace_store` from `doctor_section`: `circuit_breaker.py` is
the runtime primitive (in-memory state + best-effort persistence),
`policy.py` is a static data table (service -> label/fallback/breaker?),
and `doctor_section.py` is the `takkub doctor` read-only view over both —
three call sites with three different reasons to change.
"""

from __future__ import annotations
