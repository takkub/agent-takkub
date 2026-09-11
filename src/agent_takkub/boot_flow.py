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
and phase 5 (done) have no per-item signal at all — their `ProgressEvent`s
carry `done=None, total=None` (indeterminate) and are driven off
`run_boot_stage`'s existing text messages. A fully granular byte-level
meter would need every domain step wired the same way `_copy_phase`/
`_prune_phase` now are — a larger change than #574's brief asked for.
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
    to migrate (`layout_state() == "v2"`) — screen B (and the whole rest of
    the migration UI) has nothing to show at that point."""
    from . import auto_migrate_boot as _amb
    from .core.migration.engine import MigrationEngine
    from .core.storage.layout import layout_state

    if layout_state() == "v2":
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
    totals = {
        1: max(1, len(plan.backup_items)) if plan else 1,
        2: max(1, len(plan.promote_items)) if plan else 1,
        4: max(1, len(plan.archive_items)) if plan else 1,
    }
    grand_total = sum(totals.values())
    state = {"phase": 1}

    def emit(
        phase: int,
        *,
        done: int | None = None,
        total: int | None = None,
        log_line: str = "",
        current_path: str | None = None,
    ) -> None:
        if progress_cb is None:
            return
        state["phase"] = phase
        overall_done = 0
        for p in (1, 2, 4):
            if p < phase:
                overall_done += totals[p]
            elif p == phase and done is not None:
                overall_done += min(done, totals[p])
        percent = 100.0 if phase >= 5 else min(99.0, 100.0 * overall_done / grand_total)
        try:
            progress_cb(
                ProgressEvent(
                    phase=phase,
                    phase_label=_PHASE_LABELS[phase],
                    phases_total=PHASES_TOTAL,
                    done=done,
                    total=total if total is not None else (totals.get(phase) if plan else None),
                    unit="รายการ",
                    percent_overall=percent,
                    eta_s=None,
                    log_line=log_line,
                    backup_dir=plan.backup_dir if plan else None,
                    current_path=current_path,
                )
            )
        except Exception:
            return  # swallow-ok: a progress-observer failure must never affect migration.

    if plan is None:
        result = auto_migrate_boot.run_boot_stage()
        return _outcome_from_result(result, plan, started)

    step_done: dict[str, int] = {}

    def on_entry(step_id: str, name: str) -> None:
        phase = _phase_of_step(step_id)
        if phase not in (1, 2, 4):
            return
        step_done[step_id] = step_done.get(step_id, 0) + 1
        emit(
            phase,
            done=step_done[step_id],
            total=totals[phase],
            log_line=f"{step_id}: {name}",
            current_path=name,
        )

    def on_text(msg: str) -> None:
        if "validate" in msg or "ตรวจสอบ" in msg:
            emit(3, log_line=msg)
        else:
            emit(state["phase"], log_line=msg)

    emit(1, done=0, total=totals[1], log_line="เริ่มย้ายข้อมูล")
    result = auto_migrate_boot.run_boot_stage(progress_cb=on_text, on_entry=on_entry)
    emit(5, log_line="เสร็จ" if result.action in ("applied", "pending_applied") else "จบการทำงาน")
    return _outcome_from_result(result, plan, started)


def _outcome_from_result(
    result, plan: MigrationPlanSummary | None, started: float
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
