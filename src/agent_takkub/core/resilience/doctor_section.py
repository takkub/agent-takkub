"""`takkub doctor`'s "resilience" section — one row per `policy.POLICY`
entry, showing its fail-open label plus (for the entries actually wired to a
breaker) the last-known circuit state from `circuit_breaker.
load_persisted_state()`.

Lives here rather than inline in `doctor.py` for the exact reason `core.
context_sources.doctor_section` already gives: `doctor.py` transitively
imports PyQt6, so this module — bound by `core-is-bottom-layer` — must never
import it back, not even lazily; `doctor.check_resilience` only wraps this
module's plain rows into its own `Finding` objects."""

from __future__ import annotations

from dataclasses import dataclass

from .circuit_breaker import CircuitState, load_persisted_state
from .policy import POLICY


@dataclass(frozen=True, slots=True)
class FindingRow:
    """Doctor-agnostic stand-in for `doctor.Finding` — see `core.context_
    sources.doctor_section.FindingRow` for why this is its own copy rather
    than a shared import (keeps the two doctor-section modules free to
    change independently, and neither has to import `agent_takkub.doctor`
    to get the type)."""

    category: str
    name: str
    status: str
    detail: str = ""


def _status_for_state(state: str) -> str:
    if state == CircuitState.OPEN:
        return "warn"
    if state == CircuitState.HALF_OPEN:
        return "info"
    return "ok"  # closed


def build_findings() -> list[FindingRow]:
    persisted = load_persisted_state() or {}
    findings: list[FindingRow] = []

    for service in sorted(POLICY):
        spec = POLICY[service]
        if not spec.breaker:
            findings.append(
                FindingRow(
                    "resilience",
                    service,
                    "info",
                    f"{spec.label} · fallback: {spec.fallback} · not breaker-wired ({spec.reason})",
                )
            )
            continue

        snap = persisted.get(service)
        if snap is None:
            findings.append(
                FindingRow(
                    "resilience",
                    service,
                    "info",
                    f"{spec.label} · fallback: {spec.fallback} · no calls recorded yet",
                )
            )
            continue

        state = snap.get("state", "?")
        detail = (
            f"{spec.label} · state={state} "
            f"failures={snap.get('failure_count')}/{snap.get('failure_threshold')}"
        )
        if state == CircuitState.OPEN and snap.get("open_until") is not None:
            detail += f" open_until={snap['open_until']:.0f}"
        findings.append(FindingRow("resilience", service, _status_for_state(state), detail))

    return findings


__all__ = ["FindingRow", "build_findings"]
