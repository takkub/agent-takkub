"""Context Gate (final closeout #C, `03_CONTEXT_TOKEN_EFFICIENCY.md` +
`11_MASTER_PROMPT_FOR_LEAD.md`'s "Token policy") — classifies a task's size
so `facade.build_context_for_assign` can scope which optional sources it
calls and how much budget it spends, instead of unconditionally calling
every optional reference source (Figma/21st/Graft once item D of the
closeout pack wires them into Context Builder) for every assign regardless
of how small the task is.

Pure/stdlib-only, no I/O — `classify_task_size` only inspects task text
(plus an optional explicit override). `routing_planner.classify` has no
task-SIZE signal of its own to reuse (its `suggest_assign_mode` heuristic
answers a different question — pane vs subagent execution SHAPE, not
context scope) — this module's keyword tables are deliberately
independent, in the same regex-heuristic style.

`TAKKUB_CONTEXT_GATE=0` is the escape hatch back to the pre-gate behavior
(same "off flag = behavior exactly as before" contract `core.brain.flag`'s
V2 flags already use) — on by default per the plan doc.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

TaskSize = Literal["small", "medium", "large"]

_SIZES: frozenset[str] = frozenset({"small", "medium", "large"})

_ENV_GATE = "TAKKUB_CONTEXT_GATE"


def gate_enabled() -> bool:
    """`=0` reverts every caller to the pre-gate path (unclamped budget) —
    unset or any other value keeps the gate on (the shipped default)."""
    return os.environ.get(_ENV_GATE, "1") != "0"


# ── size heuristic — keyword tables, checked large > medium > small ───────
# Ambiguous overlap (e.g. "rename across multiple files") resolves to the
# LARGER bucket: under-provisioning a genuinely cross-file/architectural
# task is worse than a small task getting a slightly bigger budget than it
# strictly needed.

_LARGE_EN = re.compile(
    r"\b(architecture|greenfield|overhaul|rewrite)\b|"
    r"\bfrom\s+scratch\b|"
    r"\bnew\s+(feature|workflow|module|system|section|screen|integration|ui|page\s*flow)\b",
    re.IGNORECASE,
)
_LARGE_TH = re.compile(
    r"สถาปัตยกรรม|ฟีเจอร์ใหม่|ระบบใหม่|โมดูลใหม่|เวิร์กโฟลว์ใหม่|ขั้นตอนการทำงานใหม่|"
    r"ออกแบบระบบใหม่|เขียนใหม่ทั้งหมด|หน้าใหม่ทั้งหมด"
)

_MEDIUM_EN = re.compile(
    r"\b(refactor|cross[- ]file|multi[- ]file|multiple\s+files|integrat(?:e|ion)|"
    r"cross[- ]module)\b",
    re.IGNORECASE,
)
_MEDIUM_TH = re.compile(r"รีแฟคเตอร์|ข้ามไฟล์|หลายไฟล์|ข้ามโมดูล")

_SMALL_EN = re.compile(
    r"\b(rename|spacing|padding|margin|colou?r|typo|wording|font[- ]size|copy\s*change|"
    r"label\s*text)\b",
    re.IGNORECASE,
)
_SMALL_TH = re.compile(r"เปลี่ยนชื่อ|จัดระยะ|เว้นวรรค|แก้สี|พิมพ์ผิด|ปรับข้อความเล็กน้อย")

# No keyword matched at all — fall back to raw task-text length as a rough
# proxy for how much is actually being asked for.
_SMALL_MAX_LEN = 60
_MEDIUM_MAX_LEN = 200


def _explicit_override(flags: Mapping[str, object] | None) -> TaskSize | None:
    if not flags:
        return None
    value = flags.get("context") if hasattr(flags, "get") else None
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in _SIZES else None  # type: ignore[return-value]


def has_explicit_override(flags: Mapping[str, object] | None) -> bool:
    """Public wrapper so callers outside this module (e.g. `facade.py`
    deciding whether Context Strategy still applies) can check for an
    explicit `--context` override without reaching into the private
    `_explicit_override` this module already uses internally."""
    return _explicit_override(flags) is not None


def classify_task_size(
    task_text: str,
    role: str | None = None,
    flags: Mapping[str, object] | None = None,
) -> TaskSize:
    """`role` is accepted for signature symmetry with `facade.build_context_
    for_assign`'s other calls (a future refinement may want role-specific
    thresholds) but unused today — every role gets the same text heuristic.
    `flags` may carry an explicit `{"context": "small"|"medium"|"large"}`
    override (the assign-time `--context` escape hatch); an unrecognized or
    missing value falls through to the heuristic below."""
    override = _explicit_override(flags)
    if override is not None:
        return override

    text = (task_text or "").strip()
    if not text:
        return "small"

    if _LARGE_EN.search(text) or _LARGE_TH.search(text):
        return "large"
    if _MEDIUM_EN.search(text) or _MEDIUM_TH.search(text):
        return "medium"
    if _SMALL_EN.search(text) or _SMALL_TH.search(text):
        return "small"

    if len(text) <= _SMALL_MAX_LEN:
        return "small"
    if len(text) <= _MEDIUM_MAX_LEN:
        return "medium"
    return "large"


# ── per-size source/budget policy ──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    size: TaskSize
    # Gates any optional reference-source hook Context Builder wires in on
    # top of Brain/Conversation (none exist today — the OpenViking/
    # Resource pair this originally gated was removed along with
    # OpenViking). Figma/21st/Graft aren't wired into context injection yet
    # (closeout item D); extend this dataclass with their own flag when
    # they land instead of overloading this one.
    allow_reference_sources: bool
    budget_floor: int
    budget_ceiling: int


_POLICY: dict[TaskSize, SourcePolicy] = {
    "small": SourcePolicy(
        "small", allow_reference_sources=False, budget_floor=2000, budget_ceiling=4000
    ),
    "medium": SourcePolicy(
        "medium", allow_reference_sources=True, budget_floor=4000, budget_ceiling=8000
    ),
    "large": SourcePolicy(
        "large", allow_reference_sources=True, budget_floor=6000, budget_ceiling=12000
    ),
}


def policy_for(size: TaskSize) -> SourcePolicy:
    return _POLICY[size]


# v2-hardening C, `05_TOKEN_CONTROLLER.md`'s "retry/rework history" budget
# input and Large's own "6k-12k initial, expand in stages" target: each
# retry raises the CEILING (never the base_budget itself — `gate_budget`
# still clamps down to whichever is smaller, so this never pads an
# under-sized model window) by 25%, capped at 2x the size's normal ceiling
# so a runaway retry loop can't silently balloon the budget forever.
_RETRY_CEILING_STEP = 0.25
_RETRY_CEILING_MAX_MULTIPLIER = 2.0


def gate_budget(size: TaskSize, base_budget: int, *, retry_count: int = 0) -> int:
    """Clamps `base_budget` (`context_builder.budget_tokens_for`'s own
    per-model output) DOWN to this size's ceiling — never up: the plan
    doc's targets are a policy ceiling layered on top of the existing
    per-model budget, never a way to exceed it. `budget_floor` is
    informational (documents the doc's target range for tests/observability)
    rather than enforced — there is nothing to raise a too-small model
    budget up to.

    `retry_count` (from `escalation.next_retry_count`) stages the ceiling
    upward on top of whatever size `escalation.escalate_for_retry` already
    picked — the size bump alone (small->medium->large) already raises the
    ceiling once; this is the "expand in stages" half for retries that stay
    within the same (already-escalated) bucket. Still a ceiling, not a
    quota: `min(base_budget, ...)` below means this can only ever shrink
    what a caller receives relative to an unbounded model window, never
    force it to fill unused space."""
    if base_budget <= 0:
        return base_budget
    ceiling = _POLICY[size].budget_ceiling
    if retry_count > 0:
        multiplier = min(1.0 + retry_count * _RETRY_CEILING_STEP, _RETRY_CEILING_MAX_MULTIPLIER)
        ceiling = int(ceiling * multiplier)
    return min(base_budget, ceiling)


# ── Context Strategy (Fast/Automatic/Deep, `13_SIMPLE_UX.md`) ─────────────
# Persisted setting lives in `core_v2_settings.py` (read via `flag.
# context_strategy`, which owns the env/settings precedence — this module
# stays I/O-free); the three values below are what `flag.py` validates
# against and what this module's own `strategy_forced_size` accepts.

Strategy = Literal["fast", "automatic", "deep"]
STRATEGIES: tuple[Strategy, ...] = ("fast", "automatic", "deep")


def strategy_forced_size(
    strategy: str, size: TaskSize, risk_flags: tuple[str, ...] = ()
) -> tuple[TaskSize, str | None]:
    """`automatic` (default) never overrides the classifier — returns
    `(size, None)` unchanged. `deep` always forces `large`. `fast` forces
    the smallest size that still respects the risk-domain floor (never
    below `medium` when `risk_flags` is non-empty — same "never de-escalate
    a risk domain below MEDIUM" invariant `task_complexity.py` already
    enforces for its own risk override), which can mean de-escalating a
    classifier result that scored bigger than the risk floor for other
    reasons (e.g. a large file-count estimate) — `fast` means "trust the
    user's minimal-context intent", bounded only by the risk floor, not by
    whatever the classifier otherwise guessed.

    Returns `(effective_size, reason)` — `reason` is `None` when the
    strategy made no change, so a caller can skip adding a no-op trace
    line."""
    if strategy == "deep":
        if size == "large":
            return size, None
        return "large", f"strategy=deep forced size {size} -> large"
    if strategy == "fast":
        floor: TaskSize = "medium" if risk_flags else "small"
        if floor == size:
            return size, None
        return floor, f"strategy=fast forced size {size} -> {floor}"
    return size, None


def skipped_sources(size: TaskSize) -> list[dict[str, str]]:
    """Explainable Trace (`07_EXPLAINABLE_TRACE.md`) — reference sources
    this size's own `SourcePolicy.allow_reference_sources` gates off never
    even get a call attempt; this documents why, in the same
    `{name, reason}` shape the plan doc's own worked example uses. Other
    per-build skip reasons (conversation summary disabled, etc.) are outside
    this module's own state and are appended by `facade.py`'s caller."""
    policy = policy_for(size)
    if policy.allow_reference_sources:
        return []
    return [
        {
            "name": "reference_sources",
            "reason": f"task_size={size} — no design/reference signal required",
        }
    ]


__all__ = [
    "STRATEGIES",
    "SourcePolicy",
    "Strategy",
    "TaskSize",
    "classify_task_size",
    "gate_budget",
    "gate_enabled",
    "has_explicit_override",
    "policy_for",
    "skipped_sources",
    "strategy_forced_size",
]
