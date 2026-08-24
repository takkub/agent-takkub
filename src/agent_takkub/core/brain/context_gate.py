"""Context Gate (final closeout #C, `03_CONTEXT_TOKEN_EFFICIENCY.md` +
`11_MASTER_PROMPT_FOR_LEAD.md`'s "Token policy") — classifies a task's size
so `facade.build_context_for_assign` can scope which optional sources it
calls and how much budget it spends, instead of unconditionally calling
every source (OpenViking/Resource today; Figma/21st/Graft once item D of
the closeout pack wires them into Context Builder) for every assign
regardless of how small the task is.

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
    """`=0` reverts every caller to the pre-gate path (unclamped budget,
    OpenViking/Resource always attempted when the sidecar itself is on) —
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
    # Gates `context_builder.merge_openviking_traced` as a whole (both the
    # OpenViking sidecar and the local Resource/vault source it can pair
    # with in hybrid mode) — the only optional reference-source hook that
    # exists in Context Builder today. Figma/21st/Graft aren't wired into
    # context injection yet (closeout item D); extend this dataclass with
    # their own flag when they land instead of overloading this one.
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


def gate_budget(size: TaskSize, base_budget: int) -> int:
    """Clamps `base_budget` (`context_builder.budget_tokens_for`'s own
    per-model output) DOWN to this size's ceiling — never up: the plan
    doc's targets are a policy ceiling layered on top of the existing
    per-model budget, never a way to exceed it. `budget_floor` is
    informational (documents the doc's target range for tests/observability)
    rather than enforced — there is nothing to raise a too-small model
    budget up to."""
    if base_budget <= 0:
        return base_budget
    return min(base_budget, _POLICY[size].budget_ceiling)


__all__ = [
    "SourcePolicy",
    "TaskSize",
    "classify_task_size",
    "gate_budget",
    "gate_enabled",
    "policy_for",
]
