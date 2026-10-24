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
    "tiny": {"minutes": 30},
    "normal": {"minutes": 120},
    "deep": {"minutes": 360},
}
DEFAULT_LEAD_CACHED_INPUT_TOKENS = 100_000
_BUSINESS_DOMAIN = re.compile(
    r"\b(?:commission|turnover|pricing|price|tax|fee|discount|payout|threshold|"
    r"business\s+rule|eligibility|refund|deposit|withdrawal|balance|"
    r"revenue|sales|subtotal)\b"
    r"|ค่าคอม(?:มิชชั่น)?|คอมมิชชั่น|เปอร์เซ็นต์|ร้อยละ|ภาษี|ราคา|"
    r"ค่าธรรมเนียม|ส่วนลด|(?<!ต่อ)ยอด(?:ขาย|เงิน|ฝาก|ถอน)?|เงิน|บาท|สูตรคำนวณ",
    re.IGNORECASE,
)
_BUSINESS_VALUE = re.compile(
    r"(?<![\w])(?:[$฿]\s*\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"(?:\s*(?:%|บาท|฿|\$|THB|dollars?|usd|eur|percent|เปอร์เซ็นต์|ร้อยละ))"
    r"(?![\w])|(?<![\w])(?:[$฿]\s*\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s*THB)(?![\w])|"
    r"(?<![\w])\d+(?:[.,]\d+)?(?![\w])",
    re.IGNORECASE,
)
_TECHNICAL_VALUE = re.compile(
    # A dotted release/version has at least two dots. Do not consume decimal
    # business values such as 2.5% or 3.75 percent as though they were versions.
    r"(?<![\w])(?:v\d+(?:\.\d+)+|\d+(?:\.\d+){2,}|\d+(?:\.\d+)?\s*(?:px|ms|s|sec|seconds?|"
    r"minutes?|hours?|kb|mb|gb|tokens?|files?|rounds?)|\d+:\d+)(?![\w])",
    re.IGNORECASE,
)
_QUOTED_EXAMPLE = re.compile(
    r"(?:for example|e\.g\.|such as|เช่น|ตัวอย่าง(?:งาน)?)[^\n]*"
    r"(?:“[^”]*”|\"[^\"]*\"|'[^']*'|‘[^’]*’)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TaskLimits:
    minutes: int


def needs_spec_confirmation(task: str) -> bool:
    """Only gate numeric/formula rules tied to a business domain."""
    text = task or ""
    # Quoted literals in task descriptions often explain false positives or
    # give sample input. They are not the rule being requested.
    text = re.sub(r"“[^”]*”|‘[^’]*’|\"[^\"]*\"|'[^']*'", " ", text)
    text = _QUOTED_EXAMPLE.sub(" ", text)
    text = re.sub(r"`[^`]*`|(?<!\w)#\d+\b|(?:[\w./\\-]+\.py:\d+)", " ", text)
    text = re.sub(r"(?:[A-Za-z]:)?[\\/]?(?:[\w.-]+[\\/])+[\w.-]+", " ", text)
    text = _TECHNICAL_VALUE.sub(" ", text)
    domains = tuple(_BUSINESS_DOMAIN.finditer(text))
    values = tuple(_BUSINESS_VALUE.finditer(text))
    # Explicit units establish business values anywhere in the task. A bare
    # number only counts when it directly follows a business term (allowing a
    # small connector such as "to" in "refund threshold to 14 days").
    if not domains:
        return False
    for value in values:
        if re.search(r"(?:%|เปอร์เซ็นต์|ร้อยละ|บาท|฿|\$|\bTHB)\s*$", value.group(), re.IGNORECASE):
            return True
        for domain in domains:
            if domain.end() > value.start():
                continue
            between = text[domain.end() : value.start()]
            if re.fullmatch(
                r"[\s:=]*(?:to|is|equals|set\s+to|เป็น|คือ)?[\s:=]*",
                between,
                re.IGNORECASE,
            ):
                return True
    return False


def confirmation_digest(task: str) -> str:
    """Short stable binding between the confirmed task and its exact wording."""
    return hashlib.sha256((task or "").strip().encode("utf-8")).hexdigest()[:12]


def is_spec_confirmation_enabled(settings_home: Path | None = None) -> bool:
    """Check if spec confirmation modal is explicitly enabled in work-discipline.json.
    Defaults to False so assignments create backlog and proceed autonomously without
    blocking on desktop modal popups. Set 'spec_confirmation': true to require modal.
    """
    if settings_home is None:
        from .config import SETTINGS_HOME

        settings_home = SETTINGS_HOME
    try:
        raw = json.loads((settings_home / "work-discipline.json").read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "spec_confirmation" in raw:
            return bool(raw["spec_confirmation"])
    except (OSError, ValueError, TypeError):
        pass
    return False


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


def cap_reason(*, elapsed_s: float, minutes: int) -> str | None:
    """Return the hard limit crossed, if any. Elapsed time is the only cap —
    per-task token caps were removed (user 2026-10-24: no token limit)."""
    if elapsed_s >= minutes * 60:
        return "time"
    return None
