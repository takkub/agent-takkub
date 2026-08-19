"""Errors for `core.providers` (Phase 3, epic #309)."""

from __future__ import annotations


class ProviderAdapterNotWired(NotImplementedError):
    """Raised by a `ProviderAdapter` method whose real implementation
    requires PyQt6/PTY process control — off-limits to `agent_takkub.core`
    under the `core-is-bottom-layer` import-linter contract.

    See `claude_adapter.py`/`cli_adapter.py` module docstrings: this phase
    wraps the *pure* half of each spawn_engine.py branch (provider
    discovery/availability) behind the `ProviderAdapter` Protocol; the
    process-lifecycle half (`spawn`/`send`/`is_ready`/`terminate`) stays
    owned by `Orchestrator.spawn_engine` until a later phase either exposes
    a non-core façade or inverts control. Raising here — instead of a
    silent no-op — keeps a caller that mistakenly expects a real spawn from
    failing quietly.
    """

    def __init__(self, provider_id: str, method: str) -> None:
        super().__init__(
            f"ProviderAdapter.{method}() for provider {provider_id!r} is not wired to a "
            "real process yet — the PTY/orchestrator side of this WRAP is out of scope for "
            "core.providers (core-is-bottom-layer forbids importing PyQt6/spawn_engine). "
            "Spawn this role through the normal orchestrator.spawn_engine.spawn() path; "
            "see docs/v2/phase3-report.md for the tracked gap."
        )
