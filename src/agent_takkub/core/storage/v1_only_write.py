"""V1-only write detector (#502, Phase 10 "V2.1" — prep for #504's V1
removal): find a V1 config file that was written more recently than its
``core.storage.dual_write`` mirror, meaning some writer changed it without
going through dual-write and the ``v2/`` copy went stale silently.

**Why mtime, not content-hash.** ``dual_write.py``'s every writer mirrors its
V1 file in the SAME call, right after the V1 write commits — so under a
working dual-write path the V2 target's mtime is never older than its V1
source's (barring the two writes landing in the same filesystem tick, which
``_MTIME_SLOP_S`` absorbs). A V1 source strictly newer than its V2 mirror
by more than that slop is the one shape a working dual-write path can never
produce on its own: a writer changed V1 without mirroring. Hashing every
mapped file on every scan would cost real I/O to answer a question mtimes
already answer for free.

**Reuses the same mapping objects dual-write and the ladder itself are built
from** (`core.migration.steps_v1`'s ``RegistryMapping`` tuples for the flat
registries, plus the two fan-out steps' own source/target methods) — same
rule `dual_write.py`'s own docstring states for why its V2 target paths are
never recomputed by hand. Only 1:1 (one V1 file -> one V2 mirror) mappings
are covered; ``role-providers`` (routing.json, global + every known
project's role-providers.json merged into one target) has no single V1
source to compare a target's mtime against, so it is deliberately skipped
here the same way it needs its own re-read-every-scope dance in
``dual_write.dual_write_routing``.

**On-demand only** (`doctor --storage-layout`, `migrate validate` — both
CLI-triggered, pure-local, no cockpit required). A handful of `stat()` calls
per invocation, not a scan of anything unbounded, and nothing here runs on a
timer or the Qt main thread — see #488 for why a V2 storage path stalling
the main thread is a defect class this module must not repeat.

Pure detection only — returns hits, never logs. Writing `v1_only_write` to
`events.log` needs `orchestrator_text._log_event`, which this module (under
`agent_takkub.core`, bound by the `core-is-bottom-layer` import-linter
contract) must never import; callers outside `core` (`doctor.py`, `cli.py`)
do that logging themselves from the hits this returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .dual_write import _effective_data_home, _v2_present

# Coarse mtime resolution on some filesystems (FAT32-class, 2s granularity)
# plus slack for "V1 write, then its dual-write mirror" landing in the same
# tick — a gap smaller than this is normal, working dual-write, not drift.
_MTIME_SLOP_S = 2.0


@dataclass(frozen=True, slots=True)
class V1OnlyWriteHit:
    name: str
    source: Path
    target: Path
    source_mtime: float
    target_mtime: float

    @property
    def lag_s(self) -> float:
        return round(self.source_mtime - self.target_mtime, 1)


def _hit(name: str, source: Path, target: Path) -> V1OnlyWriteHit | None:
    try:
        if not source.exists() or not target.exists():
            return None
        source_mtime = source.stat().st_mtime
        target_mtime = target.stat().st_mtime
    except OSError:
        return None
    if source_mtime - target_mtime > _MTIME_SLOP_S:
        return V1OnlyWriteHit(name, source, target, source_mtime, target_mtime)
    return None


def scan_v1_only_writes(*, data_home: Path | None = None) -> list[V1OnlyWriteHit]:
    """Every known 1:1 V1->V2 mapping whose V1 source is newer than its
    dual-write mirror. Empty on a not-yet-migrated machine (no ``v2/`` root
    — nothing to compare a source against yet) or when nothing has drifted."""
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return []

    from ..migration.steps_v1 import (
        ProjectMigrationStep,
        RoleAgentMigrationStep,
        build_capability_step,
        build_readonly_registries_step,
        build_state_step,
    )

    hits: list[V1OnlyWriteHit] = []
    for builder in (build_readonly_registries_step, build_capability_step, build_state_step):
        for mapping in builder(data_home=effective).mappings:
            hit = _hit(mapping.name, mapping.source, mapping.target)
            if hit is not None:
                hits.append(hit)

    role_agent = RoleAgentMigrationStep(data_home=effective)
    hit = _hit("custom-roles", role_agent._custom_roles_source(), role_agent._custom_roles_target())
    if hit is not None:
        hits.append(hit)

    project = ProjectMigrationStep(data_home=effective)
    hit = _hit("projects-registry", effective / "projects.json", project._registry_target())
    if hit is not None:
        hits.append(hit)

    return hits


__all__ = ["V1OnlyWriteHit", "scan_v1_only_writes"]
