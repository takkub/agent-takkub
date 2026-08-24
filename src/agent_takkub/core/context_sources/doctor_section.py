"""`takkub doctor`'s "context" section (`19_DIAGNOSTICS_OBSERVABILITY.md`:
last Context Builder trace — token counts/latency/dedup). Obsidian's own
vault/canonical-metadata/dedup-index status is already `doctor.
check_obsidian` (opt-in `--obsidian`, #365 phase 8) — not repeated here.
Lives here (not inline in `doctor.py`) so a parallel pane editing
`doctor.py`'s own body for an unrelated section never collides with this
one — `doctor.py`'s `check_context` only wraps `build_findings()`'s rows
into its own `Finding` objects (see that function for why: `doctor.py`
transitively imports PyQt6 (`check_qt`), so `core.context_sources` — bound
by `core-is-bottom-layer` — must never import `agent_takkub.doctor` itself,
not even a lazy function-level import; import-linter's static analysis
sees those the same as a top-of-file one).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FindingRow:
    """Doctor-agnostic stand-in for `doctor.Finding` — `status` is one of
    doctor's own `Status` string values ("ok"/"warn"/"fail"/"skip"/"info"),
    kept as a plain `str` here so this module never has to import the enum
    (or anything else) from `agent_takkub.doctor`."""

    category: str
    name: str
    status: str
    detail: str = ""


def build_findings() -> list[FindingRow]:
    from .trace_store import load_last_trace

    findings: list[FindingRow] = []
    trace = load_last_trace()
    if trace is None:
        findings.append(
            FindingRow("context", "last-trace", "skip", "no context build recorded yet")
        )
    else:
        src_summary = ", ".join(
            f"{s['name']}={s['count']}/{s['tokens']}t"
            + (
                f" rej(scope={s.get('scope_rejects', 0)},trust={s.get('trust_rejects', 0)})"
                if s.get("scope_rejects") or s.get("trust_rejects")
                else ""
            )
            for s in trace.get("sources", [])
        )
        task_size = trace.get("task_size")
        inefficient = bool(trace.get("inefficient"))
        detail = (
            f"mode={trace.get('mode')} {src_summary} total={trace.get('total_tokens')}/"
            f"{trace.get('budget_tokens')} dedup={trace.get('dedup_count')} "
            f"latency={trace.get('latency_ms', 0):.0f}ms"
        )
        if task_size is not None:
            detail += f" size={task_size}"
        score = trace.get("score")
        if score is not None:
            detail += f" score={score} confidence={trace.get('confidence')}"
        risk_flags = trace.get("risk_flags")
        if risk_flags:
            detail += f" risk={risk_flags}"
        if inefficient:
            detail += " ⚠ inefficient (small task over 15k tokens)"
        findings.append(
            FindingRow(
                "context",
                "last-trace",
                "warn" if inefficient else "info",
                detail,
            )
        )

    return findings


__all__ = ["FindingRow", "build_findings"]
