"""Account plan tier (Pro vs Max).

`provider_state.py` gates *which CLIs* are usable; this gates *which models
the owner's account can reach*. Different boundary, different file, same
persist-across-restart, status-bar-toggle UX — kept apart on purpose.

Why this exists: the 1M-context model variant (the `[1m]` suffix, e.g.
`claude-opus-4-8[1m]`) is gated behind usage credits. A Pro account hits a
hard error — "Usage credits required for 1M context · turn on usage credits
at claude.ai/settings/usage, or use --model to switch to standard context".
The Lead pane spawns with NO `--model` flag, so it inherits the owner's
default model; on a Max install that default is often the `[1m]` variant.
Recording the plan lets the orchestrator pin the Lead to a standard-context
model under Pro instead of letting it inherit a 1M default and hard-fail.

Teammate panes are unaffected: their per-role tiers (orchestrator
`_ROLE_MODEL_TIERS`) already use plain model ids with no `[1m]` suffix.

State file: `~/.takkub/plan.json`   Format: `{"tier": "pro"}` | `{"tier": "max"}`
Missing file or corrupt JSON → MAX (the cockpit's original behaviour — no
surprise downgrade for the install that shipped before this setting existed).

Persists across cockpit restart by design: account plan is user-level intent,
not session-scoped (mirrors provider_state).
"""

from __future__ import annotations

import json
from pathlib import Path

MAX = "max"
PRO = "pro"

# The two selectable tiers. Default is MAX so installs predating this setting
# keep their original behaviour (Lead inherits the user default, incl. [1m]).
TIERS: frozenset[str] = frozenset({MAX, PRO})
_DEFAULT = MAX

# Standard-context model the Lead falls back to under Pro. Keep Opus (best
# orchestration) but drop the `[1m]` variant that needs usage credits.
# Override per-install via the TAKKUB_PRO_LEAD_MODEL env var (read in the
# orchestrator, not here, to keep this module env-free and trivially testable).
PRO_LEAD_MODEL = "claude-opus-4-8"

_PATH = Path.home() / ".takkub" / "plan.json"


def path() -> Path:
    """Where state lives. Function form so tests can monkeypatch `_PATH`."""
    return _PATH


def current() -> str:
    """Return the current plan tier. Missing file or corrupt JSON → MAX."""
    if not _PATH.exists():
        return _DEFAULT
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _DEFAULT
    if not isinstance(data, dict):
        return _DEFAULT
    tier = str(data.get("tier", _DEFAULT)).lower().strip()
    return tier if tier in TIERS else _DEFAULT


def is_pro() -> bool:
    """True iff the owner is on a Pro plan (1M context unavailable)."""
    return current() == PRO


def set_current(tier: str) -> None:
    """Persist `tier` atomically.

    Raises ValueError on an unknown tier (catches typos at the call site
    rather than silently writing garbage that load() would then ignore).
    """
    tier = str(tier).lower().strip()
    if tier not in TIERS:
        raise ValueError(f"unknown plan tier: {tier!r}")
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps({"tier": tier}, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_PATH)
