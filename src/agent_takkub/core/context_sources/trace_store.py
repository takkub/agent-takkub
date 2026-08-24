"""Best-effort persistence of the most recent Context Builder / OpenViking
merge trace (`19_DIAGNOSTICS_OBSERVABILITY.md`'s "Context: last context
source token counts, retrieval latency, dedup count"). `doctor.py` is a
leaf module with no live-cockpit round-trip for this — a plain `takkub
doctor` runs as its own short-lived process — so the only way it can see
"what did the last context build look like" is a small file on disk, not
in-memory state a separate process could never reach.

Single "last" record, not a history: matches the diagnostics doc's own
singular framing, and keeps this a fixed-size file regardless of how many
`assign`s a project has run.
"""

from __future__ import annotations

import json
import logging
import os
import time

_log = logging.getLogger(__name__)


def _trace_path():
    from agent_takkub import config

    return config.DATA_HOME / "openviking" / "last_context_trace.json"


def save_last_trace(
    trace,
    *,
    project: str | None,
    role: str,
    task_size: str | None = None,
    inefficient: bool = False,
) -> None:
    """Never raises — a failed trace write must not turn a successful
    context build into a failed `assign`. `task_size`/`inefficient` are the
    Context Gate's own additions (closeout #C, `03_CONTEXT_TOKEN_
    EFFICIENCY.md`) — both optional so a pre-gate caller (or the gate
    disabled via `TAKKUB_CONTEXT_GATE=0`) still writes the exact same
    payload shape as before."""
    try:
        path = _trace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.time(),
            "project": project,
            "role": role,
            "mode": trace.mode,
            "sources": [
                {
                    "name": s.name,
                    "count": s.count,
                    "unit": s.unit,
                    "tokens": s.tokens,
                    "scope_rejects": s.scope_rejects,
                    "trust_rejects": s.trust_rejects,
                }
                for s in trace.sources
            ],
            "total_tokens": trace.total_tokens,
            "budget_tokens": trace.budget_tokens,
            "dedup_count": trace.dedup_count,
            "latency_ms": trace.latency_ms,
            "scope_rejects": trace.scope_rejects,
            "trust_rejects": trace.trust_rejects,
            "rejected_examples": list(trace.rejected_examples),
        }
        if task_size is not None:
            payload["task_size"] = task_size
            payload["inefficient"] = inefficient
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        _log.debug("trace_store.save_last_trace failed (best-effort)", exc_info=True)


def load_last_trace() -> dict | None:
    try:
        path = _trace_path()
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


__all__ = ["load_last_trace", "save_last_trace"]
