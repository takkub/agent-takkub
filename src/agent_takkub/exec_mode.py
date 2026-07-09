"""Execution mode — how aggressively the Lead parallelises a request.

Two modes, toggled from the status bar and persisted across restart (same UX as
`plan_tier.py` / `provider_state.py`):

  • SOLO (1:1, default)  — one agent per role, the cockpit's original behaviour.
    Lead spawns a single frontend / backend / … and works features in sequence.

  • PARALLEL (multi)     — when a request decomposes into K independent features,
    the Lead fans out K instances per relevant role (frontend#1..#K,
    backend#1..#K) and runs them concurrently, like a team of several devs per
    position, to finish faster. K is the Lead's call (one per independent
    feature); the Lead is told to sequence large batches in waves rather than
    given a hard numeric ceiling (#2, 2026-07-09 core-upgrade plan — a
    machine-derived cap in the planning prompt read as an artificial limit on
    "unlimited" fan-out). `MAX_FANOUT` / `machine_fanout_cap()` below remain for
    the total-pane oversubscription telemetry (`machine_total_pane_cap()`,
    `orchestrator._warn_lead_over_cap` / the opt-in `TAKKUB_QUEUE_FANOUT` queue)
    — that safety path is unchanged and never fed into the Lead's planning text.

This module only stores the *intent* (a flag + the cap). The engine already
supports the `role#N` instances this produces (`takkub assign --role <role>
--shards N`, and direct `--role frontend#2` assigns).

State file: ``~/.takkub/exec-mode.json``  Format: ``{"mode": "solo"}`` |
``{"mode": "parallel"}``. Missing / corrupt → SOLO (no surprise parallelism for
an install that predates this setting).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import SETTINGS_HOME

SOLO = "solo"
PARALLEL = "parallel"

MODES: frozenset[str] = frozenset({SOLO, PARALLEL})
_DEFAULT = SOLO

# Ceiling used by machine_fanout_cap() below. NOT surfaced to the Lead's
# planning prompt (see module docstring, #2 2026-07-09) — kept only as the
# per-role component of the total-pane oversubscription telemetry.
MAX_FANOUT = 4

_PATH = SETTINGS_HOME / "exec-mode.json"


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


def machine_fanout_cap() -> int:
    """Max instances-per-role this machine can comfortably run concurrently,
    derived from CPU cores + free RAM, never above `MAX_FANOUT`.

    Each extra instance is roughly another claude.exe pane (often plus a dev
    server), so we budget ~2 logical cores and ~2 GB of *available* RAM per
    concurrent pane and take the tighter limit. NOT injected into the Lead's
    planning prompt (that block gives a qualitative wave-sequencing advisory
    instead, #2 2026-07-09) — this stays as a component of the total-pane
    oversubscription telemetry (`machine_total_pane_cap()`) and is exercised
    directly by tests for that reason.

    Falls back to a conservative 2 if psutil/cpu_count are unavailable, so this
    never raises on an odd environment.
    """
    try:
        cores = os.cpu_count() or 4
    except Exception:
        cores = 4
    by_cpu = max(1, cores // 2)
    try:
        import psutil

        free_gb = psutil.virtual_memory().available / (1024**3)
        by_ram = max(1, int(free_gb // 2))
    except Exception:
        by_ram = 2
    return max(1, min(MAX_FANOUT, by_cpu, by_ram))


def machine_total_pane_cap() -> int:
    """Max **total** concurrent teammate panes this machine can run before it
    starts to thrash — across *all* roles and *all* project tabs.

    This is the machine-oversubscription guard, distinct from
    `machine_fanout_cap()`: that one is *per role* and ceilinged at `MAX_FANOUT`
    (so the Lead doesn't fan a single role into a swarm), whereas this bounds the
    aggregate pane count. A machine can sit within the per-role cap for every
    role yet still be oversubscribed in total (e.g. frontend#1..#3 + backend#1..#3
    = 6 panes, each role within cap 3, but 6 claude.exe + dev servers together can
    blow a small box's RAM). The cockpit uses this only to *warn* the Lead when a
    fresh spawn would push the total over the line — it never blocks the spawn.

    Same budget as `machine_fanout_cap()` (~2 logical cores + ~2 GB available per
    pane, tighter wins) but WITHOUT the `MAX_FANOUT` ceiling, since a big box can
    legitimately run more than `MAX_FANOUT` panes in total. Floor 1; falls back
    conservatively to 2 if psutil/cpu_count are unavailable so it never raises.
    """
    try:
        cores = os.cpu_count() or 4
    except Exception:
        cores = 4
    by_cpu = max(1, cores // 2)
    try:
        import psutil

        free_gb = psutil.virtual_memory().available / (1024**3)
        by_ram = max(1, int(free_gb // 2))
    except Exception:
        by_ram = 2
    return max(1, min(by_cpu, by_ram))


def set_current(mode: str) -> None:
    """Persist `mode` atomically. Raises ValueError on an unknown mode."""
    mode = str(mode).lower().strip()
    if mode not in MODES:
        raise ValueError(f"unknown execution mode: {mode!r}")
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps({"mode": mode}, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_PATH)
