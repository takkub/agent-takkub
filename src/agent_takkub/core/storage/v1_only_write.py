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
never recomputed by hand. Most mappings are 1:1 (one V1 file -> one V2
mirror, `_hit`); ``role-providers`` (routing.json, global + every known
project's role-providers.json merged into ONE target by
`dual_write.dual_write_routing`) has no single V1 source, so it gets its
own many-to-one check (`_fanout_hit`): since a working
`dual_write_routing` call always rewrites the WHOLE merged target from
every current scope in one write, any single scope's V1 source newer than
the target's last write is drift, exactly like the 1:1 case.

**Missing mirror is itself a hit** (#502/#504 review 2026-09-07): a V1
source with NO ``v2/`` mirror at all — the domain was never migrated to
begin with, or (the case that matters most) a fresh V1-only write created
the source for a domain that dual-write never got wired up for — used to
read as zero findings under the old "both files must exist" rule, which is
exactly the shape #504's exit gate must not silently pass. Reported with
``reason="missing_mirror"`` (``target_mtime`` is ``None``, ``lag_s`` is
``None``) so a caller can tell it apart from a normal stale-mirror lag.

**On-demand only** (`doctor --storage-layout`, `migrate validate` — both
CLI-triggered, pure-local, no cockpit required). A handful of `stat()` calls
per invocation, not a scan of anything unbounded, and nothing here runs on a
timer or the Qt main thread — see #488 for why a V2 storage path stalling
the main thread is a defect class this module must not repeat. Being
on-demand also means it only ever sees ONE snapshot: a writer that skipped
dual-write and then got dual-written correctly on its very next save erases
its own drift before the next scan runs — #504's one-week dev+prod gate
needs periodic sampling, not a single clean run, to actually prove drift=0.

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
    target_mtime: float | None
    reason: str = "stale_mirror"  # "stale_mirror" | "missing_mirror"

    @property
    def lag_s(self) -> float | None:
        if self.target_mtime is None:
            return None
        return round(self.source_mtime - self.target_mtime, 1)


def _hit(name: str, source: Path, target: Path) -> V1OnlyWriteHit | None:
    try:
        if not source.exists():
            return None
        source_mtime = source.stat().st_mtime
    except OSError:
        return None
    try:
        target_exists = target.exists()
    except OSError:
        return None
    if not target_exists:
        return V1OnlyWriteHit(name, source, target, source_mtime, None, reason="missing_mirror")
    try:
        target_mtime = target.stat().st_mtime
    except OSError:
        return None
    if source_mtime - target_mtime > _MTIME_SLOP_S:
        return V1OnlyWriteHit(name, source, target, source_mtime, target_mtime)
    return None


def _fanout_hit(name: str, sources: dict[str, Path], target: Path) -> V1OnlyWriteHit | None:
    """Many V1 sources merged into ONE V2 target (role-providers ->
    routing.json). A working `dual_write.dual_write_routing` call always
    rewrites the whole target from every scope's current V1 state in one
    write, so the single newest existing source's mtime is the right thing
    to compare against the target — same rule as `_hit`, just fed the max
    over every scope instead of one file. Reports only the worst-drifted
    scope (the target write is atomic across all scopes, so one drifted
    scope already means the same "some writer skipped dual-write" finding
    as several would)."""
    newest_scope: str | None = None
    newest_mtime: float | None = None
    for scope, source in sources.items():
        try:
            if not source.exists():
                continue
            mtime = source.stat().st_mtime
        except OSError:
            continue
        if newest_mtime is None or mtime > newest_mtime:
            newest_mtime, newest_scope = mtime, scope
    if newest_mtime is None or newest_scope is None:
        return None  # no V1 source exists yet — nothing has been written
    worst_source = sources[newest_scope]
    hit_name = f"{name}:{newest_scope}"
    try:
        target_exists = target.exists()
    except OSError:
        return None
    if not target_exists:
        return V1OnlyWriteHit(
            hit_name, worst_source, target, newest_mtime, None, reason="missing_mirror"
        )
    try:
        target_mtime = target.stat().st_mtime
    except OSError:
        return None
    if newest_mtime - target_mtime > _MTIME_SLOP_S:
        return V1OnlyWriteHit(hit_name, worst_source, target, newest_mtime, target_mtime)
    return None


def scan_v1_only_writes(*, data_home: Path | None = None) -> list[V1OnlyWriteHit]:
    """Every known V1->V2 mapping (1:1 registries, plus the role-providers
    fan-out) whose V1 source is newer than its dual-write mirror, OR whose
    mirror doesn't exist at all despite the source existing
    (``reason="missing_mirror"``). Empty on a not-yet-migrated machine (no
    ``v2/`` root — nothing to compare a source against yet) or when nothing
    has drifted."""
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

    # #502/#504 review (2026-09-07): role-providers is a many-V1-sources ->
    # one-V2-target fan-out (global + every known project's
    # role-providers.json, merged by `dual_write.dual_write_routing`) — no
    # single V1 file to hand `_hit`, so it gets its own many-to-one check.
    # #480 previously drifted exactly here, and the old scan skipped it
    # entirely.
    hit = _fanout_hit("role-providers", role_agent._routing_sources(), role_agent._routing_target())
    if hit is not None:
        hits.append(hit)

    project = ProjectMigrationStep(data_home=effective)
    hit = _hit("projects-registry", effective / "projects.json", project._registry_target())
    if hit is not None:
        hits.append(hit)

    return hits


__all__ = ["V1OnlyWriteHit", "scan_v1_only_writes"]
