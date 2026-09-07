"""Execution mode — how aggressively the Lead parallelises a request.

  • SOLO (1:1)       — one agent per role, the cockpit's original behaviour.
    Lead spawns a single frontend / backend / … and works features in sequence.

  • PARALLEL (multi) — when a request decomposes into K independent features,
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

#515 Settings diet: this used to be its own status-bar chip persisted to
``exec-mode.json`` — but it duplicates the team preset (#512) switch one
level up: "ทำเอง"/"คู่" is always 1 agent per role, "ทีมเต็ม" always fans
out. `is_parallel()` now derives straight from the project's effective
preset (`team_preset.current(project)["exec_mode"]`) instead of reading its
own file — a "custom" preset carries its own explicit `exec_mode` field, and
"auto" resolves to PARALLEL (see `team_preset._resolve`). No more standalone
toggle/chip/file to keep in sync with the preset.
"""

from __future__ import annotations

import os

SOLO = "solo"
PARALLEL = "parallel"

# Ceiling used by machine_fanout_cap() below. NOT surfaced to the Lead's
# planning prompt (see module docstring, #2 2026-07-09) — kept only as the
# per-role component of the total-pane oversubscription telemetry.
MAX_FANOUT = 4

# A measured Claude/Codex pane is typically around 350 MB. Keep some room for
# transient growth while avoiding the old 2 GB/pane estimate, which made the
# advisory report false over-capacity warnings on otherwise comfortable hosts.
_PANE_RAM_GB = 0.5

# Instantaneous ``available`` memory can dip sharply while the OS is using RAM
# for reclaimable cache (especially on Windows). Treat a modest share of total
# RAM as reclaimable headroom so a momentary sample cannot collapse the cap.
_RAM_HEADROOM_FRACTION = 0.25


def _archive_legacy_file() -> None:
    """One-time cleanup of the pre-#515 ``exec-mode.json`` chip state — its
    value is meaningless now (`current()` derives from the team preset
    instead), so there is nothing to carry forward, just the file itself to
    keep around per the "never delete, archive" rule."""
    from . import config

    config.archive_settings_file(config.SETTINGS_HOME / "exec-mode.json")


def current(project: str | None = None) -> str:
    """SOLO or PARALLEL, derived from the project's effective team preset
    (#512) — see module docstring. `project=None` resolves the "default"
    project slug, same fallback `team_preset.current()` itself uses."""
    _archive_legacy_file()
    from . import team_preset

    return team_preset.current(project).get("exec_mode", PARALLEL)


def is_parallel(project: str | None = None) -> bool:
    """True iff the Lead should fan out independent features across
    instances for this project right now."""
    return current(project) == PARALLEL


def _base_pane_cap() -> int:
    """Shared CPU/RAM pane budget before any per-role ceiling."""
    try:
        cores = os.cpu_count() or 4
    except Exception:
        cores = 4
    by_cpu = max(1, cores // 2)
    try:
        import psutil

        memory = psutil.virtual_memory()
        stable_available = max(memory.available, memory.total * _RAM_HEADROOM_FRACTION)
        stable_available_gb = stable_available / (1024**3)
        by_ram = max(1, int(stable_available_gb // _PANE_RAM_GB))
    except Exception:
        by_ram = 2
    return max(1, min(by_cpu, by_ram))


def machine_fanout_cap() -> int:
    """Max instances-per-role this machine can comfortably run concurrently,
    derived from CPU cores + free RAM, never above `MAX_FANOUT`.

    Each extra instance is roughly another agent pane, so we budget ~2 logical
    cores and ~0.5 GB of RAM per concurrent pane and take the tighter limit.
    The RAM basis is the larger of currently available RAM and 25% of total RAM,
    avoiding false warnings from a single pessimistic available-memory sample.
    NOT injected into the Lead's planning prompt (that block gives a qualitative
    wave-sequencing advisory instead, #2 2026-07-09) — this stays as a component
    of the total-pane oversubscription telemetry (`machine_total_pane_cap()`) and
    is exercised directly by tests for that reason.

    Falls back to a conservative 2 if psutil/cpu_count are unavailable, so this
    never raises on an odd environment.
    """
    return max(1, min(MAX_FANOUT, _base_pane_cap()))


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

    Same budget as `machine_fanout_cap()` (~2 logical cores + ~0.5 GB stable RAM
    headroom per pane, tighter wins) but WITHOUT the `MAX_FANOUT` ceiling, since
    a big box can legitimately run more than `MAX_FANOUT` panes in total.
    Floor 1; falls back conservatively to 2 if psutil/cpu_count are unavailable
    so it never raises.
    """
    return _base_pane_cap()
