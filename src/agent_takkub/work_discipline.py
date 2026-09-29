"""Runtime work-discipline policy shared by assign and pane watchdogs.

Settings live under ``SETTINGS_HOME`` (inside DATA_HOME for installed
packages). The defaults are deliberately bounded and apply without opt-in.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LIMITS = {
    "tiny": {"minutes": 30, "tokens": 50_000},
    "normal": {"minutes": 120, "tokens": 200_000},
    "deep": {"minutes": 360, "tokens": 500_000},
}
DEFAULT_LEAD_CACHED_INPUT_TOKENS = 100_000
_BUSINESS_SPEC = re.compile(
    r"(?:\b(?:formula|threshold|condition|pricing|business rule|eligibility|"
    r"calculation|percentage|percent|ratio|commission|tax|fee|discount)\b"
    r"|(?:สูตร|เงื่อนไข|กฎธุรกิจ|คำนวณ|เปอร์เซ็นต์|ร้อยละ|ราคา|ค่าธรรมเนียม))",
    re.IGNORECASE,
)
_NUMBER = re.compile(
    r"(?<![\w])(?:\d+(?:[.,]\d+)?%?|\$\s*\d+|"
    r"\d+\s*(?:ms|sec|seconds?|minutes?|hours?|วัน|บาท))(?![\w])",
    re.I,
)


@dataclass(frozen=True)
class TaskLimits:
    minutes: int
    tokens: int


def needs_spec_confirmation(task: str) -> bool:
    """Conservative detector for business rules and numeric constraints."""
    text = re.sub(r"(?<!\w)#\d+\b", "", task or "")
    return bool(_NUMBER.search(text) or _BUSINESS_SPEC.search(text))


def confirmation_digest(task: str) -> str:
    """Short stable binding between the confirmed task and its exact wording."""
    return hashlib.sha256((task or "").strip().encode("utf-8")).hexdigest()[:12]


def task_limits(scope: str, settings_home: Path | None = None) -> TaskLimits:
    """Read validated per-tier limits; corrupt or missing settings use defaults."""
    values = DEFAULT_LIMITS
    if settings_home is None:
        from .config import SETTINGS_HOME

        settings_home = SETTINGS_HOME
    try:
        raw = json.loads((settings_home / "work-discipline.json").read_text(encoding="utf-8"))
        supplied = raw.get("limits") if isinstance(raw, dict) else None
        if isinstance(supplied, dict):
            values = {
                key: {
                    **base,
                    **(supplied.get(key) if isinstance(supplied.get(key), dict) else {}),
                }
                for key, base in DEFAULT_LIMITS.items()
            }
    except (OSError, ValueError, TypeError):
        pass
    scope = scope if scope in DEFAULT_LIMITS else "normal"
    row = values.get(scope, values["normal"])
    return TaskLimits(
        minutes=_bounded_int(row.get("minutes"), DEFAULT_LIMITS[scope]["minutes"], 1, 1440),
        tokens=_bounded_int(row.get("tokens"), DEFAULT_LIMITS[scope]["tokens"], 1_000, 10_000_000),
    )


def lead_cached_input_threshold(settings_home: Path | None = None) -> int:
    if settings_home is None:
        from .config import SETTINGS_HOME

        settings_home = SETTINGS_HOME
    try:
        raw = json.loads((settings_home / "work-discipline.json").read_text(encoding="utf-8"))
        value = raw.get("lead_cached_input_tokens") if isinstance(raw, dict) else None
    except (OSError, ValueError, TypeError):
        value = None
    return _bounded_int(value, DEFAULT_LEAD_CACHED_INPUT_TOKENS, 10_000, 1_000_000)


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, max(minimum, parsed))


def cap_reason(
    *, elapsed_s: float, minutes: int, context_tokens: int | None, token_cap: int
) -> str | None:
    """Return the first hard limit crossed, if any."""
    if elapsed_s >= minutes * 60:
        return "time"
    if context_tokens is not None and context_tokens >= token_cap:
        return "tokens"
    return None
