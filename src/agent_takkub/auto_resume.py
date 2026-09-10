"""Auto-resume (🌙) — park panes that hit Claude's usage limit and wake them
automatically when the window resets, instead of just notifying the Lead.

#515 Settings diet: this used to be a status-bar toggle persisted to
``autoresume.json``, defaulting OFF. #514 replaced "park and wait for the
same provider" with "reroute to another provider immediately" as the only
correct behaviour, which made "notify-only" no longer a real alternative
worth a toggle — so `current()`/`is_enabled()` below are now always True,
with no on/off store left to keep in sync. The tuning constants below and
the actual detection/park/wake (fallback path when there is no other
provider to reroute to) logic live in the `AutoResumeMixin`
(`limit_autoresume.py`) mixed into `Orchestrator`.
"""

from __future__ import annotations

import json
from pathlib import Path

# How many park→wake cycles are allowed per pane for its CURRENT assigned
# task before giving up and leaving it to the Lead. Reset whenever a fresh
# task is assign()ed. Guards against silently burning quota on a task that
# keeps re-hitting the limit for reasons unrelated to a normal usage window.
MAX_PARK_ROUNDS = 3

# If a pane hits the limit again this soon after being woken, the fresh
# window is already exhausted too (or the task itself is pathological) —
# stop retrying immediately instead of parking again.
RELIMIT_GRACE_S = 10 * 60

# Extra delay past the banner's advertised reset time before waking — usage
# windows sometimes lag a few seconds/minutes past what the banner claims.
WAKE_BUFFER_S = 3 * 60

# Signal (b): the profile's own limit_status telemetry must independently
# confirm the five-hour window is (near-)exhausted before we park. Guards
# against a false-positive banner match parking a pane that can still work.
CONFIRM_UTILIZATION_PCT = 95.0

# Give-up status dump (#158): how many trailing non-blank lines of the pane's
# visible screen to echo in the give-up notice, so the Lead can tell at a
# glance whether the task actually finished before the pane went quiet.
GIVE_UP_TAIL_LINES = 12

# How many leading characters of the assigned task text to preview in the
# give-up notice — enough to identify the task without dumping the whole
# spec into chat (the full text always survives in the progress-marker file
# and in PaneState.last_assigned_task / last_assigned_task_file).
GIVE_UP_TASK_PREVIEW_CHARS = 220


def _archive_legacy_file() -> None:
    """One-time cleanup of the pre-#515 ``autoresume.json`` toggle state —
    its value is meaningless now (`is_enabled()` is always True), so there is
    nothing to carry forward, just the file itself to keep around per the
    "never delete, archive" rule."""
    from . import config

    config.archive_settings_file(config.SETTINGS_HOME / "autoresume.json")


def current() -> bool:
    """Return whether auto-resume is enabled. Always True — see module docstring."""
    _archive_legacy_file()
    return True


def is_enabled() -> bool:
    """Alias for `current()` — reads more naturally at call sites."""
    return current()


# ── park-as-last-resort toggle (#514) ───────────────────────────────────────
# Reroute (see limit_autoresume.AutoResumeMixin._reroute_or_park) is always
# tried first and is not itself a toggle (see module docstring). This is a
# narrower knob: whether the OLD park-and-wait behaviour may still fire when
# reroute finds literally no other provider available right now. Defaults ON
# (park stays a safety net) — a team that would rather a stranded pane give
# up and hand the task back to Lead immediately, never silently sitting idle
# for hours, can turn this off in Settings. Resolved lazily (function, not a
# module-level constant) to keep this module import-light and so tests can
# monkeypatch `config.SETTINGS_HOME` freely.
def _park_fallback_path() -> Path:
    from .config import SETTINGS_HOME

    return SETTINGS_HOME / "park-fallback.json"


def park_fallback_enabled() -> bool:
    """True unless the user has explicitly turned the park-as-last-resort
    fallback off in Settings. Missing/corrupt file -> True (safe default)."""
    path = _park_fallback_path()
    if not path.exists():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(data, dict):
        return True
    return bool(data.get("enabled", True))


def set_park_fallback_enabled(flag: bool) -> None:
    """Persist the park-as-last-resort toggle."""
    path = _park_fallback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"enabled": bool(flag)}, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
