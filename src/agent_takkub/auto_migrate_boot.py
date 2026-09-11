"""Auto `migrate apply` at boot (#361) — every device that boots 1.1.0+
lands on the same storage layout without anyone typing `takkub migrate
apply` themselves, gated behind the same pre-flight checks a human running
the CLI manually would be told to do (`docs/v2/2.0.0-migration-plan.md`
§2.3). (L1, 2026-09-10 acceptance review: this used to say every device
lands on `"mixed"` — #504 (2.1.0) retired that as the steady state; a fully
migrated installed machine now reaches `"v2"`, with `"mixed"` only ever
transient mid-ladder. `run_boot_stage()` below runs `apply_pending()` on
BOTH states forever, exactly alike, for exactly that reason.)

Reuses `MigrationEngine` exactly as `takkub migrate` does — no second ladder.
The whole point of running this at boot rather than leaving it to a human is
that boot is the one moment nothing else could be writing the files the
ladder reads/copies (no pane exists yet) — the equivalent of §2.3's "close
the cockpit first" instruction, done automatically. Callers (`app.py` /
`boot_update_window.py`) MUST finish this stage before constructing
MainWindow — a pane spawned mid-copy writing into RUNTIME_DIR/SETTINGS_HOME
would look like corruption to `validate()` and trigger a false rollback.

State kept at `_state_path()` — `storage_layout_v2().system/auto-migrate-
state.json` (#504 H8: moved off the bare ``SETTINGS_HOME`` root, which
`ArchiveV1LegacyStep` swept up as an unrecognized V1 leftover the instant
this module wrote it there; `load_state()` still falls back to the old spot
for a machine mid-upgrade):
  ``{"applied_version": "1.1.0"}``           — the first-ever full ladder
                                                apply (state was "v1") went
                                                ok.
  ``{"rolled_back_for_version": "1.1.0"}``   — that first-ever full-ladder
                                                attempt failed validate() and
                                                was rolled back whole; never
                                                retried until the running app
                                                version changes (no
                                                retry-loop on a bad box).
  ``{"rolled_back_steps": {"core-internal-store": "1.1.0"}}``
                                              — a *mixed*-state boot's
                                                `apply_pending()` (#362) hit a
                                                genuinely new ladder step
                                                (never applied on this
                                                machine before) that failed
                                                apply(); it was rolled back
                                                on its own and is held back by
                                                this per-(step, version) guard
                                                until the app version changes
                                                again — the OTHER pending
                                                steps that boot are unaffected.
  ``{"stale_applied_steps": {"state": "..."}}``
                                              — a *mixed*-state boot found a
                                                step that already had a
                                                successful apply on this
                                                machine, but whose validate()
                                                now fails — never
                                                auto-rolled-back (that would
                                                throw away state that used to
                                                be fine), just logged and
                                                surfaced as a
                                                `takkub doctor --storage-layout`
                                                WARN for a human to look at.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .core.migration.report import StepReport

_STATE_FILE = "auto-migrate-state.json"

# #504 H8 (acceptance review): a mixed-state boot stops being a one-shot
# event once its own bookkeeping file re-triggers the archive step — see
# `_state_path()`.

# Pre-flight disk gate: refuse to start a first-run apply unless free space
# is at least this many times the estimated copy size — the ladder is
# copy-never-move, so the real machine cost is disk, not data loss (#361
# design note).
_MIN_FREE_MULTIPLE = 2


def auto_migrate_enabled() -> bool:
    """`TAKKUB_AUTO_MIGRATE` always wins when set (`=0` is the escape hatch,
    same default-ON/env-`=0`-disables shape as `boot_update_window
    .boot_update_enabled`); unset falls back to the Settings UI's persisted
    Core V2 flag (`core_v2_settings`, same env-then-settings precedence
    chain every `core/*/flag.py` module already uses)."""
    raw = os.environ.get("TAKKUB_AUTO_MIGRATE")
    if raw is not None:
        return raw.strip() != "0"
    from . import core_v2_settings

    return core_v2_settings.flag_enabled("auto_migrate")


def is_dev_checkout() -> bool:
    """A from-source checkout (`config.DATA_HOME == config.REPO_ROOT`) is
    where this feature itself gets rehearsed by hand — never auto-run there."""
    return config.DATA_HOME == config.REPO_ROOT


def _state_path() -> Path:
    """#504 H8 (acceptance review): the pre-#504 spot
    (`SETTINGS_HOME/auto-migrate-state.json`) sits at DATA_HOME's TOP LEVEL
    on every installed (non-dev) machine (`SETTINGS_HOME == DATA_HOME`) —
    the very next `ArchiveV1LegacyStep` boot swept it up as an unrecognized
    V1 leftover the instant THIS module wrote it, so a machine that never
    had any V1 data still got a brand-new `v1-archive-<ts>` on its SECOND
    boot (reviewed `fresh_two_boots` repro). `storage_layout_v2().system` is
    a V2-owned directory `ArchiveV1LegacyStep` permanently excludes from
    archival — writing here instead retires this file as an archive
    candidate for good. Resolved lazily (not at import time): dev checkouts
    never reach this module's writers at all (`is_dev_checkout()` gates
    `run_boot_stage()` before either), but keeping the import local avoids
    a module-load-order dependency on `core.storage.layout` regardless."""
    from .core.storage.layout import storage_layout_v2

    return storage_layout_v2().system / _STATE_FILE


def _legacy_state_path() -> Path:
    """Where every machine that reached this state before the #504 H8 fix
    already has it — `load_state()` falls back to reading this so an
    in-flight `rolled_back_for_version`/`rolled_back_steps` guard isn't
    silently forgotten across the upgrade; `_save_state()` always writes the
    NEW location, so the very next successful save retires this path."""
    return config.SETTINGS_HOME / _STATE_FILE


def load_state() -> dict:
    """Missing/corrupt file reads as `{}` — fail-open, same contract as
    every other small state file in this codebase (`core_v2_settings.load`,
    `auto_issue_signals`'s flag file). Tries the current location first,
    then the pre-#504-H8-fix one (#504 H8) — never both at once, so a
    machine with a stale copy at the old spot doesn't have it silently
    override a freshly-written new one."""
    for path in (_state_path(), _legacy_state_path()):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _save_state(data: dict) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        config._write_json_atomic(path, data)
    except OSError:
        return  # swallow-ok: a failed save just means recomputing state next boot.


def _dir_size(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue  # swallow-ok: an unmeasurable file just doesn't add to the total.
    return total


def _estimate_copy_bytes(data_home: Path) -> int:
    """Best-effort size of the V1/2.0.x data the ladder is about to copy,
    dominated by `RUNTIME_DIR` (`runtime-triage`'s sessions/tasks/
    role-memory/knowledge — the design note's own "sessions อาจหลายร้อย MB"
    warning) and a pre-existing nested `v2/` root (#504 `PromoteV2RootStep`'s
    own source — the reviewed `disk_gate` finding: a populated `v2/` with NO
    `runtime/` at all used to estimate exactly zero bytes and pass any disk
    gate unconditionally). Every other ladder step's source is a handful of
    small flat JSON files that don't move this number. `StepReport.detail`
    carries no per-step byte accounting yet (`inspect()` reports
    presence/counts, not sizes), so this walks the two dominant sources
    directly rather than inventing a per-step size API just for this gate.
    #504 R2-H3 (round 2 acceptance review): the ponytail note above no
    longer holds — `ArchiveV1LegacyStep`'s own candidates (V1 top-level
    leftovers + H7's shared-dir legacy files) are counted explicitly below,
    since a fixture with ONLY an archive-only payload (no `runtime/`, no
    nested `v2/`) used to estimate exactly zero bytes and pass any disk
    gate unconditionally (`disk_archive_inventory`). Also counts the
    PRE-EXISTING size of any promote destination `_copy_phase` would
    merge into — that preimage gets backed up before the merge, so room
    must cover it too, not just the new copy (a populated `providers/`
    destination can be larger than the nested `v2/providers/` subtree being
    merged in)."""
    total = _dir_size(data_home / "runtime") if (data_home / "runtime").is_dir() else 0
    legacy_v2 = data_home / "v2"
    if legacy_v2.is_dir():
        total += _dir_size(legacy_v2)

    def _path_size(path: Path) -> int:
        try:
            if path.is_dir():
                return _dir_size(path)
            return path.stat().st_size
        except OSError:
            return 0

    try:
        from .core.migration.promote_v1 import ArchiveV1LegacyStep, PromoteV2RootStep

        promote = PromoteV2RootStep(data_home=data_home)
        for src in promote._promote_candidates():
            dest = data_home / src.name
            if dest.exists():
                total += _path_size(dest)  # preimage BackupManager backs up before merging

        archive = ArchiveV1LegacyStep(data_home=data_home)
        for src in archive._archive_candidates() + archive._shared_dir_legacy_candidates():
            total += _path_size(src)
    except OSError as e:
        # swallow-ok: read-only estimate; a failure here just means the
        # margin below is computed from a smaller `total` than the real
        # disk state, so it's logged (never a bare swallow) rather than
        # left silent.
        _log_boot_event("migration_disk_estimate_partial", error=str(e))

    # #574: `PreMigrateBackupStep` (ladder position 0) copies this SAME
    # `total` worth of content a second time — once into the backup, once
    # again as every domain step below promotes/archives it for real — plus
    # `projects/`/`agents/` (protected from `_archive_candidates()` above
    # since the domain steps below WRITE into them this same pass, but the
    # backup copies them too, as an extra safety net). Doubling `total`
    # rather than adding a third independent walk keeps this in sync with
    # whatever the promote/archive walk above already counts.
    extra_backup_only = 0
    for name in ("projects", "agents"):
        p = data_home / name
        if p.is_dir():
            extra_backup_only += _dir_size(p)
    total = total * 2 + extra_backup_only
    return total


# Public alias — `boot_flow.plan_migration()` (#574) reuses this exact
# estimate for consistency with the disk gate below, rather than a second,
# independently-drifting size computation.
estimate_copy_bytes = _estimate_copy_bytes


def _estimate_restore_bytes(data_home: Path) -> int:
    """Best-effort size of what `takkub migrate restore-v1` is about to
    copy back onto disk — #504 R3 `disk_cli_restore-v1`: after a machine has
    already fully promoted/archived, `_estimate_copy_bytes` above sees
    almost nothing left to promote or archive and would estimate ~0 bytes
    even though a real restore is about to copy the WHOLE `backups/` tree
    back onto `data_home`. Conservative on purpose (the whole archive tree,
    not just the generation(s) actually selected) — cheap to compute and
    never under-counts."""
    base = data_home / "backups"
    return _dir_size(base) if base.is_dir() else 0


def _live_preimage_bytes(data_home: Path) -> int:
    """#504 round4 R4-M1 `restore_preimage_space_estimate`: current
    on-disk size of every top-level name any archive generation could
    restore INTO — a restore backs this up BEFORE overwriting it
    (`BackupManager`, inside `_copy_phase`'s own merge), and a multi-
    generation `restore-v1` ALSO takes one command-level preimage
    snapshot of it (`_begin_command_snapshot`) before touching anything.
    Neither write is captured by `_estimate_restore_bytes` above (which
    only sizes the archive tree itself), so `_disk_has_room` adds this on
    top, unconditionally — even when a caller supplied its own
    `estimate`, since this cost is real and independent of that number."""
    try:
        from .core.migration.promote_v1 import list_v1_archives
    except OSError:
        return 0  # swallow-ok: read-only import failure — best-effort
        # extra margin; the base estimate below is unaffected.
    names: set[str] = set()
    for gen in list_v1_archives(data_home):
        names.update(n for n in gen.get("archived", []) if n)
    total = 0
    for name in names:
        target = data_home / name
        try:
            if target.is_dir():
                total += _dir_size(target)
            elif target.is_file():
                total += target.stat().st_size
        except OSError:
            continue  # swallow-ok: read-only size probe for one name;
            # an unmeasurable target just doesn't add to the margin.
    return total


def _existing_ancestor(path: Path) -> Path:
    """*path* itself, or its nearest EXISTING ancestor — `shutil.disk_usage`
    needs a real path to stat, but a brand-new machine's `DATA_HOME` (or a
    test's isolated one) legitimately doesn't exist yet on the very first
    boot; its eventual parent volume is the same either way."""
    p = path
    while not p.exists():
        parent = p.parent
        if parent == p:
            return p
        p = parent
    return p


def _disk_has_room(data_home: Path, *, estimate: int | None = None) -> bool:
    """False (gate fails, boot stage skips) both when there is genuinely
    not enough free space AND when free space can't be measured at all —
    an unmeasurable disk is not a safe one to bet a first-run copy on.

    *estimate*, when given, replaces the default copy-size estimate — the
    CLI's `restore-v1` gate (#504 R3 `disk_cli_restore-v1`) passes
    `_estimate_restore_bytes` instead, since a restore's dominant cost is
    the archive tree it copies back, not what a fresh promote/archive pass
    would move.

    #504 round4 R4-M1 `per_volume_preflight`: `data_home` isn't the only
    volume this pass writes to — every snapshot/backup/WAL file this
    module's own migration steps stage lives under `migration_home()`,
    which an operator can point at a DIFFERENT volume than `data_home`
    entirely (`TAKKUB_STORAGE_ROOT`-style overrides). Both are measured;
    either one failing its own check fails the whole gate, so a full
    snapshot volume fails closed even when `data_home`'s own drive has
    plenty of room.

    #504 round4 R4-M1 `restore_preimage_space_estimate`: on top of
    *estimate* (whatever it counts), this ALWAYS separately adds
    `_live_preimage_bytes` — the current on-disk size of every name an
    archive generation could restore into, which a real restore backs up
    (at least once, more for a multi-generation command) BEFORE
    overwriting it. A caller-supplied `estimate` (e.g. the CLI's
    `_estimate_restore_bytes`) was never meant to already include this,
    so it's never assumed to."""
    from .core.migration.promote_v1 import migration_home

    volumes = {_existing_ancestor(data_home)}
    try:
        volumes.add(_existing_ancestor(migration_home()))
    except OSError as e:
        # swallow-ok: read-only path resolution; if this can't even be
        # computed, the data_home-only check below still applies. Logged
        # (never a bare swallow) since a caller relying on this volume
        # actually being checked would otherwise never know it wasn't.
        _log_boot_event("migration_disk_gate_volume_unresolved", error=str(e))
    needed = estimate if estimate is not None else _estimate_copy_bytes(data_home)
    needed += _live_preimage_bytes(data_home)
    for volume in volumes:
        try:
            free = shutil.disk_usage(volume).free
        except OSError:
            return False
        if free < _MIN_FREE_MULTIPLE * needed:
            return False
    return True


def _log_boot_event(event: str, **details: object) -> None:
    """Best-effort; a logging failure must never fail the boot stage itself.
    Not `from .orchestrator import _log_event` (the re-export facade) —
    orchestrator.py transitively imports Qt widgets, and this module must
    stay importable (and callable) from a plain headless test with no
    QApplication running at all. `orchestrator_text` has zero Qt imports."""
    try:
        from .orchestrator_text import _log_event

        _log_event(event, **details)
    except Exception:
        return  # swallow-ok: this IS the fallback logging path itself — no
        # further sink to report its own failure to.


@dataclass
class BootMigrationResult:
    """What `run_boot_stage()` actually did, for callers that want to show
    or assert on it (`boot_update_window.py`'s splash; tests). `messages` is
    every progress line reported via `progress_cb`, in order — kept here too
    so a caller that didn't pass a callback can still inspect what happened."""

    action: str  # "skipped" | "applied" | "rolled_back" | "pending_applied" | "pending_rolled_back" | "cleanup_pending" | "error"
    reason: str = ""
    messages: list[str] = field(default_factory=list)
    # #574: the ladder's own step reports for this call — apply_reports (or
    # apply_pending's), validate_reports, or rollback_reports, whichever
    # this stage's own branch actually produced. Additive: every existing
    # caller (`boot_update_window.py`, tests) reads only `action`/`reason`/
    # `messages` and is unaffected by this staying empty when unset.
    reports: list[StepReport] = field(default_factory=list)
    # #574: the full-ladder `validate()` pass specifically (v1-state first
    # apply only — `apply_pending()` has no equivalent single pass, it
    # calls `validate()` per-step internally instead) — additive, empty
    # for every other action. Lets a caller report "step 7/11 ตรวจสอบ
    # ไม่ผ่าน" (screen E) without re-running validate() a second time.
    validate_reports: list[StepReport] = field(default_factory=list)


def _run_apply_pending(
    progress_cb: Callable[[str], None] | None,
    *,
    on_entry: Callable[[str, str], None] | None = None,
    on_file_progress: Callable[[str, str, int, int, str], None] | None = None,
    on_step: Callable[[str, str], None] | None = None,
    on_validate_step: Callable[[str, bool], None] | None = None,
) -> BootMigrationResult:
    """Every boot after a successful full apply (`layout_state() ==
    "mixed"`) — run only the ladder steps this machine still needs (#362):
    `version-marker`'s re-pin every version bump (closes the prod incident
    this stage was written to prevent — the marker sitting on an old
    version and `validate` complaining forever after every later upgrade),
    AND any step added to the ladder since this machine's last full apply
    (e.g. #360's `core-internal-store` landing on a machine that already
    migrated under the pre-#360 ladder). A step that already succeeded and
    still validates clean is left untouched — this is never a full re-walk
    of the ladder.

    Failure handling is per-step, not whole-ladder:
    - a step this machine has NEVER applied before (genuinely new) that
      fails apply() is rolled back on its own via
      `MigrationEngine.rollback_step()`, and held back by a
      (version, step_id) retry-guard so it isn't retried every boot — the
      OTHER pending steps in this same boot are unaffected.
    - a step this machine already applied successfully before, whose
      validate() now says it's broken, is NOT auto-rolled-back (that would
      throw away state that used to be fine) — just logged and recorded for
      `takkub doctor --storage-layout` to WARN about."""
    messages: list[str] = []

    def _report(msg: str) -> None:
        messages.append(msg)
        if progress_cb is not None:
            try:
                progress_cb(msg)
            except Exception:
                return  # swallow-ok: an observer failure must never affect migration.

    from . import __version__ as app_version
    from .core.migration.engine import MigrationEngine

    # #504 H4 (acceptance review): the "v2"/"mixed" branch used to jump
    # straight here without ever reaching the disk-space gate the "v1"
    # first-boot path already has (`run_boot_stage`'s own `_disk_has_room`
    # call sits further down, unreachable from this branch) — the reviewed
    # `disk_gate` finding showed `_disk_has_room` called ZERO times on this
    # path even with the check patched to always fail. `PromoteV2RootStep`
    # copies real data on exactly this path (a pre-existing nested `v2/`),
    # so it needs the same preflight the "v1" ladder gets.
    if not _disk_has_room(config.DATA_HOME):
        _report("พื้นที่ดิสก์ไม่พอสำหรับ pending migration step(s) — ข้ามรอบนี้")
        return BootMigrationResult("skipped", "disk-space", messages)

    _report("ตรวจ pending migration step(s)…")
    engine = MigrationEngine(
        on_entry=on_entry,
        on_file_progress=on_file_progress,
        on_step=on_step,
        on_validate_step=on_validate_step,
    )
    applied_before = set(engine.applied_step_ids())
    guard = load_state().get("rolled_back_steps", {})
    guarded_now = {step_id for step_id, ver in guard.items() if ver == app_version}

    reports = engine.apply_pending(skip_step_ids=guarded_now)
    if not reports:
        _report("ไม่มี step ที่ต้องรัน")
        return BootMigrationResult("pending_applied", messages=messages)

    # #504/#574 R8-M2 (round14b): `apply_pending()` itself now validates
    # every step it applies successfully, immediately after that step's own
    # apply, in ladder order — so `MigrationOutcome.validated_steps` is
    # never permanently 0 the way it was before this pass had any real
    # validate() call at all. `last_validate_reports` is read here rather
    # than calling `validate_ok_steps()` again: that separate call used to
    # fire `on_validate_step` for every domain step AFTER
    # `archive-v1-legacy`'s own copy phase had already run (this method
    # runs the whole ladder, `archive-v1-legacy` included, in one
    # per-step loop) — always too late for `boot_flow.py`'s phase-3 UI,
    # which had already advanced to phase 4 by then.
    validate_reports = engine.last_validate_reports

    stale = [r for r in reports if not r.ok and r.step_id in applied_before]
    new_failures = [r for r in reports if not r.ok and r.step_id not in applied_before]

    if stale:
        for r in stale:
            _log_boot_event(
                "auto_migrate_pending_step_stale", step_id=r.step_id, summary=r.summary[:200]
            )
            _report(
                f"'{r.step_id}' (apply สำเร็จมาก่อนหน้านี้) validate ไม่ผ่านตอนนี้ — "
                "ข้าม auto-rollback, ต้องตรวจด้วยมือ"
            )
        st = load_state()
        stale_map = dict(st.get("stale_applied_steps", {}))
        stale_map.update({r.step_id: r.summary[:200] for r in stale})
        st["stale_applied_steps"] = stale_map
        _save_state(st)

    if new_failures:
        # #574 round9 G11 `boot_completes_after_a_transient_prune_denial`:
        # a step whose OWN failure detail names `cleanup_pending` (`_prune_
        # phase`'s DUPLICATE/PRUNE_FAILED entry — every file this attempt
        # already copy-verified STAYS at its new home, only the one denied
        # source removal is still outstanding) is not a genuinely broken
        # step the way every OTHER `new_failures` entry is — it's a
        # transient environment blip (a locked file, a permission blip)
        # that a LATER boot, with the block lifted, can and must finish on
        # its own. Rolling it back (undoing already-safely-copied work) and
        # version-gating its retry (per the guard's whole design point:
        # never retry a REAL bug every boot) both actively work against
        # that self-heal — the WAL `apply_copy_only()`/`prune()` already
        # left behind IS the resume state, so this step is deliberately
        # left OUT of both the rollback loop and `rolled_back_steps` below,
        # unguarded, so the very next boot's `apply_pending()` retries it
        # unconditionally.
        cleanup_pending = [r for r in new_failures if r.detail.get("cleanup_pending")]
        hard_failures = [r for r in new_failures if not r.detail.get("cleanup_pending")]
        for r in cleanup_pending:
            _log_boot_event(
                "auto_migrate_cleanup_pending", step_id=r.step_id, summary=r.summary[:200]
            )
            _report(
                f"'{r.step_id}' ลบต้นทางไม่ได้ตอนนี้ (ข้อมูลปลอดภัย, คัดลอกไว้แล้ว) — จะลองใหม่ตอน boot ครั้งถัดไป"
            )
        for r in hard_failures:
            _report(f"'{r.step_id}' ไม่ผ่าน — กำลัง rollback เฉพาะ step นี้…")
            rb = engine.rollback_step(r.step_id)
            _log_boot_event(
                "auto_migrate_rolled_back",
                step_id=r.step_id,
                failing_summary=r.summary[:200],
                rollback_ok=rb.ok,
            )
            _report(f"rollback {r.step_id} " + ("สำเร็จ" if rb.ok else "ไม่สำเร็จ — ต้องตรวจด้วยมือ"))
        if hard_failures:
            st = load_state()
            guard_map = dict(st.get("rolled_back_steps", {}))
            guard_map.update({r.step_id: app_version for r in hard_failures})
            st["rolled_back_steps"] = guard_map
            _save_state(st)
        return BootMigrationResult(
            "pending_rolled_back" if hard_failures else "cleanup_pending",
            "; ".join(f"{r.step_id}: {r.summary}" for r in new_failures),
            messages,
            reports=reports,
            validate_reports=validate_reports,
        )

    _report("pending step(s) apply สำเร็จ")
    return BootMigrationResult(
        "pending_applied", messages=messages, reports=reports, validate_reports=validate_reports
    )


def run_boot_stage(
    progress_cb: Callable[[str], None] | None = None,
    *,
    on_entry: Callable[[str, str], None] | None = None,
    on_file_progress: Callable[[str, str, int, int, str], None] | None = None,
    on_step: Callable[[str, str], None] | None = None,
    on_validate_step: Callable[[str, bool], None] | None = None,
) -> BootMigrationResult:
    """The whole boot-time gate, in order (#361 design §2-4):

    1. escape hatches (env / Settings flag off, dev checkout) → skip
    2. `layout_state()` must be exactly `"v1"` to attempt a first full-ladder
       apply — `"v2"` has nothing left to do, `"mixed"` means a prior apply
       already ran at least once and only `apply_pending()` (#362) runs:
       never re-walk the whole ladder over a `"mixed"` machine
    3. retry-guard: a version that already rolled back once is never retried
       until the running app version changes
    4. disk-space gate (2x the estimated copy size)
    5. `apply()` → `validate()` → any `ok: false` anywhere triggers
       `rollback()` + `auto_migrate_rolled_back` + the retry-guard write;
       otherwise `auto_migrate_applied` + the applied-version write

    Pure Python, no Qt — callable from a plain test or from a headless boot
    (`TAKKUB_BOOT_UPDATE=0`, no splash at all) exactly the same way the
    splash-driven path calls it off a worker thread.
    """
    messages: list[str] = []

    def _report(msg: str) -> None:
        messages.append(msg)
        if progress_cb is not None:
            try:
                progress_cb(msg)
            except Exception:
                return  # swallow-ok: an observer failure must never affect migration.

    def _done(
        action: str,
        reason: str = "",
        reports: list[StepReport] = (),
        validate_reports: list[StepReport] = (),
    ) -> BootMigrationResult:
        return BootMigrationResult(
            action, reason, messages, reports=list(reports), validate_reports=list(validate_reports)
        )

    if not auto_migrate_enabled():
        return _done("skipped", "disabled")
    if is_dev_checkout():
        return _done("skipped", "dev-checkout")

    from . import __version__ as app_version
    from .core.storage.layout import layout_state

    state = layout_state()
    if state in ("v2", "mixed"):
        # #504: "v2" is now the normal steady state of every fully-migrated
        # machine (not the "unreachable in practice" case it was pre-#504 —
        # see `authority_state`'s own now-stale docstring claim), so it must
        # keep running `apply_pending()` exactly like "mixed" does, forever
        # — that's the only thing that ever picks up a ladder step added in
        # a LATER release after this machine already promoted (#362's whole
        # point), or self-heals a step whose validate() has since gone
        # stale (e.g. `role-agent`'s drift-repair). A bare `skip` here would
        # permanently strand every promoted machine on whatever ladder
        # existed the boot it first reached "v2" — each step's own
        # apply()/validate() being cheap existence-ish checks is what makes
        # calling this every boot fine even when truly nothing is pending.
        return _run_apply_pending(
            progress_cb,
            on_entry=on_entry,
            on_file_progress=on_file_progress,
            on_step=on_step,
            on_validate_step=on_validate_step,
        )

    # state == "v1" from here — the only state a first-run apply is allowed on.
    st = load_state()
    if st.get("rolled_back_for_version") == app_version:
        return _done("skipped", "previously-rolled-back")
    if not _disk_has_room(config.DATA_HOME):
        return _done("skipped", "disk-space")

    _report("กำลังตั้งค่า storage layout ใหม่ (ครั้งแรกหลังอัป)…")
    from .core.migration.engine import MigrationEngine

    engine = MigrationEngine(
        on_entry=on_entry,
        on_file_progress=on_file_progress,
        on_step=on_step,
        on_validate_step=on_validate_step,
    )
    apply_reports = engine.apply()
    validate_reports: list[StepReport] = []
    failing = next((r for r in apply_reports if not r.ok), None)
    if failing is None:
        _report("apply สำเร็จ — กำลัง validate…")
        validate_reports = engine.validate()
        failing = next((r for r in validate_reports if not r.ok), None)

    if failing is not None:
        _report(f"'{failing.step_id}' ไม่ผ่าน — กำลัง rollback อัตโนมัติ…")
        rollback_reports = engine.rollback()
        rollback_ok = all(r.ok for r in rollback_reports)
        _save_state({"rolled_back_for_version": app_version})
        _log_boot_event(
            "auto_migrate_rolled_back",
            failing_step=failing.step_id,
            failing_summary=failing.summary[:200],
            rollback_ok=rollback_ok,
        )
        _report("rollback " + ("สำเร็จ" if rollback_ok else "ไม่สำเร็จ — ต้องตรวจด้วยมือ"))
        return _done(
            "rolled_back",
            failing.summary,
            reports=rollback_reports,
            validate_reports=validate_reports,
        )

    _save_state({"applied_version": app_version})
    _log_boot_event("auto_migrate_applied", steps=len(apply_reports))
    _report("apply + validate สำเร็จ")
    return _done("applied", reports=apply_reports, validate_reports=validate_reports)


__all__ = [
    "BootMigrationResult",
    "auto_migrate_enabled",
    "is_dev_checkout",
    "load_state",
    "run_boot_stage",
]
