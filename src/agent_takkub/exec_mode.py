"""Execution mode — how aggressively the Lead parallelises a request.

Two modes, toggled from the status bar and persisted across restart (same UX as
`plan_tier.py` / `provider_state.py`):

  • SOLO (1:1, default)  — one agent per role, the cockpit's original behaviour.
    Lead spawns a single frontend / backend / … and works features in sequence.

  • PARALLEL (multi)     — when a request decomposes into K independent features,
    the Lead fans out K instances per relevant role (frontend#1..#K,
    backend#1..#K) and runs them concurrently, like a team of several devs per
    position, to finish faster. K is the Lead's call (one per independent
    feature), bounded by `MAX_FANOUT` so a vague request can't explode the
    machine.

This module only stores the *intent* (a flag + the cap). The Lead's planning
behaviour reads it via the system-prompt block injected in `lead_context`, and
the engine already supports the `role#N` instances this produces (`takkub assign
--role <role> --shards N`, and direct `--role frontend#2` assigns).

State file: ``~/.takkub/exec-mode.json``  Format: ``{"mode": "solo"}`` |
``{"mode": "parallel"}``. Missing / corrupt → SOLO (no surprise parallelism for
an install that predates this setting).
"""

from __future__ import annotations

import json
from pathlib import Path

SOLO = "solo"
PARALLEL = "parallel"

MODES: frozenset[str] = frozenset({SOLO, PARALLEL})
_DEFAULT = SOLO

# Hard ceiling on instances-per-role the Lead may fan out in PARALLEL mode, so a
# broad request ("build the whole app") can't spawn an unbounded swarm. The Lead
# picks K = number of independent features up to this cap. Override per-install
# via TAKKUB_MAX_FANOUT (read in the orchestrator/Lead context, not here, to keep
# this module env-free and trivially testable).
MAX_FANOUT = 4

_PATH = Path.home() / ".takkub" / "exec-mode.json"


def path() -> Path:
    """Where state lives. Function form so tests can monkeypatch `_PATH`."""
    return _PATH


def current() -> str:
    """Return the current execution mode. Missing file or corrupt JSON → SOLO."""
    if not _PATH.exists():
        return _DEFAULT
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _DEFAULT
    if not isinstance(data, dict):
        return _DEFAULT
    mode = str(data.get("mode", _DEFAULT)).lower().strip()
    return mode if mode in MODES else _DEFAULT


def is_parallel() -> bool:
    """True iff the Lead should fan out independent features across instances."""
    return current() == PARALLEL


def set_current(mode: str) -> None:
    """Persist `mode` atomically. Raises ValueError on an unknown mode."""
    mode = str(mode).lower().strip()
    if mode not in MODES:
        raise ValueError(f"unknown execution mode: {mode!r}")
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps({"mode": mode}, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_PATH)
