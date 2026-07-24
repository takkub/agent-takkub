"""Session prompt-cap policy shared by the pane model and UI.

Claude is currently the only provider whose transcript exposes the usage
shape consumed by :mod:`agent_takkub.token_meter`.  Provider capability
gating stays at the pane boundary; this module only owns provider-neutral
threshold parsing and the user/agent-facing advisory text.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

DEFAULT_SESSION_CAP_TOKENS = 180_000
SESSION_CAP_ENV = "TAKKUB_SESSION_CAP_TOKENS"
SESSION_CAP_SETTING = "session_cap/prompt_tokens"


def _positive_int(value: object) -> int | None:
    """Parse a positive integer setting, returning None for invalid input."""
    if value is None:
        return None
    try:
        parsed = int(str(value).strip().replace("_", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def resolve_session_cap_threshold(
    setting_value: object = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Resolve the prompt cap with env > QSettings > default precedence.

    Invalid or non-positive values are ignored instead of disabling the
    watchdog silently.  Passing ``environ`` makes the policy deterministic in
    tests and avoids platform-specific environment mutation.
    """
    env = os.environ if environ is None else environ
    env_value = _positive_int(env.get(SESSION_CAP_ENV))
    if env_value is not None:
        return env_value
    setting = _positive_int(setting_value)
    if setting is not None:
        return setting
    return DEFAULT_SESSION_CAP_TOKENS


def teammate_session_cap_advice(prompt: int, threshold: int) -> str:
    """Instruction queued for a teammate only after its current turn is idle."""
    return (
        f"⚠️ [session-cap] prompt context {prompt:,} tokens crossed the "
        f"{threshold:,}-token cap. Do not interrupt or discard the current task. "
        "Finish it and report normally with `takkub done`. Before accepting more "
        "work, use `/compact`; if `takkub done` closes this pane, let the cockpit "
        "reopen a fresh pane instead."
    )
