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
  ``{"applied_version": "1.1.0"}``           — full ladder already ok once;
                                                every later boot only re-runs
                                                step 1 (version-marker).
  ``{"rolled_back_for_version": "1.1.0"}``   — that version's attempt failed
                                                validate() and was rolled
                                                back; never retried until the
                                                running app version changes
                                                (no retry-loop on a bad box).
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

    action: str  # "skipped" | "applied" | "rolled_back" | "step1_only" | "error"
    reason: str = ""
    messages: list[str] = field(default_factory=list)


def _run_step1_only(progress_cb: Callable[[str], None] | None) -> BootMigrationResult:
    """Every boot after a successful full apply (`layout_state() ==
    "mixed"`) — re-pin `system/version.json` to the running build via the
    SAME ladder's own step 0, never a fresh `VersionMarkerStep`. Closes the
    prod incident this task was written to prevent: the marker sitting on
    an old version (1.0.86) and `validate` complaining about it forever
    after every later upgrade."""
    messages: list[str] = []

    def _report(msg: str) -> None:
        messages.append(msg)
        if progress_cb is not None:
            try:
                progress_cb(msg)
            except Exception:
                pass

    _report("ตรวจ version marker…")
    from .core.migration.engine import MigrationEngine

    result = MigrationEngine().apply_version_marker_only()
    if not result.ok:
        return BootMigrationResult("error", result.summary, messages)
    return BootMigrationResult("step1_only", messages=messages)


def run_boot_stage(
    progress_cb: Callable[[str], None] | None = None,
) -> BootMigrationResult:
    """The whole boot-time gate, in order (#361 design §2-4):

    1. escape hatches (env / Settings flag off, dev checkout) → skip
    2. `layout_state()` must be exactly `"v1"` to attempt a first apply —
       `"v2"` has nothing left to do, `"mixed"` means a prior apply already
       ran (step-1-only fast path), never re-apply over a `"mixed"` machine
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
        return _run_step1_only(progress_cb)

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
