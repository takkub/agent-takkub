"""Auto-resume (🌙) — park panes that hit Claude's usage limit and wake them
automatically when the window resets, instead of just notifying the Lead.

Toggled from the status bar and persisted across restart (same pattern as
`exec_mode.py` / `provider_state.py`).  This module only stores the on/off
*intent* plus the tuning constants; the actual detection/park/wake logic
lives in the `AutoResumeMixin` (`limit_autoresume.py`) mixed into
`Orchestrator`.

State file: ``~/.takkub/autoresume.json``  Format: ``{"enabled": true|false}``.
Missing / corrupt → OFF (a fresh install must never auto-inject into a
teammate pane the user hasn't opted into).
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import SETTINGS_HOME

_DEFAULT = False

_PATH = SETTINGS_HOME / "autoresume.json"

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


def path() -> Path:
    """Where state lives. Function form so tests can monkeypatch `_PATH`."""
    return _PATH


def current() -> bool:
    """Return whether auto-resume is enabled. Missing file/corrupt JSON → False."""
    if not _PATH.exists():
        return _DEFAULT
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _DEFAULT
    if not isinstance(data, dict):
        return _DEFAULT
    return bool(data.get("enabled", _DEFAULT))


def is_enabled() -> bool:
    """Alias for `current()` — reads more naturally at call sites."""
    return current()


def set_enabled(flag: bool) -> None:
    """Persist the auto-resume toggle atomically."""
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps({"enabled": bool(flag)}, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_PATH)
