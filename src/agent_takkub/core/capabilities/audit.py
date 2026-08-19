"""Capability Hub audit log (Phase 5c, epic #309).

Appends structured events to the SAME `EVENTS_LOG` JSONL file
`orchestrator_text._log_event` / `lead_context._log_event` /
`pipeline_executor._log_event` already write to — matrix §5 Platform
Layer "Observability": REUSE + EXTEND ("เติม audit fields"), and "Event
bus": REUSE ("ห้ามเพิ่ม bus ตัวที่สอง"). `core/` cannot import any of
those three (they sit above `core` in `core-is-bottom-layer` — importing
one back into `core` would create the exact UI/engine→core→UI/engine
cycle that contract exists to forbid), so this duplicates their same
minimal tmp-free JSONL-append shape rather than calling one of them
directly — consistent with those three already being three independent
copies of the same pattern, not one shared helper.

Additive only: this never touches the *existing* `{"ts", "event", ...}`
shape those three write — it just appends its own `capability.*` event
names with a fixed field set (who/agent/provider/account/tool) to the
same file, so a log reader that greps `EVENTS_LOG` sees Capability Hub
activity interleaved with everything else, in one timeline.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from agent_takkub import config

_log = logging.getLogger(__name__)

# Same rotation threshold as orchestrator_text._EVENTS_LOG_MAX_BYTES — kept
# as its own constant (not imported) for the same core-is-bottom-layer
# reason the log-write itself is duplicated rather than shared.
_EVENTS_LOG_MAX_BYTES = 5_000_000


def log_capability_event(
    event: str,
    *,
    who: str | None = None,
    agent: str | None = None,
    provider: str | None = None,
    account: str | None = None,
    tool: str | None = None,
    **extra: object,
) -> None:
    """Append one `capability.<event>`-shaped audit line. Best-effort —
    never raises, so an audit-log failure can never block a capability
    decision (mirrors every other `_log_event` in this codebase)."""
    try:
        # Read `config.EVENTS_LOG` fresh on every call (not bound at import
        # time) so tests that monkeypatch `config.EVENTS_LOG` — the same
        # convention `check_capability_skill_store` etc. rely on — are
        # honoured here too.
        events_log = config.EVENTS_LOG
        config.ensure_runtime()
        try:
            if events_log.exists() and events_log.stat().st_size > _EVENTS_LOG_MAX_BYTES:
                os.replace(events_log, events_log.parent / (events_log.name + ".old"))
        except OSError:
            pass
        payload = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "who": who,
            "agent": agent,
            "provider": provider,
            "account": account,
            "tool": tool,
            **extra,
        }
        line = json.dumps(payload, ensure_ascii=False)
        with open(events_log, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        _log.debug("log_capability_event: could not append %r", event, exc_info=True)
