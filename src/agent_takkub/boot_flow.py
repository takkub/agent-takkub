"""Boot flow (#574) — provider-update choice, pre-migration backup plan, and
migration progress/outcome, as one pure (no Qt) business-logic module the
Qt boot splash, a terminal renderer, and headless boot can all share.

Five pieces, matching the 5-screen mockup this backs (A provider-update
choice, B pre-migrate backup plan, C progress, D success, E failure):

1. `ProviderUpdateItem` / `check_provider_updates()` / `remembered_provider_
   choice()` / `remember_provider_choice()` / `run_provider_updates()` —
   screen A. Reuses `provider_update.py`'s eligibility/update mechanism
   verbatim; adds a best-effort latest-version PROBE on top (that module
   only ever updates blind, it never checked first).
2. `MigrationPlanSummary` / `plan_migration()` — screen B. Reuses the same
   ladder step objects (`core.migration.engine.MigrationEngine`) the real
   apply runs, so the plan can never drift from what apply actually does.
3. `ProgressEvent` / `run_migration()` — screen C. Wraps `auto_migrate_boot
   .run_boot_stage()` (never re-implements its skip/dispatch/retry-guard
   logic) and adds the `on_entry` per-entry observer (#574) `MigrationEngine`
   now accepts, so progress is real per-item movement, not a guess.
4. `MigrationOutcome` — screens D/E, built from `run_migration()`'s result.

Ponytail (documented shortcut, upgrade path): per-entry progress is only as
fine-grained as `on_entry` fires — one event per top-level item
`pre-migrate-backup`/`promote-v2-root`/`archive-v1-legacy` copy-verify (not
per underlying file within a directory entry, and not at all for the other,
simpler V1->V2 domain steps, which write in one shot). Phase 3 (validate)
gets one event per domain step instead, from that step's own real
`validate()` result (#574 round14) — still not a fine-grained WITHIN-step
counter (nothing to report mid-step, and no event at all for a step whose
validate() never actually ran this pass). Phase 5 (done) has no per-item
signal at all — its `ProgressEvent`s carry `done=None, total=None`
(indeterminate) and are driven off `run_boot_stage`'s existing text
messages. A fully granular byte-level meter would need every domain step
wired the same way `_copy_phase`/`_prune_phase` now are — a larger change
than #574's brief asked for.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from . import config

# ─────────────────────────────────────────────────────────────────────
# 1. Provider updates (screen A)
# ─────────────────────────────────────────────────────────────────────

PROVIDER_STATUS_UP_TO_DATE = "up_to_date"
PROVIDER_STATUS_UPDATE_AVAILABLE = "update_available"
PROVIDER_STATUS_NOT_INSTALLED = "not_installed"
PROVIDER_STATUS_DISABLED = "disabled"
PROVIDER_STATUS_NO_MECHANISM = "no_mechanism"
PROVIDER_STATUS_CHECKING = "checking"
PROVIDER_STATUS_FAILED = "failed"

_CHOICE_MODES = ("ask", "update_all", "skip", "selected")


@dataclass(frozen=True, slots=True)
class ProviderUpdateItem:
    name: str
    label: str
    current: str | None
    latest: str | None
    selected: bool
    status: str


def _label(spec) -> str:
    return spec.display_name or spec.name


def _current_version_generic(spec) -> str | None:
    """`<binary> --version` -> parsed version, best-effort — same shape as
    `claude_update.current_version()` but for any provider whose binary
    supports a bare `--version` flag (every `PROVIDER_REGISTRY` entry this
    module has actually tried it against does)."""
    from .claude_update import _run, parse_version
    from .provider_update import _discover

    binary = _discover(spec)
    if not binary:
        return None
    try:
        proc = _run([binary, "--version"], timeout=15.0)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return parse_version(proc.stdout or proc.stderr or "")


def _npm_view_version(pkg: str, timeout_s: float) -> tuple[bool, str | None]:
    from .claude_update import _run, parse_version

    npm = config.find_npm()
    if not npm:
        return False, None
    try:
        proc = _run(
            [npm, "view", pkg, "version", "--registry", config.npm_registry()], timeout=timeout_s
        )
    except Exception:
        return False, None
    if proc.returncode != 0:
        return False, None
    return True, parse_version(proc.stdout or "")


def _probe_versions(name: str, spec, timeout_s: float) -> tuple[str | None, str | None, bool]:
    """`(current, latest, probe_failed)` for *name* — *probe_failed* is
    True only when a latest-version check was actually ATTEMPTED and
    errored (network/timeout/non-zero exit), never merely "no known probe
    mechanism for this manager" (that's `latest=None, probe_failed=False`
    — see `check_provider_updates()`'s docstring for what that means)."""
    if name == "claude":
        from . import claude_update

        current = claude_update.current_version()
        ok, latest_or_err = claude_update.latest_version(timeout=timeout_s)
        return current, (latest_or_err if ok else None), not ok

    current = _current_version_generic(spec)
    cmd = spec.install_command
    if not cmd or cmd[0] != "npm":
        # uv-managed providers (kimi) and no-mechanism providers
        # (gemini/cursor) have no latest-version probe this module has
        # verified — never guess (#574 task brief).
        return current, None, False
    ok, latest = _npm_view_version(cmd[-1], timeout_s)
    return current, latest, not ok


def check_provider_updates(timeout_s: float = 20.0) -> list[ProviderUpdateItem]:
    """One row per `PROVIDER_REGISTRY` entry. A provider with no known
    latest-version probe (uv-managed, or genuinely no update mechanism at
    all) reports `latest=None` and status `up_to_date` — it is never
    flagged as needing an update it can't even check for, matching the
    boot-flow mockup (gemini/kimi both show "ล่าสุดแล้ว" despite neither
    having a version-check mechanism)."""
    from . import provider_update
    from .provider_spec import PROVIDER_REGISTRY

    items: list[ProviderUpdateItem] = []
    for name, spec in PROVIDER_REGISTRY.items():
        gap = provider_update.eligibility_gap(name)
        if gap is not None:
            status = {
                provider_update.STATUS_SKIPPED_NOT_INSTALLED: PROVIDER_STATUS_NOT_INSTALLED,
                provider_update.STATUS_SKIPPED_DISABLED: PROVIDER_STATUS_DISABLED,
            }.get(gap.status, PROVIDER_STATUS_NO_MECHANISM)
            items.append(ProviderUpdateItem(name, _label(spec), None, None, False, status))
            continue

        current, latest, probe_failed = _probe_versions(name, spec, timeout_s)
        if probe_failed:
            status = PROVIDER_STATUS_FAILED
        elif latest is None:
            status = PROVIDER_STATUS_UP_TO_DATE
        elif current is None:
            status = PROVIDER_STATUS_FAILED
        else:
            from .claude_update import compare_versions

            status = (
                PROVIDER_STATUS_UP_TO_DATE
                if compare_versions(current, latest) >= 0
                else PROVIDER_STATUS_UPDATE_AVAILABLE
            )
        items.append(
            ProviderUpdateItem(
                name,
                _label(spec),
                current,
                latest,
                status == PROVIDER_STATUS_UPDATE_AVAILABLE,
                status,
            )
        )
    return items


def _choice_path() -> Path:
    from .core.storage.layout import storage_layout_v2
    from .core.storage.v2_target import effective_data_home

    home = effective_data_home(None, prefer_primary=True)
    return storage_layout_v2(home).config_dir / "boot-provider-choice.json"


def remembered_provider_choice() -> dict | None:
    """``{"mode": "ask"|"update_all"|"skip"|"selected", "selected": [name,
    ...]}`` from the last time a user answered screen A with "remember this
    choice" checked, or `None` when nothing (valid) is on record — missing/
    corrupt/malformed reads as `None`, never raises."""
    try:
        data = json.loads(_choice_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("mode") not in _CHOICE_MODES:
        return None
    return data


def remember_provider_choice(choice: dict) -> None:
    if not isinstance(choice, dict) or choice.get("mode") not in _CHOICE_MODES:
        raise ValueError(f"invalid provider choice: {choice!r}")
    path = _choice_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        config._write_json_atomic(path, choice)
    except OSError:
        return  # swallow-ok: a failed remember just means asking again next boot.


_OUTCOME_STATUS_MAP = {
    "up_to_date": PROVIDER_STATUS_UP_TO_DATE,
    "updated": PROVIDER_STATUS_UP_TO_DATE,
    "failed": PROVIDER_STATUS_FAILED,
    "skipped_not_installed": PROVIDER_STATUS_NOT_INSTALLED,
    "skipped_disabled": PROVIDER_STATUS_DISABLED,
    "skipped_no_mechanism": PROVIDER_STATUS_NO_MECHANISM,
}


def run_provider_updates(
    items: list[ProviderUpdateItem],
    progress_cb: Callable[[ProviderUpdateItem], None] | None = None,
) -> list[ProviderUpdateItem]:
    """Run `provider_update.update_provider()` for every *selected* item,
    in order — an unselected item passes through unchanged. Mirrors
    `boot_update_window.py`'s own per-provider update loop, just without
    the Qt worker-thread plumbing around it."""
    from . import provider_update

    out: list[ProviderUpdateItem] = []
    for item in items:
        if not item.selected:
            out.append(item)
            continue
        outcome = provider_update.update_provider(item.name)
        status = _OUTCOME_STATUS_MAP.get(outcome.status, PROVIDER_STATUS_FAILED)
        current = item.latest if (outcome.status == "updated" and item.latest) else item.current
        updated = replace(item, status=status, current=current)
        out.append(updated)
        if progress_cb is not None:
            try:
                progress_cb(updated)
            except Exception:
                continue  # swallow-ok: an observer failure must never abort the update loop.
    return out


# ─────────────────────────────────────────────────────────────────────
# 2. Migration plan (screen B)
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MigrationPlanSummary:
    # (label, count, bytes, unit) — unit in {"ไฟล์", "โปรเจค", "รายการ"}.
    backup_items: list[tuple[str, int, int, str]]
    estimated_bytes: int
    free_bytes: int
    backup_dir: Path
    promote_items: list[str]
    archive_items: list[str]
    junk_items: list[str]
    # Real ladder length (`MigrationEngine.step_count()`) — NOT
    # `len(promote_items)` (that's only ONE step's own candidate count).
    # Matches what `MigrationOutcome.failed_step_total` reports once a
    # validate() pass actually runs, so the wizard's "step X/N" preview
    # never drifts from the real thing.
    verify_steps: int = 0


def _free_bytes(data_home: Path) -> int:
    import shutil as _shutil

    p = data_home
    while not p.exists():
        parent = p.parent
        if parent == p:
            break
        p = parent
    try:
        return _shutil.disk_usage(p).free
    except OSError:
        return 0


def _entry_bytes(entry) -> int:
    from . import auto_migrate_boot as _amb

    try:
        if entry.kind == "file":
            return entry.src.stat().st_size
        return _amb._dir_size(entry.src) if entry.src.is_dir() else 0
    except OSError:
        return 0


def _backup_item_row(entry) -> tuple[str, int, int, str]:
    """(label, count, bytes, unit) for one backup input entry — `entry.
    name == "projects"` counts distinct project directories (unit
    "โปรเจค"), `"runtime/core"` counts as generic items (unit "รายการ" —
    it groups disparate things: journal, backups, version marker), and
    everything else counts files (unit "ไฟล์"), matching the mockup's own
    per-row units."""
    size = _entry_bytes(entry)
    if entry.kind == "file":
        return (entry.name, 1, size, "ไฟล์")
    if entry.name == "projects":
        count = len({p.split("/", 1)[0] for p in entry.paths}) if entry.paths else 0
        return (entry.name, count, size, "โปรเจค")
    if entry.name == "runtime/core":
        return (entry.name, len(entry.paths), size, "รายการ")
    return (entry.name, len(entry.paths), size, "ไฟล์")


def plan_migration() -> MigrationPlanSummary | None:
    """`None` once this machine is fully on the V2 layout with nothing left
    to migrate (`layout_state() == "v2"`), OR on a dev checkout — screen B
    (and the whole rest of the migration UI) has nothing to show at that
    point.

    Dev-checkout bug (found 2026-09-12): `layout_state()` returns `"mixed"`
    forever on a dev checkout, never `"v2"` (`data_home / "v2"` IS its
    permanent, by-design nested root — see `storage_layout_v2`'s own
    docstring), so the `== "v2"` check alone never short-circuits there. The
    real physical-migration ladder was already correctly gated off for dev
    checkouts everywhere else (`auto_migrate_boot.run_boot_stage`,
    `is_dev_checkout()`) — clicking "migrate" on this screen was already a
    harmless no-op — but this function forgot the same gate, so every dev
    boot showed the "must migrate or close the program" wizard for nothing
    real to do."""
    from . import auto_migrate_boot as _amb
    from .core.migration.engine import MigrationEngine
    from .core.storage.layout import layout_state

    if _amb.is_dev_checkout() or layout_state() == "v2":
        return None

    data_home = config.DATA_HOME
    engine = MigrationEngine()
    backup_step = engine.get_step("pre-migrate-backup")
    promote_step = engine.get_step("promote-v2-root")
    archive_step = engine.get_step("archive-v1-legacy")

    promote_items = [p.name for p in promote_step._promote_candidates()]
    archive_items = [p.name for p in archive_step._archive_candidates()]
    archive_items += [
        p.relative_to(data_home).as_posix() for p in archive_step._shared_dir_legacy_candidates()
    ]
    junk_items = [p.name for p in archive_step._delete_candidates()]

    backup_entries = backup_step._input_entries()
    backup_items = [_backup_item_row(e) for e in backup_entries]

    return MigrationPlanSummary(
        backup_items=backup_items,
        estimated_bytes=_amb.estimate_copy_bytes(data_home),
        free_bytes=_free_bytes(data_home),
        backup_dir=backup_step._backup_dir(),
        promote_items=promote_items,
        archive_items=archive_items,
        junk_items=junk_items,
        verify_steps=engine.step_count(),
    )


# ─────────────────────────────────────────────────────────────────────
# 3 & 4. Progress + outcome (screens C/D/E)
# ─────────────────────────────────────────────────────────────────────

_PHASE_LABELS: dict[int, str] = {
    1: "สำรองข้อมูล",
    2: "คัดลอกขึ้นโครงใหม่",
    3: "ตรวจสอบ",
    4: "เก็บของเก่าเข้า archive",
    5: "เสร็จ",
}
PHASES_TOTAL = 5

# #574 round12 item 3: `MigrationEngine`'s own fixed ladder order (engine.py
# `__init__`'s `self._steps` construction) — needed here only to number a
# domain step's phase-3 "step X/verify_steps" position; duplicated as
# literals (not imported from the engine) the same way `engine.py` itself
# duplicates `promote-v2-root`/`archive-v1-legacy`'s ids rather than
# constructing a step just to read one attribute.
_LADDER_STEP_ORDER: tuple[str, ...] = (
    "pre-migrate-backup",
    "version-marker",
    "promote-v2-root",
    "readonly-registries",
    "role-agent",
    "capability",
    "project",
    "state",
    "credential-reference",
    "runtime-triage",
    "core-internal-store",
    "archive-v1-legacy",
)
# The 8 V1->V2 "domain" steps — everything in the ladder except backup/
# version-marker/promote/archive, which already have their own phase 1/2/4
# per-entry signal. These write in one shot with no per-file progress of
# their own (#574's ponytail note), so phase 3 only ever gets a start/done
# pair per step, never a fine-grained counter.
_DOMAIN_STEP_IDS = frozenset(_LADDER_STEP_ORDER) - {
    "pre-migrate-backup",
    "version-marker",
    "promote-v2-root",
    "archive-v1-legacy",
}


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    phase: int
    phase_label: str
    phases_total: int
    done: int | None
    total: int | None
    unit: str
    percent_overall: float
    eta_s: float | None
    log_line: str
    backup_dir: Path | None
    # Short path (relative to DATA_HOME) of the entry/file currently being
    # moved — the same `name` `on_entry(step_id, name)` was fired with.
    # `None` for every event with no single item behind it (phases 3/5,
    # and the very first phase-1 kickoff before any entry has fired yet).
    current_path: str | None = None
    # #574 round11 item 3: progress WITHIN one large directory entry's own
    # copy+verify (from `on_file_progress`, not `on_entry`) — `None` for
    # every event this doesn't apply to (a `file`-kind entry, one entry
    # small enough its own top-level `on_entry` fire is the only signal,
    # or any event with no single entry behind it at all). A consumer
    # must read both fields defensively (`getattr(event, "files_done",
    # None)`) — they were added after `ProgressEvent` first shipped.
    files_done: int | None = None
    files_total: int | None = None
    # #574 round12 (R3-M5): structured log parts — a UI must never parse
    # `log_line` itself (the mockup's own review found that fragile: "two
    # NBSPs measuring 14px", "parser requires double spaces"). `log_operation`
    # is a machine token (a step_id, or "validate"/"info" for a plain text
    # message); `current_path` above already carries the path part;
    # `log_detail` is the free-form human explanation. `log_line` stays as
    # the legacy combined string for the terminal renderer, which has no
    # need to re-split it. All three are `None`/`""` for a hand-built event
    # that predates them — read defensively, same as files_done/files_total.
    log_operation: str | None = None
    log_detail: str = ""
    log_timestamp: str | None = None


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    ok: bool
    duration_s: float
    promoted: list[str]
    archived: list[str]
    junk_deleted: int
    projects_count: int
    backup_dir: Path | None
    archive_dir: Path | None
    failed_phase: int | None
    failed_step: str | None
    error: str | None
    rolled_back: bool
    data_intact: bool
    log_paths: list[Path]
    validated_steps: int = 0
    failed_step_index: int | None = None
    failed_step_total: int | None = None
    # #574 round12 (R3-M6): the "app" component version.json held BEFORE
    # this run's own version-marker overwrite — screen D's downgrade note
    # ("takkub migrate restore-v1 puts back <previous_version>") and
    # screen E's "currently running <app_version>, migration to
    # <target> failed" both need it. `None` when unreadable/missing (a
    # from-scratch install with no prior version.json).
    previous_version: str | None = None


def _phase_of_step(step_id: str | None) -> int | None:
    if step_id is None:
        return None
    if step_id == "pre-migrate-backup":
        return 1
    if step_id == "archive-v1-legacy":
        return 4
    return 2  # version-marker + every V1->V2 domain step, promote-v2-root included


def _projects_count() -> int:
    from .core.storage.layout import storage_layout_v2

    layout = storage_layout_v2()
    if layout.projects_root.is_dir():
        return sum(1 for p in layout.projects_root.iterdir() if p.is_dir())
    try:
        # `config.DATA_HOME / "projects.json"`, computed here rather than
        # via `config.PROJECTS_JSON` — that module attribute is frozen at
        # import time against whatever DATA_HOME resolved to THEN, not
        # whatever it's been monkeypatched to since (a real correctness
        # gap for tests, and for any caller after a runtime DATA_HOME
        # override).
        data = json.loads((config.DATA_HOME / "projects.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if isinstance(data, dict):
        return len(data.get("projects", data))
    if isinstance(data, list):
        return len(data)
    return 0


def _log_plan_runtime_mismatch(*, phase: int, done: int, planned_total: int) -> None:
    """#574 round14 (R5-M5): a real plan/runtime count mismatch (`emit()`'s
    own R8-M4 growing-total branch) is worth keeping SOMEWHERE, just never
    in `ProgressEvent.log_detail` — that reaches the wizard's own
    user-facing log verbatim, and an English developer diagnostic there
    breaks every other line's finished Thai prose. Mirrors
    `auto_migrate_boot._log_boot_event`'s own reasoning for not importing
    `orchestrator._log_event` directly (Qt-heavy transitively; this module
    documents itself as pure/no-Qt) — best-effort, a logging failure must
    never affect the migration it's observing."""
    try:
        from .orchestrator_text import _log_event

        _log_event(
            "migration_plan_undercounted", phase=phase, done=done, planned_total=planned_total
        )
    except Exception:
        return  # swallow-ok: this IS the fallback logging path itself.


def _log_paths() -> list[Path]:
    from .core.migration.journal import MigrationJournal

    paths = [config.RUNTIME_DIR / "boot.log"]
    try:
        paths.append(MigrationJournal().store_path)
    except Exception:
        return paths  # swallow-ok: best-effort extra log path, boot.log alone is enough.
    return paths


def run_migration(
    progress_cb: Callable[[ProgressEvent], None] | None = None,
) -> MigrationOutcome:
    """Wraps `auto_migrate_boot.run_boot_stage()` — every skip/dispatch/
    retry-guard/disk-gate decision is still made there, unchanged; this
    only adds a `ProgressEvent` stream on top via the engine's `on_entry`
    observer plus `run_boot_stage`'s existing text messages (see module
    docstring's ponytail note for exactly how granular that is)."""
    from . import auto_migrate_boot

    started = time.monotonic()
    plan = plan_migration()
    previous_version = _read_previous_app_version()
    # #574 round14 (R5-M1): phase 1's counter must read the same FILE
    # total the approved mockup shows ("15,762 / 15,762 ไฟล์"), not a count
    # of top-level entries — a real backup can sit on "1 / 4" for minutes
    # while each of those 4 entries is itself thousands of files.
    # `plan.backup_items`' own per-row `count` field already IS that real
    # count (`_backup_item_row`'s own number, the same one screen B shows)
    # — reused here, and per-entry below, instead of a second count that
    # could drift from it.
    backup_item_files = {name: count for name, count, _, _ in plan.backup_items} if plan else {}
    totals = {
        1: max(1, sum(backup_item_files.values())) if plan else 1,
        2: max(1, len(plan.promote_items)) if plan else 1,
        4: max(1, len(plan.archive_items)) if plan else 1,
    }
    grand_total = sum(totals.values())
    # `phase`: the phase the last emitted event belongs to. `phase_started`/
    # `phase_events`: reset every time `phase` changes — #574 round12 item 4
    # (R3-M3)'s eta_s basis, "rate of the CURRENT phase", needs its own
    # clock/count per phase, never one running total since the whole
    # migration started (an early slow phase 1 must never poison phase 2's
    # own, unrelated rate).
    state = {"phase": 0, "phase_started": started, "phase_events": 0, "max_phase": 0}
    # #504/#574 R8-M1: the running high-water percent — `percent_overall`
    # must never decrease (see `emit()`'s own clamp below).
    max_percent = 0.0

    def emit(
        phase: int,
        *,
        done: int | None = None,
        total: int | None = None,
        log_line: str = "",
        current_path: str | None = None,
        files_done: int | None = None,
        files_total: int | None = None,
        unit: str = "รายการ",
        operation: str | None = None,
        detail: str = "",
    ) -> None:
        nonlocal grand_total, max_percent
        if progress_cb is None:
            return
        # #504/#574 R8-M1: hold to the highest phase ever reached — a late
        # notify for an earlier-phase step (`promote-v2-root`'s deferred
        # prune firing its own `on_entry` again AFTER `archive-v1-legacy`
        # has already advanced the stream to phase 4) must never walk the
        # reported phase backwards, and must never reset `phase_started`/
        # `phase_events` for a phase this stream has already finished.
        phase = max(phase, state["max_phase"])
        state["max_phase"] = phase
        if state["phase"] != phase:
            state["phase"] = phase
            state["phase_started"] = time.monotonic()
            state["phase_events"] = 0
        state["phase_events"] += 1
        total_for_phase = total if total is not None else (totals.get(phase) if plan else None)
        # #574 round12 item 2 (R6-B1/off-by-one): `promote-v2-root`/
        # `archive-v1-legacy` each fire `on_entry` TWICE per top-level entry
        # over a full apply — once from their own copy phase, once from
        # their deferred prune phase (`_copy_phase`/`_prune_phase` in
        # `promote_v1.py` both call the SAME bound `on_entry`) — while
        # `totals[phase]` only ever counted entries once. `on_entry` below
        # also dedupes by name so `done` climbs meaningfully instead of
        # sticking at `total` for the whole prune half.
        #
        # #504/#574 R8-M4: a real `done` that STILL exceeds the plan's own
        # estimate once past that dedup (`archive-v1-legacy` genuinely
        # processing 4 entries against a planned 3, say) is a real plan/
        # runtime mismatch, not a duplicate-notify artifact — silently
        # clamping it down to `total_for_phase` hid that gap behind a
        # counter permanently stuck at 100%. Grow the total to match
        # reality instead, never clamp it away quietly.
        #
        # #574 round14 (R5-M5): the mismatch itself used to be appended, in
        # English, straight into `detail` — reaching the wizard's own
        # user-facing log verbatim (finished Thai prose everywhere else).
        # The signal is still worth keeping, just not there: it goes to the
        # audit log instead, same as every other best-effort diagnostic
        # this module doesn't want to fail the migration over.
        if done is not None and total_for_phase and done > total_for_phase:
            _log_plan_runtime_mismatch(phase=phase, done=done, planned_total=total_for_phase)
            if phase in totals:
                grand_total += done - totals[phase]
                totals[phase] = done
            total_for_phase = done
        overall_done = 0
        for p in (1, 2, 4):
            if p < phase:
                overall_done += totals[p]
            elif p == phase and done is not None:
                overall_done += min(done, totals[p])
        percent = 100.0 if phase >= 5 else min(99.0, 100.0 * overall_done / grand_total)
        # #504/#574 R8-M1: never let `percent_overall` decrease either — a
        # late out-of-order event recomputed against the (now-clamped-
        # forward) `phase` above can still land below what was already
        # reported, since `done` for that stale call was never meant for
        # this later phase's own totals.
        percent = max(percent, max_percent)
        max_percent = percent
        # #574 round12 item 4 (R3-M3): eta from the CURRENT phase's own
        # done/elapsed rate, once there's enough signal to trust it (>= 2s
        # of this phase, or >= 5 events in it) — before that, an early
        # single event's rate is noise, so eta stays None rather than
        # reporting something wildly wrong.
        eta: float | None = None
        phase_elapsed = time.monotonic() - state["phase_started"]
        if (
            done is not None
            and total_for_phase
            and done > 0
            and (phase_elapsed >= 2.0 or state["phase_events"] >= 5)
        ):
            rate = done / phase_elapsed if phase_elapsed > 0 else None
            if rate:
                eta = max(0.0, (total_for_phase - done) / rate)
        try:
            progress_cb(
                ProgressEvent(
                    phase=phase,
                    phase_label=_PHASE_LABELS[phase],
                    phases_total=PHASES_TOTAL,
                    done=done,
                    total=total_for_phase,
                    unit=unit,
                    percent_overall=percent,
                    eta_s=eta,
                    log_line=log_line,
                    backup_dir=plan.backup_dir if plan else None,
                    current_path=current_path,
                    files_done=files_done,
                    files_total=files_total,
                    log_operation=operation,
                    log_detail=detail,
                    log_timestamp=time.strftime("%H:%M:%S"),
                )
            )
        except Exception:
            return  # swallow-ok: a progress-observer failure must never affect migration.

    if plan is None:
        result = auto_migrate_boot.run_boot_stage()
        return _outcome_from_result(result, plan, started, previous_version=previous_version)

    step_done: dict[str, int] = {}
    step_seen: dict[str, set[str]] = {}
    # #574 round14b: `files_done`/`files_total` climb across the WHOLE
    # step, never reset — `verify_copy.copy_verified()`'s own copy-phase
    # throttle and verify-phase throttle each legitimately count 1..N
    # independently for one entry (the verify pass restarting its own
    # count after the copy pass reached N is expected, round11 behavior),
    # and a step like `pre-migrate-backup` backs up several separate small
    # entries whose own raw counts each start back at 1 too (`models` the
    # whole directory, then `models/registry.json` as its own domain-target
    # entry). A caller forwarding the raw per-call numbers straight through
    # saw `files_done` fall mid-step every time either happened. `_last`
    # holds the last RAW (done, total) reported for this step; the instant
    # a lower `done` arrives (a genuine restart, never a caller bug), the
    # previous peak is banked into `_base` so the combined count for this
    # whole step only ever climbs.
    file_progress_last: dict[str, tuple[int, int]] = {}
    file_progress_base: dict[str, int] = {}

    def on_entry(step_id: str, name: str) -> None:
        phase = _phase_of_step(step_id)
        if phase not in (1, 2, 4):
            return
        # #574 round12 item 2: count each NAME once per step — `promote-
        # v2-root`/`archive-v1-legacy` notify the same entry name again
        # during their own deferred prune phase (see `emit()`'s own note
        # above); a repeat notify still refreshes the log line/current_path
        # (the entry is genuinely being worked on, just not NEW progress)
        # but must not advance `done` a second time.
        seen = step_seen.setdefault(step_id, set())
        if name not in seen:
            seen.add(name)
            # #574 round14 (R5-M1): phase 1 (backup) counts FILES, using
            # this entry's own real file count from the plan (falling back
            # to 1 for a name the plan never saw — a genuine plan/runtime
            # mismatch, same `emit()` growing-total branch as any other
            # phase). Phase 2/4 keep counting whole entries, one per name,
            # unchanged since R4-H1.
            step_done[step_id] = step_done.get(step_id, 0) + (
                backup_item_files.get(name, 1) if phase == 1 else 1
            )
        emit(
            phase,
            done=step_done.get(step_id, 0),
            total=totals[phase],
            log_line=f"{step_id}: {name}",
            current_path=name,
            # #504/#574 R4-H1 (round14 R5-M1 for phase 1): `unit` describes
            # what `done`/`total` THEMSELVES count, stable for the whole
            # phase — never the content-type of whichever entry currently
            # happens to be streaming through, which flipped the noun mid-
            # phase ("1/4 ไฟล์" -> "2/4 โปรเจค") with no change in what the
            # number meant. Phase 1 counts files ("ไฟล์"); phase 2/4 count
            # entries (`emit()`'s own "รายการ" default, left unset here).
            unit="ไฟล์" if phase == 1 else "รายการ",
            operation=step_id,
        )

    def on_text(msg: str) -> None:
        if "validate" in msg or "ตรวจสอบ" in msg:
            emit(3, log_line=msg, operation="validate", detail=msg)
        else:
            emit(state["phase"], log_line=msg, operation="info", detail=msg)

    def on_file_progress(
        step_id: str, name: str, files_done: int, files_total: int, current_path: str
    ) -> None:
        # #574 round11 item 3: progress WITHIN one entry's own copy+verify
        # — `step_done[step_id]` (whole-entry count) stays whatever
        # `on_entry` last set it to; this only refines `current_path`/
        # `files_done`/`files_total` for the SAME phase/step in between
        # `on_entry` fires, so a large directory entry never goes silent.
        phase = _phase_of_step(step_id)
        if phase not in (1, 2, 4):
            return
        last_done, last_total = file_progress_last.get(step_id, (0, 0))
        if files_done < last_done:
            file_progress_base[step_id] = file_progress_base.get(step_id, 0) + last_total
        file_progress_last[step_id] = (files_done, files_total)
        base = file_progress_base.get(step_id, 0)
        emit(
            phase,
            done=step_done.get(step_id, 0),
            total=totals[phase],
            log_line=f"{step_id}: {name} ({files_done}/{files_total})",
            current_path=f"{name}/{current_path}",
            files_done=base + files_done,
            files_total=base + files_total,
            unit="ไฟล์" if phase == 1 else "รายการ",
            operation=step_id,
            detail=f"{files_done}/{files_total}",
        )

    def on_validate_step(step_id: str, ok: bool) -> None:
        # #574 round14 (R5-M3, and the remaining half of R8-M2): the 8
        # domain steps (readonly-registries, role-agent, capability,
        # project, state, credential-reference, runtime-triage, core-
        # internal-store) write in one shot with no `on_entry`/
        # `on_file_progress` of their own — phase 3's row/log must come
        # from each one's own REAL `validate()` result, fired by
        # `MigrationEngine` AFTER that step's apply (`on_validate_step`,
        # `validate()`/`validate_ok_steps()`), never from `on_step`
        # (apply-time only — R8-M2's original finding: labeling that
        # "ตรวจสอบแล้ว"/"กำลังตรวจสอบ" claimed a verification that, on the
        # `apply_pending()` path, might never have run at all). Positioned
        # by the step's FIXED ladder index (`_LADDER_STEP_ORDER`) out of
        # the real ladder length (`plan.verify_steps`) — the same
        # "step X/N" scheme `MigrationOutcome.failed_step_index/_total`
        # already uses. A step whose validate() genuinely failed is
        # labeled truthfully, never "ตรวจสอบแล้ว" — the row and its own log
        # line can never disagree about what happened, unlike before.
        if step_id not in _DOMAIN_STEP_IDS:
            return
        try:
            position = _LADDER_STEP_ORDER.index(step_id) + 1
        except ValueError:
            return
        verify_total = plan.verify_steps or len(_LADDER_STEP_ORDER)
        detail = "ตรวจสอบแล้ว" if ok else "ตรวจสอบไม่ผ่าน"
        emit(
            3,
            done=position,
            total=verify_total,
            log_line=f"{step_id}: {detail}",
            unit="ขั้น",
            operation=step_id,
            detail=detail,
        )

    _start_msg = "เริ่มย้ายข้อมูล"
    emit(
        1,
        done=0,
        total=totals[1],
        log_line=_start_msg,
        unit="ไฟล์",
        operation="info",
        detail=_start_msg,
    )
    result = auto_migrate_boot.run_boot_stage(
        progress_cb=on_text,
        on_entry=on_entry,
        on_file_progress=on_file_progress,
        on_validate_step=on_validate_step,
    )
    # #504/#574 R4-H2: the interface contract (docs/v2/574-boot-flow-
    # interface.md "log structure") requires `log_detail` to carry the
    # full message text for every "info"/"validate" event — these two
    # bracket the whole migration and used to leave it empty, so a window
    # reading the structured fields (not re-parsing `log_line`) rendered
    # only "HH:MM:SS  info" with the actual message lost.
    _end_msg = "เสร็จ" if result.action in ("applied", "pending_applied") else "จบการทำงาน"
    emit(5, log_line=_end_msg, operation="info", detail=_end_msg)
    return _outcome_from_result(result, plan, started, previous_version=previous_version)


def _read_previous_app_version() -> str | None:
    """The "app" component `version.json` held BEFORE this call — read
    BEFORE `run_boot_stage()` runs (its own `VersionMarkerStep.apply()`
    overwrites this same file with the running build's version), so this
    is genuinely the version a `restore-v1` on this run would put back
    (#574 R3-M6). `None` on a from-scratch install with no prior
    version.json, or any read failure — `read_version_doc` already
    fails open to `[]`."""
    from .core.versioning.store import read_version_doc, version_doc_path

    components = {c.component: c.version for c in read_version_doc(version_doc_path())}
    return components.get("app")


def _outcome_from_result(
    result,
    plan: MigrationPlanSummary | None,
    started: float,
    *,
    previous_version: str | None = None,
) -> MigrationOutcome:
    duration = time.monotonic() - started
    failing = next((r for r in result.reports if not r.ok), None)
    # #574 round6 R6-H1: `auto_migrate_boot` deliberately keeps reporting
    # `action="pending_applied"` for a step that was already applied before
    # and whose re-attempt failed again ("stale" — never auto-rolled-back,
    # by design; see `test_stale_applied_step_does_not_roll_back_and_is_
    # recorded_for_doctor`). That is the right call for `auto_migrate_boot`
    # itself, but a caller showing this to a person must not repeat it as
    # an unqualified success — `result.reports` (added alongside this fix)
    # carries the real per-step verdict, so `ok` here also requires no
    # failing report, not just a success-shaped `action` string. This is
    # what makes 3 consecutive boots after such a failure converge on one
    # stable, honest `ok=False` + `failed_step` every time, never a false
    # "pending_applied" success with nothing to point at.
    ok = result.action in ("applied", "pending_applied", "skipped") and failing is None
    rolled_back = result.action in ("rolled_back", "pending_rolled_back")
    if ok:
        data_intact = True
    elif rolled_back:
        data_intact = "ไม่สำเร็จ" not in " ".join(result.messages)
    else:
        # A per-step (not whole-ladder) failure — `_copy_phase`'s own
        # contract guarantees the source is left fully intact on any
        # failure it reports (undoes every dest it touched this attempt),
        # so a step-level ok=False alone never means data was lost, only
        # that nothing new got copied yet.
        data_intact = True

    failed_step = failing.step_id if failing else None
    failed_phase = _phase_of_step(failed_step) if (failing is not None) else None

    # #574: "step 7/11 ตรวจสอบไม่ผ่าน" (screen E) is specifically about the
    # VALIDATE pass's own step position — only populated for the full
    # first-time apply (`run_boot_stage`'s v1 branch; `apply_pending()` has
    # no single validate() pass to report a position within).
    validate_reports = getattr(result, "validate_reports", [])
    validated_steps = sum(1 for r in validate_reports if r.ok)
    failed_step_index: int | None = None
    failed_step_total: int | None = len(validate_reports) or None
    for i, r in enumerate(validate_reports, start=1):
        if not r.ok:
            failed_step_index = i
            failed_step = r.step_id
            failed_phase = _phase_of_step(failed_step)
            break

    archive_report = next((r for r in result.reports if r.step_id == "archive-v1-legacy"), None)
    archive_dir = None
    if archive_report is not None and archive_report.detail.get("archive_root"):
        archive_dir = Path(archive_report.detail["archive_root"])

    return MigrationOutcome(
        ok=ok,
        duration_s=duration,
        promoted=list(plan.promote_items) if plan else [],
        archived=list(plan.archive_items) if plan else [],
        junk_deleted=len(plan.junk_items) if (plan is not None and ok) else 0,
        projects_count=_projects_count(),
        backup_dir=plan.backup_dir if plan else None,
        archive_dir=archive_dir,
        failed_phase=failed_phase,
        failed_step=failed_step,
        error=None if ok else (failing.summary if failing else (result.reason or None)),
        rolled_back=rolled_back,
        data_intact=data_intact,
        log_paths=_log_paths(),
        validated_steps=validated_steps,
        failed_step_index=failed_step_index,
        failed_step_total=failed_step_total,
        previous_version=previous_version,
    )


__all__ = [
    "PHASES_TOTAL",
    "PROVIDER_STATUS_CHECKING",
    "PROVIDER_STATUS_DISABLED",
    "PROVIDER_STATUS_FAILED",
    "PROVIDER_STATUS_NOT_INSTALLED",
    "PROVIDER_STATUS_NO_MECHANISM",
    "PROVIDER_STATUS_UPDATE_AVAILABLE",
    "PROVIDER_STATUS_UP_TO_DATE",
    "MigrationOutcome",
    "MigrationPlanSummary",
    "ProgressEvent",
    "ProviderUpdateItem",
    "check_provider_updates",
    "plan_migration",
    "remember_provider_choice",
    "remembered_provider_choice",
    "run_migration",
    "run_provider_updates",
]
