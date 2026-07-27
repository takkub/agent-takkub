"""Session prompt-cap policy shared by the pane model and UI.

Claude is currently the only provider whose transcript exposes the usage
shape consumed by :mod:`agent_takkub.token_meter`.  Provider capability
gating stays at the pane boundary; this module only owns provider-neutral
threshold parsing.

The cap is a *ratio* of the pane's own live context window
(:func:`agent_takkub.token_meter.effective_context_limit`), not a fixed
token count — a 200k-window pane and a 1M-window pane should both warn at
roughly the same fraction of "full", not at the same absolute number. Older
configs that stored an absolute token count (pre-ratio) still work: any
resolved value greater than 1 is treated as a legacy fixed cap instead of a
fraction.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_SESSION_CAP_RATIO = 0.85
SESSION_CAP_ENV = "TAKKUB_SESSION_CAP_TOKENS"
SESSION_CAP_SETTING = "session_cap/prompt_tokens"


@dataclass(frozen=True)
class SessionCapThreshold:
    """Resolved cap policy for one pane.

    Exactly one of `ratio`/`tokens` is set at a time; both `None` means the
    watchdog is disabled for this pane.
    """

    ratio: float | None = None
    tokens: int | None = None

    def tokens_for(self, context_limit: int) -> int | None:
        """Cap in tokens given the pane's live context-window size.

        Returns None when the watchdog is disabled.
        """
        if self.tokens is not None:
            return self.tokens
        if self.ratio is None:
            return None
        return max(1, int(context_limit * self.ratio))


SESSION_CAP_DISABLED = SessionCapThreshold()


def _parse_cap_value(value: object) -> SessionCapThreshold | None:
    """Parse one raw config value into a threshold, or None if unusable.

    `0` disables the watchdog. A value in `(0, 1]` is a context-window
    fraction. Anything greater than `1` is a legacy absolute token cap.
    """
    if value is None:
        return None
    try:
        text = str(value).strip().replace("_", "").replace(",", "")
        if not text:
            return None
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    if parsed == 0:
        return SESSION_CAP_DISABLED
    if parsed < 0:
        return None
    if parsed <= 1:
        return SessionCapThreshold(ratio=parsed)
    return SessionCapThreshold(tokens=int(parsed))


def resolve_session_cap_threshold(
    setting_value: object = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> SessionCapThreshold:
    """Resolve the cap policy with env > QSettings > default precedence.

    Invalid values are ignored (fall through to the next source) instead of
    disabling the watchdog silently. Passing ``environ`` makes the policy
    deterministic in tests and avoids platform-specific environment mutation.
    """
    env = os.environ if environ is None else environ
    env_spec = _parse_cap_value(env.get(SESSION_CAP_ENV))
    if env_spec is not None:
        return env_spec
    setting_spec = _parse_cap_value(setting_value)
    if setting_spec is not None:
        return setting_spec
    return SessionCapThreshold(ratio=DEFAULT_SESSION_CAP_RATIO)
