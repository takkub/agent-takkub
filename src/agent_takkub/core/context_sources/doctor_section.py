"""`takkub doctor`'s "knowledge/context" section (issue #372, `19_
DIAGNOSTICS_OBSERVABILITY.md`: Knowledge = OpenViking enabled+health+
version+indexed counts; Context = last trace token counts/latency/dedup).
Obsidian's own vault/canonical-metadata/dedup-index status is already
`doctor.check_obsidian` (opt-in `--obsidian`, #365 phase 8) — not repeated
here. Lives here (not inline in `doctor.py`) so a parallel pane editing
`doctor.py`'s own body for an unrelated section never collides with this
one — `doctor.py`'s `check_knowledge_context` only wraps `build_findings()`'s
rows into its own `Finding` objects (see that function for why: `doctor.py`
transitively imports PyQt6 (`check_qt`), so `core.context_sources` — bound
by `core-is-bottom-layer` — must never import `agent_takkub.doctor` itself,
not even a lazy function-level import; import-linter's static analysis
sees those the same as a top-of-file one).

`doctor.py` is a leaf module (no live cockpit needed for this check): the
OpenViking health call is a single short-timeout `GET /health`, skipped
entirely when the feature is disabled (the default) so a plain `takkub
doctor` never pays a network round-trip for a sidecar nobody configured.
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


def build_findings(project: str | None = None) -> list[FindingRow]:
    from . import openviking_adapter as adapter
    from .indexing import index_status
    from .trace_store import load_last_trace

    findings: list[FindingRow] = []

    if not adapter.enabled():
        findings.append(
            FindingRow("knowledge", "openviking", "skip", "disabled (TAKKUB_OPENVIKING_ENABLED=0)")
        )
    else:
        status = index_status(project)
        if not status["health_ok"]:
            findings.append(
                FindingRow(
                    "knowledge",
                    "openviking",
                    "warn",
                    f"mode={status['mode']} sidecar unreachable at {adapter.base_url()}",
                )
            )
        else:
            level = "ok" if status["healthy"] and status["known_version"] else "warn"
            detail = (
                f"mode={status['mode']} healthy={status['healthy']} "
                f"version={status['version'] or '?'} known_version={status['known_version']} "
                f"indexed={status['indexed_count']}"
            )
            findings.append(FindingRow("knowledge", "openviking", level, detail))

    trace = load_last_trace()
    if trace is None:
        findings.append(
            FindingRow("context", "last-trace", "skip", "no context build recorded yet")
        )
    else:
        src_summary = ", ".join(
            f"{s['name']}={s['count']}/{s['tokens']}t" for s in trace.get("sources", [])
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
