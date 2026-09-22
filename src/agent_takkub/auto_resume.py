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

# Same budget shape for the #514 quota reroute: how many times ONE task may
# be moved to another provider before auto-resume stops and hands the
# decision to Lead. Reset on every fresh assign(). #699: the Lead reroute
# looped 300+ rounds in 25 minutes on prod (close ignored → spawn "already
# running" → takeover brief pasted into the same quota-hit pane every 5 s
# tick) — a cap turns any such regression into a bounded, visible stop.
MAX_REROUTE_ROUNDS = 3

# If a pane hits the limit again this soon after being woken, the fresh
# window is already exhausted too (or the task itself is pathological) —
# stop retrying immediately instead of parking again.
RELIMIT_GRACE_S = 10 * 60

# Extra delay past the banner's advertised reset time before waking — usage
# windows sometimes lag a few seconds/minutes past what the banner claims.
WAKE_BUFFER_S = 3 * 60

# #663: while a provider is recorded quota-hit, ask it (usage/rate-limit
# probe, no model turn) this often whether it has actually recovered instead
# of trusting the banner's "resets in Xh" until it expires — the banner is a
# provider estimate, not a contract. Field case 2026-09-17: codex was usable
# again mid-window but every new assign kept substituting claude all day.
QUOTA_REPROBE_INTERVAL_S = 20 * 60
# Utilization (percent) at or above which a probe still counts as exhausted.
QUOTA_REPROBE_EXHAUSTED_PERCENT = 95.0

# Signal (b): the profile's own limit_status telemetry must independently
# confirm the five-hour window is (near-)exhausted before we park. Guards
# against a false-positive banner match parking a pane that can still work.
CONFIRM_UTILIZATION_PCT = 95.0

# #595: how often `_maybe_auto_resume_park` is allowed to fire a fresh
# signal-(b) confirm fetch for the SAME episode — without this it re-fires
# on every IDLE_WATCHDOG_INTERVAL_MS tick (5s) regardless of whether the
# previous fetch even finished, spawning a new thread every tick.
CONFIRM_RETRY_INTERVAL_S = 30

# #595: how long the confirm loop may retry before giving up on signal (b)
# and rerouting on signal (a) (the banner) alone — real incident:
# `pane_limit_confirm_failed` fired every ~5s for 2h51m+ (1637 events) with
# zero reroute, because the profile's own usage telemetry never independently
# confirmed a banner that was, per the CLI's own text, real. A pane genuinely
# frozen on quota deserves to move to another provider well before 5 hours
# pass in silence; a handful of confirm attempts (CONFIRM_RETRY_INTERVAL_S
# apart) is enough to rule out a one-off fetch/network hiccup without
# guessing forever.
CONFIRM_FALLBACK_TIMEOUT_S = 3 * 60

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
