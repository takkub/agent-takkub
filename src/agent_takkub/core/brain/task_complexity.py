"""Task Complexity Classifier v2 (`docs/plans/v2-hardening-2026-08-24/02_
CLASSIFIER_V2.md`) — a structural/risk-scored Stage 2 layered in front of
`context_gate.classify_task_size`'s Stage 1 keyword-bucket heuristic.

Stage 1 (`context_gate.classify_task_size`) stays exactly as it is — same
signature, same regex tables, same tests — this module never edits it.
Stage 2 reuses it as one input signal (agreement/disagreement feeds
`confidence`) and as the literal fallback result when `task_text` is empty
(nothing to score).

Pure/stdlib-only, no I/O, no LLM call — same "inspects task text only"
contract `context_gate.py` documents for itself. Estimated files/modules
come from parsing path-like tokens out of the task text itself; there is no
Graft-graph lookup here even though `graft_store.py` exists and could in
principle confirm real topology, because that store is a handle onto an
external CLI-built graph on disk (subprocess/IO-bound to populate) — wiring
it in would break the "no I/O" contract this module and `context_gate.py`
both keep today. ponytail: revisit if a cheap in-process topology index
ever exists; until then this is a text-only estimate, not a verified count.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .context_gate import TaskSize, _explicit_override, classify_task_size

# ── risk domains — hard-override list (never SMALL) ────────────────────────
# EN/TH keyword groups per domain. `auth` matches bare "login"/"auth" too
# (the spec's own worked example: "แก้สี button หน้า login auth" must not
# classify small just because "แก้สี" is a Stage 1 small-signal word).

_RISK_DOMAINS: dict[str, re.Pattern[str]] = {
    "auth": re.compile(
        r"\b(auth(?:entication|orization)?|login|log[- ]?in|session|oauth|jwt|permission)\b|"
        r"ล็อกอิน|เข้าสู่ระบบ|สิทธิ์|การยืนยันตัวตน|โทเคน",
        re.IGNORECASE,
    ),
    "security": re.compile(
        r"\b(security|vulnerabilit\w*|exploit|injection|xss|csrf|encrypt\w*|secret|credential)\b|"
        r"ความปลอดภัย|ช่องโหว่|เข้ารหัส|รหัสลับ",
        re.IGNORECASE,
    ),
    "payment": re.compile(
        r"\b(payment|billing|invoice|checkout|credit\s*card|refund|stripe)\b|"
        r"ชำระเงิน|การเงิน|ใบแจ้งหนี้|บัตรเครดิต|คืนเงิน",
        re.IGNORECASE,
    ),
    "migration": re.compile(
        r"\b(migrat\w*|alter\s+table|schema\s+change)\b|"
        r"ไมเกรชัน|ย้ายฐานข้อมูล|เปลี่ยนสคีมา",
        re.IGNORECASE,
    ),
    "destructive": re.compile(
        r"\b(delete|drop\s+table|truncate|wipe|purge|force[- ]push|reset\s*--hard)\b|"
        r"ลบถาวร|ล้างข้อมูล|ทำลายข้อมูล",
        re.IGNORECASE,
    ),
    "prod": re.compile(
        r"\bprod(?:uction)?\b|live\s+environment|"
        r"โปรดักชัน|ขึ้นโปรดักชัน|สภาพแวดล้อมจริง",
        re.IGNORECASE,
    ),
    "infra": re.compile(
        r"\b(infra(?:structure)?|ci/?cd|docker|kubernetes|k8s|server\s+config\w*)\b|"
        r"โครงสร้างพื้นฐาน|ระบบเซิร์ฟเวอร์",
        re.IGNORECASE,
    ),
}

# ── other scored signal categories (name -> pattern), each list capped by
# the caller to the spec's own 0-N range for that category ─────────────────

_API_SCHEMA_CATEGORIES: dict[str, re.Pattern[str]] = {
    "api/endpoint": re.compile(r"\b(api|endpoint|route)\b|เอนด์พอยต์|เส้นทาง\s*api", re.IGNORECASE),
    "schema/model": re.compile(r"\b(schema|model|contract|dto)\b|สคีมา|โมเดลข้อมูล", re.IGNORECASE),
    "database/migration": re.compile(
        r"\b(database|migrat\w*|table|column)\b|ฐานข้อมูล|ตาราง|ไมเกรชัน", re.IGNORECASE
    ),
}

_HISTORY_CATEGORIES: dict[str, re.Pattern[str]] = {
    "history/past": re.compile(r"\b(history|previous(?:ly)?|past)\b|ประวัติ|ที่ผ่านมา", re.IGNORECASE),
    "rationale/decision": re.compile(
        r"\b(rationale|decision|why\s+(?:was|did))\b|เหตุผล|ทำไมถึง|มติ", re.IGNORECASE
    ),
}

_DESIGN_CATEGORIES: dict[str, re.Pattern[str]] = {
    "ui/design": re.compile(r"\b(ui|design|mockup|wireframe)\b|ดีไซน์|ออกแบบ", re.IGNORECASE),
    "screen/layout": re.compile(
        r"\b(screen|layout|component|page)\b|หน้าจอ|เลย์เอาต์|คอมโพเนนต์", re.IGNORECASE
    ),
}

_MULTI_AGENT_CATEGORIES: dict[str, re.Pattern[str]] = {
    "cross-role": re.compile(
        r"\b(frontend\s+and\s+backend|backend\s+and\s+frontend|multiple\s+roles?|"
        r"cross[- ]team)\b|หลายทีม|หลายฝ่าย",
        re.IGNORECASE,
    ),
    "coordinate": re.compile(r"\b(coordinat\w*|hand\s*off)\b|ประสานงาน|ส่งต่องาน", re.IGNORECASE),
}

_PROD_ROLLBACK_CATEGORIES: dict[str, re.Pattern[str]] = {
    "deploy/release": re.compile(r"\b(deploy\w*|release)\b|ปล่อยรีลีส|ดีพลอย", re.IGNORECASE),
    "rollback/revert": re.compile(r"\b(rollback|revert)\b|ย้อนกลับ|ยกเลิกการเปลี่ยนแปลง", re.IGNORECASE),
    "hotfix/incident": re.compile(r"\b(hotfix|incident|outage)\b|แก้ด่วน|เหตุขัดข้อง", re.IGNORECASE),
}

# ── file/module estimate — text-only, no filesystem access ─────────────────

_FILE_TOKEN_RE = re.compile(
    r"[\w./\\-]+\.(?:py|ts|tsx|js|jsx|md|json|ya?ml|css|html|sql|go|rs|java|kt)\b"
)
_EXPLICIT_FILE_COUNT_RE = re.compile(r"\b(\d+)\s*(?:files?|ไฟล์)\b", re.IGNORECASE)
_EXPLICIT_MODULE_COUNT_RE = re.compile(r"\b(\d+)\s*(?:modules?|services?|โมดูล)\b", re.IGNORECASE)


def _estimate_files_and_modules(text: str) -> tuple[int, int]:
    unique_paths = set(_FILE_TOKEN_RE.findall(text))
    explicit_files = _EXPLICIT_FILE_COUNT_RE.search(text)
    estimated_files = max(len(unique_paths), int(explicit_files.group(1)) if explicit_files else 0)

    normalized = (p.replace("\\", "/") for p in unique_paths)
    module_prefixes = {p.rsplit("/", 1)[0] for p in normalized if "/" in p}
    explicit_modules = _EXPLICIT_MODULE_COUNT_RE.search(text)
    estimated_modules = max(
        len(module_prefixes), int(explicit_modules.group(1)) if explicit_modules else 0
    )
    return estimated_files, estimated_modules


def _score_categories(
    text: str, categories: Mapping[str, re.Pattern[str]], cap: int
) -> tuple[int, list[str]]:
    hits = [name for name, pattern in categories.items() if pattern.search(text)]
    return min(len(hits), cap), hits


def _bucket_for_files(n: int) -> int:
    if n <= 0:
        return 0
    if n == 1:
        return 1
    if n <= 3:
        return 2
    return 3


def _bucket_for_modules(n: int) -> int:
    if n <= 0:
        return 0
    if n == 1:
        return 1
    if n == 2:
        return 2
    return 3


def _bucket_for_score(score: int) -> TaskSize:
    if score <= 4:
        return "small"
    if score <= 9:
        return "medium"
    return "large"


_SIZE_RANK: dict[TaskSize, int] = {"small": 0, "medium": 1, "large": 2}
_RANK_SIZE: dict[int, TaskSize] = {v: k for k, v in _SIZE_RANK.items()}


@dataclass(frozen=True, slots=True)
class TaskComplexity:
    """Stage 2 classifier output. `score`/`estimated_files`/`estimated_
    modules` are diagnostic — `size` is the only field callers need for
    routing (same `TaskSize` Stage 1 already returns), the rest exists so
    the Explainable Trace (`07_EXPLAINABLE_TRACE.md`) can show its work."""

    size: TaskSize
    score: int
    confidence: float
    reasons: tuple[str, ...] = field(default_factory=tuple)
    risk_flags: tuple[str, ...] = field(default_factory=tuple)
    estimated_files: int = 0
    estimated_modules: int = 0


def classify_task_complexity(
    task_text: str,
    role: str | None = None,
    flags: Mapping[str, object] | None = None,
) -> TaskComplexity:
    """Stage 2 — structural/risk-scored classification. Falls back to
    Stage 1 (`classify_task_size`) verbatim for an explicit `flags`
    override or empty task text; otherwise scores the signals in `02_
    CLASSIFIER_V2.md` and applies the risk-domain hard override (never
    SMALL) before bucketing."""
    override = _explicit_override(flags)
    if override is not None:
        return TaskComplexity(
            size=override,
            score=0,
            confidence=1.0,
            reasons=(f"explicit override: context={override}",),
        )

    text = (task_text or "").strip()
    if not text:
        return TaskComplexity(
            size="small",
            score=0,
            confidence=0.3,
            reasons=("empty task text — Stage 1 fallback",),
        )

    reasons: list[str] = []

    estimated_files, estimated_modules = _estimate_files_and_modules(text)
    files_score = _bucket_for_files(estimated_files)
    if files_score:
        reasons.append(f"files: ~{estimated_files} impacted file(s) (+{files_score})")

    modules_score = _bucket_for_modules(estimated_modules)
    if modules_score:
        reasons.append(f"modules: ~{estimated_modules} impacted module(s) (+{modules_score})")

    api_score, api_hits = _score_categories(text, _API_SCHEMA_CATEGORIES, cap=3)
    if api_score:
        reasons.append(f"api/schema: matched {api_hits} (+{api_score})")

    risk_hits = [name for name, pattern in _RISK_DOMAINS.items() if pattern.search(text)]
    risk_score = min(len(risk_hits), 4)
    if risk_score:
        reasons.append(f"risk: matched domain(s) {risk_hits} (+{risk_score})")

    history_score, history_hits = _score_categories(text, _HISTORY_CATEGORIES, cap=2)
    if history_score:
        reasons.append(f"history: matched {history_hits} (+{history_score})")

    design_score, design_hits = _score_categories(text, _DESIGN_CATEGORIES, cap=2)
    if design_score:
        reasons.append(f"design: matched {design_hits} (+{design_score})")

    multi_agent_score, multi_agent_hits = _score_categories(text, _MULTI_AGENT_CATEGORIES, cap=2)
    if multi_agent_score:
        reasons.append(f"multi-agent: matched {multi_agent_hits} (+{multi_agent_score})")

    prod_score, prod_hits = _score_categories(text, _PROD_ROLLBACK_CATEGORIES, cap=3)
    if prod_score:
        reasons.append(f"prod/rollback: matched {prod_hits} (+{prod_score})")

    score = (
        files_score
        + modules_score
        + api_score
        + risk_score
        + history_score
        + design_score
        + multi_agent_score
        + prod_score
    )
    scored_bucket = _bucket_for_score(score)
    reasons.append(f"score={score} -> {scored_bucket}")

    # Stage 1 stays in the mix as a floor, not just a confidence check: it
    # catches keyword-obvious cases (e.g. "refactor") that the structural
    # signals above have no category for, and — same ambiguous-overlap rule
    # `context_gate.py` already documents for itself — the LARGER of the two
    # bucket estimates wins, since under-provisioning a genuinely bigger
    # task is worse than a small one getting a slightly bigger budget.
    stage1 = classify_task_size(text, role, None)
    agree = stage1 == scored_bucket
    combined_rank = max(_SIZE_RANK[scored_bucket], _SIZE_RANK[stage1])
    size = _RANK_SIZE[combined_rank]
    if not agree:
        reasons.append(f"stage1 heuristic={stage1} vs stage2 score bucket={scored_bucket}")

    if risk_hits and _SIZE_RANK[size] < _SIZE_RANK["medium"]:
        reasons.append(
            f"risk override: domain(s) {risk_hits} present — minimum size raised to medium"
        )
        size = "medium"

    categories_hit = sum(
        1
        for s in (
            files_score,
            modules_score,
            api_score,
            risk_score,
            history_score,
            design_score,
            multi_agent_score,
            prod_score,
        )
        if s > 0
    )
    confidence = 0.4 + 0.075 * categories_hit
    if not agree:
        confidence -= 0.25
        reasons.append(f"confidence lowered: Stage 1 heuristic disagreed (stage1={stage1})")
    if size != scored_bucket:
        confidence -= 0.1
    confidence = max(0.1, min(1.0, round(confidence, 2)))
    reasons.append(f"confidence={confidence}")

    return TaskComplexity(
        size=size,
        score=score,
        confidence=confidence,
        reasons=tuple(reasons),
        risk_flags=tuple(risk_hits),
        estimated_files=estimated_files,
        estimated_modules=estimated_modules,
    )


__all__ = ["TaskComplexity", "classify_task_complexity"]
