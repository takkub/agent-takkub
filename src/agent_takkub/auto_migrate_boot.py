"""Auto `migrate apply` at boot (#361) — every device that boots 1.1.0+
lands on the same storage layout (`mixed`) without anyone typing `takkub
migrate apply` themselves, gated behind the same pre-flight checks a human
running the CLI manually would be told to do (`docs/v2/2.0.0-migration-plan.md`
§2.3).

Reuses `MigrationEngine` exactly as `takkub migrate` does — no second ladder.
The whole point of running this at boot rather than leaving it to a human is
that boot is the one moment nothing else could be writing the files the
ladder reads/copies (no pane exists yet) — the equivalent of §2.3's "close
the cockpit first" instruction, done automatically. Callers (`app.py` /
`boot_update_window.py`) MUST finish this stage before constructing
MainWindow — a pane spawned mid-copy writing into RUNTIME_DIR/SETTINGS_HOME
would look like corruption to `validate()` and trigger a false rollback.

State kept at ``SETTINGS_HOME/auto-migrate-state.json``:
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

_STATE_FILE = "auto-migrate-state.json"

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
    return config.SETTINGS_HOME / _STATE_FILE


def load_state() -> dict:
    """Missing/corrupt file reads as `{}` — fail-open, same contract as
    every other small state file in this codebase (`core_v2_settings.load`,
    `auto_issue_signals`'s flag file)."""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(data: dict) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        config._write_json_atomic(path, data)
    except OSError:
        pass


def _estimate_copy_bytes(data_home: Path) -> int:
    """Best-effort size of the V1 data the ladder is about to copy under
    `v2/`, dominated by `RUNTIME_DIR` (`runtime-triage`'s sessions/tasks/
    role-memory/knowledge — the design note's own "sessions อาจหลายร้อย MB"
    warning); every other step's source is a handful of small flat JSON
    files that don't move this number. `StepReport.detail` carries no
    per-step byte accounting yet (`inspect()` reports presence/counts, not
    sizes), so this walks the one dominant source directly rather than
    inventing a per-step size API just for this gate. ponytail: if a future
    ladder step's source ever outgrows RUNTIME_DIR, widen this — nothing
    here assumes RUNTIME_DIR is the *only* source, just the biggest one
    worth gating disk space on.
    """
    total = 0
    runtime_dir = data_home / "runtime"
    if not runtime_dir.is_dir():
        return 0
    for root, _dirs, files in os.walk(runtime_dir):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _disk_has_room(data_home: Path) -> bool:
    """False (gate fails, boot stage skips) both when there is genuinely
    not enough free space AND when free space can't be measured at all —
    an unmeasurable disk is not a safe one to bet a first-run copy on."""
    try:
        free = shutil.disk_usage(data_home).free
    except OSError:
        return False
    return free >= _MIN_FREE_MULTIPLE * _estimate_copy_bytes(data_home)


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
        pass


@dataclass
class BootMigrationResult:
    """What `run_boot_stage()` actually did, for callers that want to show
    or assert on it (`boot_update_window.py`'s splash; tests). `messages` is
    every progress line reported via `progress_cb`, in order — kept here too
    so a caller that didn't pass a callback can still inspect what happened."""

    action: str  # "skipped" | "applied" | "rolled_back" | "pending_applied" | "pending_rolled_back" | "error"
    reason: str = ""
    messages: list[str] = field(default_factory=list)


def _run_apply_pending(progress_cb: Callable[[str], None] | None) -> BootMigrationResult:
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
                pass

    from . import __version__ as app_version
    from .core.migration.engine import MigrationEngine

    _report("ตรวจ pending migration step(s)…")
    engine = MigrationEngine()
    applied_before = set(engine.applied_step_ids())
    guard = load_state().get("rolled_back_steps", {})
    guarded_now = {step_id for step_id, ver in guard.items() if ver == app_version}

    reports = engine.apply_pending(skip_step_ids=guarded_now)
    if not reports:
        _report("ไม่มี step ที่ต้องรัน")
        return BootMigrationResult("pending_applied", messages=messages)

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
        for r in new_failures:
            _report(f"'{r.step_id}' ไม่ผ่าน — กำลัง rollback เฉพาะ step นี้…")
            rb = engine.rollback_step(r.step_id)
            _log_boot_event(
                "auto_migrate_rolled_back",
                step_id=r.step_id,
                failing_summary=r.summary[:200],
                rollback_ok=rb.ok,
            )
            _report(f"rollback {r.step_id} " + ("สำเร็จ" if rb.ok else "ไม่สำเร็จ — ต้องตรวจด้วยมือ"))
        st = load_state()
        guard_map = dict(st.get("rolled_back_steps", {}))
        guard_map.update({r.step_id: app_version for r in new_failures})
        st["rolled_back_steps"] = guard_map
        _save_state(st)
        return BootMigrationResult(
            "pending_rolled_back",
            "; ".join(f"{r.step_id}: {r.summary}" for r in new_failures),
            messages,
        )

    _report("pending step(s) apply สำเร็จ")
    return BootMigrationResult("pending_applied", messages=messages)


def run_boot_stage(
    progress_cb: Callable[[str], None] | None = None,
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
                pass

    def _done(action: str, reason: str = "") -> BootMigrationResult:
        return BootMigrationResult(action, reason, messages)

    if not auto_migrate_enabled():
        return _done("skipped", "disabled")
    if is_dev_checkout():
        return _done("skipped", "dev-checkout")

    from . import __version__ as app_version
    from .core.storage.layout import layout_state

    state = layout_state()
    if state == "v2":
        return _done("skipped", "layout-state-v2")
    if state == "mixed":
        return _run_apply_pending(progress_cb)

    # state == "v1" from here — the only state a first-run apply is allowed on.
    st = load_state()
    if st.get("rolled_back_for_version") == app_version:
        return _done("skipped", "previously-rolled-back")
    if not _disk_has_room(config.DATA_HOME):
        return _done("skipped", "disk-space")

    _report("กำลังตั้งค่า storage layout ใหม่ (ครั้งแรกหลังอัป)…")
    from .core.migration.engine import MigrationEngine

    engine = MigrationEngine()
    apply_reports = engine.apply()
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
        return _done("rolled_back", failing.summary)

    _save_state({"applied_version": app_version})
    _log_boot_event("auto_migrate_applied", steps=len(apply_reports))
    _report("apply + validate สำเร็จ")
    return _done("applied")


__all__ = [
    "BootMigrationResult",
    "auto_migrate_enabled",
    "is_dev_checkout",
    "load_state",
    "run_boot_stage",
]
