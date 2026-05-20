"""routing_planner — CLAUDE.md auto-routing rules as testable Python.

Encodes every section of the "Auto-routing (propose-then-fire)" spec so
Lead's classification logic can be unit-tested independently of prompt
context. If the prompt description and this module ever diverge, the
module is authoritative (it has tests; the prompt does not).

Public API
----------
classify(user_message, context=None) -> RoutingAction
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


# ─────────────────────────────────────────────────────────────────────
# Public types
# ─────────────────────────────────────────────────────────────────────


class ActionKind(Enum):
    PROPOSE = "propose"           # show plan table, wait for user confirm
    FIRE_ASSIGN = "fire_assign"   # execute takkub assign immediately
    FIRE_ONESHOT = "fire_oneshot" # execute takkub codex/gemini (no pane spawn)
    ASK_CLARIFY = "ask_clarify"   # ambiguous — need more info before acting
    INFORMATIONAL = "info"        # pure question/explanation, just respond


@dataclass
class RoutingAction:
    kind: ActionKind
    role: str | None = None               # primary role (single-role proposal)
    roles: list[str] | None = None        # multi-role parallel proposal
    task_hint: str | None = None          # key fragment from user message
    cross_check: list[str] | None = None  # extra roles to auto-fire (codex/gemini)
    reason: str = ""                      # human-readable explanation
    mixed: bool = False                   # has both informational + actionable intent


# ─────────────────────────────────────────────────────────────────────
# Pattern tables  (compiled once at import time)
# ─────────────────────────────────────────────────────────────────────

_ACTIONABLE_EN = re.compile(
    r"\b(add|build|implement|fix|refactor|migrate|setup|set.up|deploy|"
    r"test|create|make|write|update|change|delete|remove|rename|extract|"
    r"scaffold|install|configure|integrate|connect|seed|review|audit|run|"
    r"init(?:ialise|ialize)?|ลอง)\b",  # 'ลอง' (try/attempt) = Thai actionable
    re.IGNORECASE,
)
_ACTIONABLE_TH = re.compile(
    # ทำ(?!งาน|ไม) — "ทำงาน" means "work/function" (informational);
    # "ทำไม" means "why" (informational). Plain "ทำ" = do/make = actionable.
    r"(ทำ(?!งาน|ไม)|สร้าง|แก้|เพิ่ม|ลบ|ปรับ|เขียน|อัพ|แก้ไข|ติดตั้ง|ตั้งค่า|เชื่อม|รัน|ลอง)"
)

_INFORMATIONAL_EN = re.compile(
    # "do/does/is/are" are too broad — "do a code review" is actionable.
    # Keep only clearly non-actionable question words.
    r"\b(explain|why|show|read|list|what|how|describe|tell|where|when)\b",
    re.IGNORECASE,
)
_INFORMATIONAL_TH = re.compile(
    r"(ดู|อธิบาย|ทำไม|สรุป|บอก|อ่าน|คือ|หมายถึง|ยังไง|อย่างไร|ทำงานยังไง|ทำงานอย่างไร)"
)

# Explicit role: "ให้ backend ทำ X"
_EXPLICIT_ROLE = re.compile(
    r"ให้\s*(frontend|backend|mobile|devops|qa|reviewer|codex|gemini)"
    r"\s*(ทำ|สร้าง|แก้|build|implement|fix|test|review|deploy|refactor)",
    re.IGNORECASE,
)

# One-shot codex/gemini: "ถาม codex ว่า...", "ขอ codex review", "ให้ gemini ดู"
_ONESHOT = re.compile(
    r"(ถาม|ขอ|ให้)\s*(codex|gemini)\s*(ว่า|review|check|cross.check|ดู|ช่วย)?",
    re.IGNORECASE,
)

# Routing table: (pattern, primary_role_or_None, cross_check_list_or_None)
# Order matters — earlier entries win.  None role means "derive from content".
_ROUTE_TABLE: list[tuple[re.Pattern, str | None, list[str] | None]] = [
    # Rollout / strategy → gemini (checked before generic "deploy")
    (
        re.compile(
            r"\b(rollout|deployment.strategy|migration.plan|phase.plan|safe.deploy|strategy)\b",
            re.IGNORECASE,
        ),
        "gemini",
        None,
    ),
    # Refactor / extract / rename / migrate (verb form) → primary + codex
    (
        re.compile(r"\b(refactor|extract|rename|restructure|migrate)\b", re.IGNORECASE),
        None,  # derived from content keywords below
        ["codex"],
    ),
    # Code review / security audit
    (
        re.compile(r"\b(review|code.review|security.review|audit)\b", re.IGNORECASE),
        "reviewer",
        None,
    ),
    # Test / e2e / regression
    (
        re.compile(
            r"\b(test|smoke.test|e2e|end.to.end|regression|unit.test|integration.test)\b",
            re.IGNORECASE,
        ),
        "qa",
        None,
    ),
    # DevOps (checked before backend so "deploy pipeline" stays devops)
    (
        re.compile(
            r"\b(docker|compose|pipeline|infra|k8s|kubernetes|nginx|helm|terraform|CI|CD)\b",
            re.IGNORECASE,
        ),
        "devops",
        None,
    ),
    # Mobile
    (
        re.compile(
            r"\b(mobile|iOS|Android|Capacitor|React.Native|expo)\b", re.IGNORECASE
        ),
        "mobile",
        None,
    ),
    # Backend (API / db / schema)
    (
        re.compile(
            r"\b(endpoint|API|route|handler|schema|database|db|migration|model|"
            r"ORM|query|controller|service|REST|GraphQL)\b",
            re.IGNORECASE,
        ),
        "backend",
        None,
    ),
    # Frontend (UI / page / form / component …)
    (
        re.compile(
            r"\b(UI|page|form|component|button|style|CSS|layout|modal|dialog|"
            r"sidebar|navbar|widget|React|Vue|Next\.js|Tailwind|HTML)\b",
            re.IGNORECASE,
        ),
        "frontend",
        None,
    ),
]

# Confirm / abort / ambiguous signals
_CONFIRM_FIRE = re.compile(
    r"^(ok|ลุย|ลุยเลย|go|เอาเลย|ยืนยัน|confirm|proceed|ดำเนินการ)[\s!.]*$",
    re.IGNORECASE,
)
_CONFIRM_ABORT = re.compile(
    r"^(ไม่เอา|stop|หยุด|ยกเลิก|cancel|abort|no+)[\s!.]*$",
    re.IGNORECASE,
)
_EDIT_SIGNAL = re.compile(r"(แก้|ใช้.{1,30}แทน|change|swap|replace|switch)", re.IGNORECASE)
_FIRE_SIGNAL = re.compile(r"(ลุย|ลุยเลย|แล้วลุย|go|เอาเลย)", re.IGNORECASE)
_AMBIGUOUS_CONFIRM = re.compile(r"(เออๆ|เออ\s|ok\s*แต่|but\b|แต่)", re.IGNORECASE)

# Multi-role detection helpers (UI + API co-presence)
_HAS_UI = re.compile(
    r"\b(UI|page|form|component|button|style|CSS|layout|modal|dialog|"
    r"sidebar|navbar|widget|React|Vue|Next\.js|Tailwind|HTML|frontend|login.page|signup.page)\b",
    re.IGNORECASE,
)
_HAS_API = re.compile(
    r"\b(endpoint|API|route|handler|schema|database|db|model|query|"
    r"controller|service|REST|GraphQL|backend|server)\b",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────


def _is_actionable(msg: str) -> bool:
    return bool(_ACTIONABLE_EN.search(msg) or _ACTIONABLE_TH.search(msg))


def _is_informational(msg: str) -> bool:
    return bool(
        _INFORMATIONAL_EN.search(msg)
        or _INFORMATIONAL_TH.search(msg)
        or msg.strip().endswith("?")
    )


def _detect_explicit_role(msg: str) -> str | None:
    m = _EXPLICIT_ROLE.search(msg)
    return m.group(1).lower() if m else None


def _detect_oneshot(msg: str) -> str | None:
    m = _ONESHOT.search(msg)
    return m.group(2).lower() if m else None


def _handle_confirm(msg: str) -> RoutingAction | None:
    """Parse a reply to a pending proposal. Returns None if not a confirm phrase."""
    s = msg.strip()
    if _CONFIRM_ABORT.match(s):
        return RoutingAction(kind=ActionKind.INFORMATIONAL, reason="user aborted proposal")
    # Edit + fire together ("แก้ X แล้วลุยเลย")
    if _EDIT_SIGNAL.search(s) and _FIRE_SIGNAL.search(s):
        return RoutingAction(kind=ActionKind.FIRE_ASSIGN, reason="edit+fire shorthand")
    # Clean fire confirm
    if _CONFIRM_FIRE.match(s):
        return RoutingAction(kind=ActionKind.FIRE_ASSIGN, reason="user confirmed proposal")
    # Ambiguous partial confirm
    if _AMBIGUOUS_CONFIRM.search(s):
        return RoutingAction(kind=ActionKind.ASK_CLARIFY, reason="ambiguous — clarify before firing")
    # Edit-only (re-propose needed)
    if _EDIT_SIGNAL.search(s):
        return RoutingAction(kind=ActionKind.ASK_CLARIFY, reason="edit requested — re-propose")
    return None


def _derive_primary_role(msg: str) -> str:
    """Guess the primary role from content when the routing rule has role=None."""
    if _HAS_UI.search(msg):
        return "frontend"
    if _HAS_API.search(msg):
        return "backend"
    return "backend"  # safest default for ambiguous actionable tasks


def _route(msg: str) -> dict:
    """Apply routing decision table. Returns dict with role/roles/cross_check/reason."""
    # Multi-role: UI + API together → parallel frontend + backend
    if _HAS_UI.search(msg) and _HAS_API.search(msg):
        return {
            "roles": ["frontend", "backend"],
            "reason": "UI + API keywords detected — parallel roles",
        }
    for pattern, role, cross_check in _ROUTE_TABLE:
        if pattern.search(msg):
            resolved_role = role if role is not None else _derive_primary_role(msg)
            return {
                "role": resolved_role,
                "cross_check": cross_check,
                "reason": f"matched: {pattern.pattern[:50]}",
            }
    # No specific match — default to backend
    return {"role": "backend", "reason": "no domain keyword — defaulting to backend"}


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def classify(user_message: str, context: dict | None = None) -> RoutingAction:
    """Classify a user message and return the routing action Lead should take.

    Args:
        user_message: Raw message from the user.
        context: Optional state dict.  Keys:
            ``pending_proposal`` (bool) — True when Lead has shown a plan
            table and is waiting for the user to confirm/abort/edit it.

    Returns:
        RoutingAction with kind, role(s), cross_check, reason, mixed.
    """
    msg = user_message.strip()

    # 1. Explicit role ("ให้ backend ทำ X") → FIRE_ASSIGN immediately
    explicit = _detect_explicit_role(msg)
    if explicit:
        return RoutingAction(
            kind=ActionKind.FIRE_ASSIGN,
            role=explicit,
            task_hint=msg,
            reason="explicit role specified by user",
        )

    # 2. One-shot codex/gemini → FIRE_ONESHOT (no pane spawn)
    oneshot = _detect_oneshot(msg)
    if oneshot:
        return RoutingAction(
            kind=ActionKind.FIRE_ONESHOT,
            role=oneshot,
            task_hint=msg,
            reason="one-shot query to AI peer (no pane)",
        )

    # 3. Pending proposal → handle confirm/abort/edit phrases
    if context and context.get("pending_proposal"):
        result = _handle_confirm(msg.lower())
        if result:
            result.task_hint = msg
            return result

    # 4. Classify intent
    is_act = _is_actionable(msg)
    is_info = _is_informational(msg)
    is_mixed = is_act and is_info

    if is_info and not is_act:
        return RoutingAction(kind=ActionKind.INFORMATIONAL, reason="informational query")

    if not is_act:
        return RoutingAction(kind=ActionKind.INFORMATIONAL, reason="no actionable verb detected")

    # 5. Route actionable message to role(s)
    routing = _route(msg)
    return RoutingAction(
        kind=ActionKind.PROPOSE,
        role=routing.get("role"),
        roles=routing.get("roles"),
        task_hint=msg,
        cross_check=routing.get("cross_check"),
        reason=routing.get("reason", ""),
        mixed=is_mixed,
    )
