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
from dataclasses import dataclass
from enum import Enum

# ─────────────────────────────────────────────────────────────────────
# Public types
# ─────────────────────────────────────────────────────────────────────


class ActionKind(Enum):
    PROPOSE = "propose"  # show plan table, wait for user confirm
    FIRE_ASSIGN = "fire_assign"  # execute takkub assign immediately
    FIRE_ONESHOT = "fire_oneshot"  # execute takkub codex/gemini (no pane spawn)
    ASK_CLARIFY = "ask_clarify"  # ambiguous — need more info before acting
    INFORMATIONAL = "info"  # pure question/explanation, just respond
    EXPLAIN_SYSTEM = "explain_system"  # "review/explain the system" → produce HTML explainer
    GENERATE_GUIDE_HTML = (
        "generate_guide_html"  # user-facing guide/setup/how-to/checklist → md source + HTML
    )


@dataclass
class RoutingAction:
    kind: ActionKind
    role: str | None = None  # primary role (single-role proposal)
    roles: list[str] | None = None  # multi-role parallel proposal
    task_hint: str | None = None  # key fragment from user message
    cross_check: list[str] | None = None  # extra roles to auto-fire (codex/gemini)
    reason: str = ""  # human-readable explanation
    mixed: bool = False  # has both informational + actionable intent
    # Tier 2c: ordered execution when the message signals a data dependency
    # between the multi-role split (e.g. "form ตาม schema ที่ backend ส่ง").
    # None = independent, parallel dispatch is fine. Non-None = dispatch in
    # THIS order, waiting for each done (Multi-mode must not fan these out).
    sequence: list[str] | None = None


# ─────────────────────────────────────────────────────────────────────
# Pattern tables  (compiled once at import time)
# ─────────────────────────────────────────────────────────────────────

_ACTIONABLE_EN = re.compile(
    r"\b(add|build|implement|fix|refactor|migrate|setup|set.up|deploy|rollout|"
    r"test|create|make|write|update|change|delete|remove|rename|extract|"
    r"scaffold|install|configure|integrate|connect|seed|review|audit|run|"
    r"debug|analyze|verify|optimi[sz]e|investigate|upgrade|enable|patch|"
    r"port|moderni[sz]e|convert|replace|"
    r"init(?:ialise|ialize)?|ลอง)\b",  # 'ลอง' (try/attempt) = Thai actionable
    re.IGNORECASE,
)
_IMPLEMENTATION_EN = re.compile(
    r"\b(add|build|implement|create|make|write|scaffold|setup|set.up)\b",
    re.IGNORECASE,
)
_IMPLEMENTATION_TH = re.compile(r"(ทำ|สร้าง|เพิ่ม|เขียน|จัด)")
_ACTIONABLE_TH = re.compile(
    # ทำ(?!งาน|ไม) — "ทำงาน" means "work/function" (informational);
    # "ทำไม" means "why" (informational). Plain "ทำ" = do/make = actionable.
    # "รีวิว" (review) + "ออกแบบ" (design) added so design-critic triggers
    # like "รีวิว UI หน้า X" or "ออกแบบ flow X" classify as actionable.
    r"(ทำ(?!งาน|ไม)|สร้าง|แก้|เพิ่ม|ลบ|ปรับ|เขียน|อัพ|แก้ไข|ติดตั้ง|ตั้งค่า|"
    r"เชื่อม|รัน|ลอง|จัด|ฝาก|รบกวน|เช็ค|รีวิว|ออกแบบ)"
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

# ─────────────────────────────────────────────────────────────────────
# UI / API keyword cores  — single source of truth for both the routing
# table (single-role rules) and the multi-role _HAS_UI/_HAS_API detectors.
#
# Pre-refactor these patterns were duplicated inline; adding a keyword to
# the routing entry but forgetting to mirror it in _HAS_UI/_HAS_API caused
# silent drift (e.g. `migration`/`ORM` lived in the backend route but not
# in _HAS_API, so "add migration to login form" missed multi-role).  Now
# the base tuples drive both — the _MULTIROLE_EXTRA tuples add signals
# that are too weak to route a single role on their own but help when
# paired with the other side (UI + API simultaneous-work intent).
# ─────────────────────────────────────────────────────────────────────

_UI_EN_BASE: tuple[str, ...] = (
    "UI",
    "page",
    "form",
    "component",
    "button",
    "style",
    "CSS",
    "layout",
    "modal",
    "dialog",
    "sidebar",
    "navbar",
    "widget",
    "React",
    "Vue",
    r"Next\.js",
    "Tailwind",
    "HTML",
)
_UI_EN_MULTIROLE_EXTRA: tuple[str, ...] = ("frontend", r"login\.page", r"signup\.page")
_UI_TH_FRAGMENT = r"หน้าจอ|(?<!ก่อน)(?<!ข้าง)(?<!ด้าน)(?<!เบื้อง)หน้า(?=\s*[/a-zA-Z])|ปุ่ม"

_API_EN_BASE: tuple[str, ...] = (
    "endpoint",
    "API",
    "route",
    "handler",
    "schema",
    "database",
    "db",
    "migration",
    "model",
    "ORM",
    "query",
    "controller",
    "service",
    "REST",
    "GraphQL",
)
_API_EN_MULTIROLE_EXTRA: tuple[str, ...] = ("backend", "server")
_API_EN_AMBIGUOUS: frozenset[str] = frozenset({"model", "query", "service"})
_API_EN_MULTIROLE_BASE: tuple[str, ...] = tuple(
    token for token in _API_EN_BASE if token.lower() not in _API_EN_AMBIGUOUS
)
_API_TH_FRAGMENT = r"ฐานข้อมูล|หลังบ้าน"


def _build_ui_regex(extra: tuple[str, ...] = ()) -> re.Pattern:
    en_alt = "|".join(_UI_EN_BASE + extra)
    return re.compile(rf"(?:\b({en_alt})\b|{_UI_TH_FRAGMENT})", re.IGNORECASE)


def _build_api_regex(
    extra: tuple[str, ...] = (), *, base: tuple[str, ...] = _API_EN_BASE
) -> re.Pattern:
    en_alt = "|".join(base + extra)
    return re.compile(rf"(?:\b({en_alt})\b|{_API_TH_FRAGMENT})", re.IGNORECASE)


# Explicit role — four patterns, each captures the role in its own group.
# _detect_explicit_role() returns the first non-None group.
_EXPLICIT_ROLE = re.compile(
    r"(?:"
    # Pattern 1 (original): ให้ <role> <action-verb>
    r"ให้\s*(frontend|backend|mobile|devops|qa|reviewer|critic|designer|codex|gemini)"
    r"\s*(?:ทำ|สร้าง|แก้|build|implement|fix|test|review|deploy|refactor)"
    r"|"
    # Pattern 2: <role> ช่วย <action-verb>  — e.g. "backend ช่วยแก้ X"
    r"(frontend|backend|mobile|devops|qa|reviewer|critic|designer|codex|gemini)"
    r"\s*ช่วย\s*(?:ทำ|สร้าง|แก้|build|implement|fix|test|review|deploy|refactor)"
    r"|"
    # Pattern 3: ฝาก <role> <verb>  — e.g. "ฝาก devops ดู pipeline"
    r"ฝาก\s*(frontend|backend|mobile|devops|qa|reviewer|critic|designer|codex|gemini)"
    r"\s*(?:ทำ|แก้|ดู|review|check|build|implement|fix|test|deploy|refactor)"
    r"|"
    # Pattern 4: <non-ai-role> review/check/ดู  — exclude codex/gemini (handled by _ONESHOT)
    # \b only on English words; ดู is Thai so no \b needed
    r"(frontend|backend|mobile|devops|qa|reviewer|critic|designer)"
    r"\s*(?:review\b|check\b|ดู)"
    r")",
    re.IGNORECASE,
)

# "Explain / review the SYSTEM (how it works)" → produce an HTML system
# explainer (not a chat answer, not a code review). Distinguished from
# code-review / design-review by the system/architecture + understand-how
# signal. Examples: "รีวิวระบบหน่อย ทำงานยังไง", "อธิบายระบบ",
# "how does the system work", "explain the architecture", "system overview".
_EXPLAIN_SYSTEM = re.compile(
    r"(?:"
    r"รีวิว\s*ระบบ"
    r"|อธิบาย\s*(?:ระบบ|โครงสร้าง|สถาปัตยกรรม|โค้?ด|codebase)"
    r"|(?:ระบบ|โค้?ด|codebase)\s*(?:นี้|ตัวนี้)?\s*(?:ทำงาน|เป็น)\s*(?:ยังไง|อย่างไร|ยัง)"
    r"|(?:ภาพรวม|โครงสร้าง|สถาปัตยกรรม)\s*(?:ระบบ|โปรเจ[คก]ต?|code|app)"
    r"|\b(?:explain|describe|walk\s*me\s*through|overview\s*of|map\s*out)\b"
    r"[^.\n]{0,25}\b(?:system|architecture|codebase|how\s*it\s*works)\b"
    r"|\bhow\s+(?:does|do)\b[^.\n]{0,25}\b(?:system|architecture|the\s*app|everything)\b"
    r"[^.\n]{0,15}\bwork"
    r"|\b(?:system|architecture)\s+(?:overview|explainer|diagram|map|walkthrough)\b"
    r")",
    re.IGNORECASE,
)

# "Write a setup guide / how-to / checklist / คู่มือ" for users → produce md source + HTML.
# Requires an explicit doc-indicator word or combination so bare "setup docker"
# and "add checklist component" don't false-positive.
#
# Positive: เขียน setup guide สำหรับ LINE · คู่มือการใช้งาน · วิธีตั้งค่า X
#           installation guide · getting started guide · checklist for deploy
# Negative: setup docker compose · setup CI pipeline · add checklist component
_GENERATE_GUIDE = re.compile(
    r"(?:"
    # ── Thai standalone doc-indicator words ──────────────────────────────────
    r"คู่มือ"  # "user manual / guide"
    r"|วิธีตั้งค่า"  # "how to configure"
    r"|วิธีใช้"  # "how to use"
    r"|เอกสาร(?:ติดตั้ง|การใช้งาน|สำหรับผู้ใช้)"  # "installation/usage docs"
    r"|เขียน\s*(?:docs?|เอกสาร)\s*(?:ให้|สำหรับ)"  # "write docs for"
    # ── Thai checklist with doc-writing context ───────────────────────────────
    r"|(?:เขียน|สร้าง|ทำ)\s*checklist"  # "write/create checklist"
    r"|checklist\s*(?:สำหรับ|ของ|ให้)"  # "checklist for/of X"
    # ── English: "<topic> guide/tutorial/manual/how-to/checklist" ────────────
    r"|\b(?:setup|install(?:ation)?|getting[- ]started|onboarding|usage|user)"
    r"\s+(?:guide|tutorial|manual|how.to|instructions?|walkthrough|checklist)\b"
    # ── English: "write/create/draft <...> guide/tutorial/manual/how-to" ─────
    r"|\b(?:write|create|make|draft|build|produce)\b[^.\n]{0,30}"
    r"\b(?:guide|how.to|tutorial|manual|walkthrough)\b"
    # ── Other English doc-intent patterns ────────────────────────────────────
    r"|\bhow.to\s+guide\b"
    r"|\bstep.by.step\s+(?:guide|instructions?|tutorial)\b"
    r"|\bchecklist\s+for\b"  # "checklist for X"
    r")",
    re.IGNORECASE,
)

# One-shot codex/gemini: "ถาม codex ว่า...", "ขอ codex review", "ให้ gemini ดู"
# Also direct: "codex review function นี้", "gemini check plan"
_ONESHOT = re.compile(
    r"(?:"
    r"(?:ถาม|ขอ|ให้)\s*(codex|gemini)\s*(?:ว่า|review|check|cross.check|ดู|ช่วย)?"
    r"|"
    r"(codex|gemini)\s*(?:review\b|check\b|ลอง)"
    r")",
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
    # Design / UI critique — routed to `critic` (post-QA visual reviewer).
    # MUST come before the generic `review` rule below or "design review"
    # / "UI review" would land on `reviewer` (which is code-review only).
    # Triggers: "design review", "UI review", "UX review", "visual review",
    # "heuristic" (Nielsen), "look and feel", and Thai equivalents:
    # "รีวิว UI / หน้าตา / ดีไซน์", "ดู UI", "ดู หน้าตา".
    # Cross-check: gemini pane spawned in parallel so critic can immediately
    # `takkub send --to gemini` shot paths without waiting for a fresh pane.
    (
        re.compile(
            r"(?:"
            r"\b(design.review|UI.review|UX.review|visual.review|heuristic|look.and.feel|"
            r"design.critique|design.critic|critique.UI|review.UI|review.design)\b"
            r"|รีวิว\s*(?:UI|หน้าตา|ดีไซน์|design|ux)"
            r"|ดู\s*(?:UI|หน้าตา|ดีไซน์)"
            r"|ปรับ(?:ปรุง)?\s*(?:UI|หน้าตา|ดีไซน์)\s*(?:หน่อย|ให้)?"
            r")",
            re.IGNORECASE,
        ),
        "critic",
        ["gemini"],
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
        re.compile(r"\b(mobile|iOS|Android|Capacitor|React.Native|expo)\b", re.IGNORECASE),
        "mobile",
        None,
    ),
    # Backend (API / db / schema) — Thai: ฐานข้อมูล (database), หลังบ้าน (server-side)
    # Pattern built from _API_EN_BASE so the routing rule and multi-role
    # detection (_HAS_API below) stay in sync automatically.
    (_build_api_regex(), "backend", None),
    # Frontend (UI / page / form / component …) — Thai: หน้าจอ/หน้า (screen/page), ปุ่ม (button)
    # หน้า uses lookbehind (ก่อน/ข้าง/ด้าน/เบื้อง) + lookahead (\s*[/a-zA-Z]) inside
    # _UI_TH_FRAGMENT to avoid false positives from compound words: ก่อนหน้า,
    # ข้างหน้า, หน้าหนาว, หน้าฝน, etc.
    (_build_ui_regex(), "frontend", None),
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

# Multi-role detection helpers (UI + API co-presence). Built from the same
# base tuples as the routing-table entries above plus _MULTIROLE_EXTRA terms
# ("frontend"/"backend"/"server" etc. — too weak alone to route a single role
# but useful for pair detection). Drift between the two layers is now
# impossible: change the base, both update.
_HAS_UI = _build_ui_regex(_UI_EN_MULTIROLE_EXTRA)
_HAS_API = _build_api_regex(_API_EN_MULTIROLE_EXTRA, base=_API_EN_MULTIROLE_BASE)


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────


def _is_actionable(msg: str) -> bool:
    return bool(_ACTIONABLE_EN.search(msg) or _ACTIONABLE_TH.search(msg))


def _is_informational(msg: str) -> bool:
    return bool(
        _INFORMATIONAL_EN.search(msg) or _INFORMATIONAL_TH.search(msg) or msg.strip().endswith("?")
    )


def _detect_explicit_role(msg: str) -> str | None:
    m = _EXPLICIT_ROLE.search(msg)
    if not m:
        return None
    for g in m.groups():
        if g is not None:
            return g.lower()
    return None


def _detect_oneshot(msg: str) -> str | None:
    m = _ONESHOT.search(msg)
    if not m:
        return None
    for g in m.groups():
        if g is not None:
            return g.lower()
    return None


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
        return RoutingAction(
            kind=ActionKind.ASK_CLARIFY, reason="ambiguous — clarify before firing"
        )
    # Edit-only (re-propose needed)
    if _EDIT_SIGNAL.search(s):
        return RoutingAction(kind=ActionKind.ASK_CLARIFY, reason="edit requested — re-propose")
    return None


# ─────────────────────────────────────────────────────────────────────
# Tier 2c — dependency sequencing + verify-failure classification
# ─────────────────────────────────────────────────────────────────────

# Signals that the frontend half of a UI+API task CONSUMES the backend half's
# output (schema/contract/response) — parallel dispatch would build the form
# against a guessed contract and fail integration. Ordered dispatch instead.
_FRONTEND_NEEDS_BACKEND = re.compile(
    r"(ตาม\s*(schema|contract|spec|response)|"
    r"ใช้\s*(ข้อมูล|response|ผลลัพธ์|result)\s*จาก|"
    r"จาก\s*endpoint|ที่\s*api\s*(ส่ง|ให้|คืน)|"
    r"รอ\s*backend|หลัง(จาก)?\s*backend\s*(เสร็จ|done)|"
    r"depends?\s+on\s+the\s+(api|backend|schema|contract)|"
    r"based\s+on\s+the\s+(api|schema|contract|response)|"
    r"(after|once)\s+the\s+backend)",
    re.IGNORECASE,
)

# Verify-failure signatures → the role a fix loop should route back to.
# Ordered by root-cause priority: infra failures masquerade as API errors and
# API errors masquerade as UI errors, so devops > backend > frontend > qa.
_FAILURE_RULES: list[tuple[re.Pattern, str, str]] = [
    (
        re.compile(
            r"(docker|compose|container|econnrefused|connection\s+refused|"
            r"service\s+(down|unavailable)|healthcheck|stack\s*ไม่ขึ้น|"
            r"port\s+(ชน|conflict|in\s+use)|ยังไม่ได้\s*(รัน|start))",
            re.IGNORECASE,
        ),
        "devops",
        "infra/stack signature (container/port/healthcheck)",
    ),
    (
        re.compile(
            r"(\b50[0-4]\b|\b40[13]\b|api\s*(error|fail)|exception|traceback|"
            r"stack\s*trace|database|migration|\bsql\b|endpoint.{0,20}(fail|error|พัง)|"
            r"unauthorized|jwt|token\s+(invalid|expired))",
            re.IGNORECASE,
        ),
        "backend",
        "server/API signature (5xx/auth/db/exception)",
    ),
    (
        re.compile(
            r"(selector|hydration|\bcss\b|layout|render|component|"
            r"undefined\s+is\s+not|console\s+error|หน้า(เพี้ยน|พัง|ขาว)|"
            r"ปุ่ม.{0,15}(หาย|กดไม่ได้)|ui\s+(broken|ผิด))",
            re.IGNORECASE,
        ),
        "frontend",
        "UI signature (render/selector/console)",
    ),
    (
        re.compile(
            r"(flaky|intermittent|เทสไม่เสถียร|timeout\s*รอ|"
            r"wait(ed)?\s+for\s+(element|selector)|retry\s+passed)",
            re.IGNORECASE,
        ),
        "qa",
        "test-side flakiness (fix the test, not the app)",
    ),
]


def classify_failure(note: str) -> tuple[str | None, str]:
    """Map a verify-fail note to the role a fix loop should target (Tier 2c).

    Returns ``(role, reason)`` from the first matching signature, or
    ``(None, "")`` when nothing matches — the Lead diagnoses manually then.
    Rule order encodes root-cause priority (infra > server > UI > test);
    this is a SUGGESTION for the fix-loop proposal, never an auto-route.
    """
    s = (note or "").strip()
    if not s:
        return None, ""
    for pattern, role, reason in _FAILURE_RULES:
        if pattern.search(s):
            return role, reason
    return None, ""


def _derive_primary_role(msg: str) -> str:
    """Guess the primary role from content when the routing rule has role=None."""
    if _HAS_UI.search(msg):
        return "frontend"
    if _HAS_API.search(msg):
        return "backend"
    return "backend"  # safest default for ambiguous actionable tasks


def _sub_note(role: str) -> str:
    """Reason fragment when a disabled codex/gemini role is routed anyway.

    The spawn layer (provider_config.effective_provider_for) backs an
    unavailable codex/gemini role with claude, so we never refuse — we just
    flag the substitution so Lead can pre-warn the user.
    """
    return f"{role} disabled → claude substitutes (same slot)"


def _route(msg: str) -> dict:
    """Apply routing decision table. Returns dict with role/roles/cross_check/reason."""
    # Multi-role: UI + API implementation together → parallel frontend + backend.
    # Non-implementation intents (review/test/refactor/design) must reach the
    # ordered route table so reviewer/qa/critic/codex cross-check rules win.
    has_impl_intent = bool(_IMPLEMENTATION_EN.search(msg) or _IMPLEMENTATION_TH.search(msg))
    if has_impl_intent and _HAS_UI.search(msg) and _HAS_API.search(msg):
        # Tier 2c: a data dependency between the halves forbids parallel
        # dispatch — the frontend would code against a guessed contract.
        if _FRONTEND_NEEDS_BACKEND.search(msg):
            return {
                "roles": ["frontend", "backend"],
                "sequence": ["backend", "frontend"],
                "reason": (
                    "UI + API with data dependency (frontend consumes backend "
                    "schema/response) — SEQUENCE backend→frontend, ห้าม parallel"
                ),
            }
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
        context: Optional state dict. Keys:
            ``pending_proposal`` (bool) — True when Lead has shown a plan
            table and is waiting for the user to confirm/abort/edit it.
            ``disabled_providers`` (set[str]) — provider names the user has
            disabled via the cockpit status bar toggle. These are NO LONGER
            refused: routing proceeds normally and the spawn layer backs the
            unavailable codex/gemini role with claude ("Claude รับตำแหน่งแทน").
            The only effect here is a substitution note in ``reason`` and a
            disabled FIRE_ONESHOT degrading to FIRE_ASSIGN (a claude-backed
            pane — one-shot has no substitute path).

    Returns:
        RoutingAction with kind, role(s), cross_check, reason, mixed.
    """
    msg = user_message.strip()
    disabled: set[str] = set((context or {}).get("disabled_providers") or set())

    # 1. Explicit role ("ให้ backend ทำ X") → FIRE_ASSIGN immediately
    explicit = _detect_explicit_role(msg)
    if explicit:
        # A disabled codex/gemini explicit role is fired anyway — the spawn
        # layer backs it with claude. Just note the substitution.
        reason = "explicit role specified by user"
        if explicit in disabled:
            reason = f"explicit role; {_sub_note(explicit)}"
        return RoutingAction(
            kind=ActionKind.FIRE_ASSIGN,
            role=explicit,
            task_hint=msg,
            reason=reason,
        )

    # 2. One-shot codex/gemini → FIRE_ONESHOT (no pane spawn)
    oneshot = _detect_oneshot(msg)
    if oneshot:
        if oneshot in disabled:
            # No CLI to one-shot against — substitute a claude-backed pane in
            # that role's slot instead (also matches "Lead never one-shots,
            # always uses a pane").
            return RoutingAction(
                kind=ActionKind.FIRE_ASSIGN,
                role=oneshot,
                task_hint=msg,
                reason=f"one-shot target disabled → {_sub_note(oneshot)} via pane",
            )
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

    # 4. User-facing guide / setup / how-to / checklist → produce md source + HTML.
    # Checked before EXPLAIN_SYSTEM so "write a guide on how the system works"
    # (guide intent more specific) and before the informational short-circuit so
    # "วิธีใช้ X" / "วิธีตั้งค่า X" don't degrade to a plain chat answer.
    # No role to assign — Lead writes the .md then runs design_review_html.
    if _GENERATE_GUIDE.search(msg):
        return RoutingAction(
            kind=ActionKind.GENERATE_GUIDE_HTML,
            task_hint=msg,
            reason=(
                "user-facing guide/setup/checklist request → produce md source"
                " + run design_review_html converter to emit .html"
            ),
        )

    # 5. "Explain / review the system" → produce an HTML system explainer.
    # Checked before the informational short-circuit (so "ระบบทำงานยังไง"
    # doesn't degrade to a chat answer) and before the route table (so
    # "รีวิวระบบ" doesn't land on reviewer/critic). Pure-understanding intent
    # — no role to assign; Lead writes a system-overview .md then runs
    # design_review_html to emit the .html.
    if _EXPLAIN_SYSTEM.search(msg):
        return RoutingAction(
            kind=ActionKind.EXPLAIN_SYSTEM,
            task_hint=msg,
            reason="explain/review-the-system request → produce HTML explainer (md source + converter)",
        )

    # 7. Classify intent
    is_act = _is_actionable(msg)
    is_info = _is_informational(msg)
    is_mixed = is_act and is_info

    if is_info and not is_act:
        return RoutingAction(kind=ActionKind.INFORMATIONAL, reason="informational query")

    if not is_act:
        return RoutingAction(kind=ActionKind.INFORMATIONAL, reason="no actionable verb detected")

    # 8. Route actionable message to role(s)
    routing = _route(msg)
    primary = routing.get("role")
    cross_check = routing.get("cross_check")
    reason = routing.get("reason", "")

    # A disabled codex/gemini — whether it's the primary (e.g. rollout→gemini
    # when gemini is off) or a cross-check (refactor→codex) — is no longer
    # refused or stripped. Routing proceeds identically; the spawn layer backs
    # the unavailable role with claude. We only annotate the reason so Lead can
    # tell the user a claude substitute is coming.
    subs = [r for r in ([primary] + (cross_check or [])) if r in disabled]
    if subs:
        reason = f"{reason}; " + ", ".join(_sub_note(r) for r in subs)

    return RoutingAction(
        kind=ActionKind.PROPOSE,
        role=primary,
        roles=routing.get("roles"),
        task_hint=msg,
        cross_check=cross_check,
        reason=reason,
        mixed=is_mixed,
        sequence=routing.get("sequence"),
    )
