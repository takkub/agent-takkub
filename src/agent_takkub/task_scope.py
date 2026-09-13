"""Task scope budget & tier classifier (#585).

Classifies a task into one of three budget tiers:
- "deep": high risk or broad blast radius (schema, migration, auth, security,
  tokens, crypto, payments, dependencies, lockfiles, infra, CI/CD, cross-module
  refactor, project-wide rename). Wins over all other signals.
- "tiny": small, low-risk changes ("แค่", "นิดเดียว", "1 บรรทัด", "5 บรรทัด",
  one-line, typo, tweak, wording, label, copy, color, padding, margin,
  spacing, single config value, change text) or targeting a single pinpointed
  file/location without deep signals.
- "normal": default tier for everything else.

Pure stdlib-only leaf module (no PyQt, no config, no orchestrator imports)
to satisfy architectural contracts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SCOPE_TIERS: tuple[str, ...] = ("tiny", "normal", "deep")
TIER_ORDER: dict[str, int] = {"tiny": 0, "normal": 1, "deep": 2}


@dataclass(frozen=True)
class ScopeDecision:
    """Outcome of task scope classification."""

    scope: str  # "tiny" | "normal" | "deep"
    reason: str


# ── Deep patterns (win over every other signal) ──────────────────────────────
# Note: Thai keywords do not use \b because Python regex treats adjacent Thai
# characters as \w (no word boundary between Thai words).
_DEEP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("schema", re.compile(r"\b(?:schemas?|prisma)\b|สคีมา", re.I)),
    (
        "migration",
        re.compile(
            r"\b(?:migrations?|migrate|migrating)\b|ไมเกรต|ไมเกรชัน",
            re.I,
        ),
    ),
    (
        "auth",
        re.compile(
            r"\b(?:auth|authentication|authorization|oauth|jwt|login|logout|signin|signup)\b"
            r"|สิทธิ์|ล็อกอิน|ยืนยันตัวตน|ระบบสมาชิก|รหัสผ่าน",
            re.I,
        ),
    ),
    (
        "security",
        re.compile(
            r"\b(?:security|vulnerabilit(?:y|ies)|cve|xss|csrf|sanitize|sanitization|exploit)\b"
            r"|ความปลอดภัย|ช่องโหว่",
            re.I,
        ),
    ),
    (
        "token",
        re.compile(
            r"\b(?:api[_-]?keys?|secret[_-]?keys?|bearer)\b"
            r"|(?:หมุน|rotate)\s*(?:token|secret|key|api|คีย์)"
            r"|(?:api|auth|access|refresh|secret)[_-]?tokens?"
            r"|หมุน.*(?:token|secret)",
            re.I,
        ),
    ),
    (
        "crypto",
        re.compile(
            r"\b(?:crypto|cryptography|encryption|decryption|encrypt|decrypt|cipher|bcrypt|argon2)\b"
            r"|เข้ารหัส|ถอดรหัส",
            re.I,
        ),
    ),
    (
        "payment",
        re.compile(
            r"\b(?:payments?|stripe|paypal|billing|invoices?|checkout[_-]?sessions?)\b"
            r"|จ่ายเงิน|ชำระเงิน",
            re.I,
        ),
    ),
    (
        "dependency",
        re.compile(
            r"\b(?:dependenc(?:y|ies)|package\.json|requirements\.txt|pyproject\.toml|go\.mod|cargo\.toml|podfile|gemfile)\b"
            r"|(?:npm|pnpm|yarn|bun)\s+(?:install|add|i)\b|pip3?\s+install\b|(?:อัป|อัปเดต|อัปเกรด)\s*(?:dep|dependenc(?:y|ies))",
            re.I,
        ),
    ),
    (
        "lockfile",
        re.compile(
            r"\b(?:lockfiles?|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|uv\.lock|poetry\.lock|bun\.lock)\b",
            re.I,
        ),
    ),
    (
        "infra",
        re.compile(
            r"\b(?:infra|infrastructure|terraform|k8s|kubernetes|cluster)\b|อินฟรา",
            re.I,
        ),
    ),
    (
        "deploy",
        re.compile(
            r"\b(?:deploy|deployment|deploying|เดพลอย)\b.*(?:prod|production|staging|ขึ้น)"
            r"|(?:deploy|เดพลอย)\s+ขึ้น\s*(?:prod|production|staging)"
            r"|(?:deploy\s+to|ขึ้น)\s*(?:prod|production|staging)",
            re.I,
        ),
    ),
    (
        "ci",
        re.compile(
            r"\.github/(?:workflows|actions)|\b(?:gitlab[_-]?ci)\b"
            r"|(?:แก้|setup|config|สร้าง).*(?:ci|cd|github[_-]?actions|workflows?|pipelines?)",
            re.I,
        ),
    ),
    (
        "refactor_cross_module",
        re.compile(
            r"(?:refactor.*(?:ข้าม|across|both|all)|ข้าม\s*โมดูล|cross[_-]?module)",
            re.I,
        ),
    ),
    (
        "rename_project_wide",
        re.compile(
            r"(?:rename.*(?:ทั้ง|all|whole|across)|(?:เปลี่ยนชื่อ|แก้ชื่อ).*(?:ทั้งโปรเจค|ทั้งระบบ))",
            re.I,
        ),
    ),
    (
        "rewrite_major",
        re.compile(
            r"(?:rewrite|เขียนใหม่|รื้อ).*(?:ทั้งไฟล์|ทั้งระบบ|ทั้งโปรเจค|ใหม่ทั้งไฟล์)"
            r"|(?:ทั้งไฟล์|ทั้งระบบ|ทั้งโปรเจค).*(?:rewrite|เขียนใหม่|รื้อ)"
            r"|(?:rewrite|เขียนใหม่|รื้อ)\s+[\w./\\-]+\s*(?:ใหม่|ทั้ง)",
            re.I,
        ),
    ),
)

# Verbs / scopes that strictly forbid tiny tier (#585 round 2)
_FORBIDDEN_TINY_RE = re.compile(
    r"\b(?:rewrite|refactor|redesign)\b"
    r"|เขียนใหม่|ทำใหม่|ย้าย|แยก|รวม|ออกแบบใหม่|ทั้งไฟล์|ทั้งระบบ|ทั้งโปรเจค",
    re.I,
)

# ── Tiny patterns (checked when no deep signals match) ───────────────────────
_TINY_KEYWORD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "explicit_small",
        re.compile(
            r"แค่|นิดเดียว|เล็กน้อย|นิดๆ\s*หน่อยๆ",
            re.I,
        ),
    ),
    (
        "line_budget",
        re.compile(
            r"(?:[1-5]\s*บรรทัด|\b[1-5]\s*lines?\b|\bone[_-]?line\b|\bsingle[_-]?line\b)",
            re.I,
        ),
    ),
    (
        "typo_wording",
        re.compile(
            r"\b(?:typos?|labels?)\b"
            r"|(?:แก้|เปลี่ยน|fix|change|update)\s*(?:the\s+)?(?:copy|wordings?|ข้อความ)"
            r"|แก้คำผิด|คำผิด|เปลี่ยนข้อความ|แก้ข้อความ",
            re.I,
        ),
    ),
    (
        "styling_tweak",
        re.compile(
            r"\b(?:tweaks?|colors?|colours?|paddings?|margins?|spacings?)\b|แก้สี|สี\s+|เป็นสี|padding|margin|spacing",
            re.I,
        ),
    ),
    (
        "single_config",
        re.compile(
            r"ค่า\s*config\s*ตัวเดียว|\bsingle\s*config\b|\bone\s*config\b",
            re.I,
        ),
    ),
    (
        "pinpoint_spot",
        re.compile(
            r"[1-3]\s*จุด|จุดเดียว|\bsingle\s*spot\b",
            re.I,
        ),
    ),
)


# ── Budget block prose ───────────────────────────────────────────────────────
BUDGET_BLOCKS: dict[str, str] = {
    "tiny": (
        "งานขนาดเล็ก — ห้ามเขียนไฟล์เทสใหม่ · ห้ามรัน test suite หรือ takkub qa-gate · "
        "ห้าม refactor/จัดระเบียบนอกโจทย์ · ทดสอบแค่ว่าสิ่งที่แก้ทำงานจริง 1 อย่าง · เสร็จแล้ว takkub done ทันที "
        "· ถ้าลงมือแล้วพบว่างานใหญ่กว่าที่ประเมิน ให้ takkub progress บอก Lead ก่อน อย่าขยายเอง"
    ),
    "normal": (
        "ทดสอบของจริงว่าสิ่งที่แก้ทำงานถูก (รันจริง/เปิดดูจริง/เรียกฟังก์ชันจริง) — "
        "**ไม่ต้องเขียนไฟล์เทสใหม่** · ถ้าไฟล์ที่แตะมีเทสอยู่แล้ว ให้รัน targeted เฉพาะไฟล์นั้น"
    ),
    "deep": (
        "ทดสอบของจริง + เขียนเทสกันถอยเฉพาะ logic ที่เสี่ยงจริง "
        "(schema/auth/trust boundary/สิ่งที่พลาดแล้วแพง) ไม่ใช่ทุกบรรทัดที่แตะ"
    ),
}


def budget_block(scope: str) -> str:
    """Return the Thai prose budget block for *scope*."""
    norm = (scope or "normal").strip().lower()
    return BUDGET_BLOCKS.get(norm, BUDGET_BLOCKS["normal"])


def inject_budget(task_text: str, scope: str) -> str:
    """Prepend budget_block(scope) as the first line of task_text if not already present."""
    block = budget_block(scope)
    text = task_text or ""
    # Check if this exact block or any scope budget marker is already present
    if (
        block in text
        or "ห้ามเขียนไฟล์เทสใหม่" in text
        or "**ไม่ต้องเขียนไฟล์เทสใหม่**" in text
        or "เขียนเทสกันถอยเฉพาะ logic ที่เสี่ยงจริง" in text
    ):
        return text
    if not text.strip():
        return block
    return f"{block}\n\n{text}"


def classify(task_text: str) -> ScopeDecision:
    """Classify *task_text* into a ScopeDecision ("tiny" | "normal" | "deep").

    Precedence:
      1. deep signals always win (schema, migration, auth, security, lockfile...)
      2. tiny signals match explicit user size limits (unless forbidden verbs match)
      3. normal is the fallback for everything else
    """
    text = (task_text or "").strip()
    if not text:
        return ScopeDecision("normal", "ข้อความเปล่า (default tier)")

    # 1. Deep check (wins over everything)
    for signal_name, pattern in _DEEP_PATTERNS:
        match = pattern.search(text)
        if match:
            return ScopeDecision(
                "deep",
                f"ตรวจพบ signal deep: '{match.group(0)}' ({signal_name}) — ชนะทุก signal",
            )

    # 2. Forbidden tiny check (blocks tiny tier from refactor/rewrite/move/etc.)
    if not _FORBIDDEN_TINY_RE.search(text):
        for signal_name, pattern in _TINY_KEYWORD_PATTERNS:
            match = pattern.search(text)
            if match:
                return ScopeDecision(
                    "tiny",
                    f"ตรวจพบคำระบุขนาดงานเล็ก: '{match.group(0).strip()}' ({signal_name})",
                )

    # 3. Default
    return ScopeDecision("normal", "งานทั่วไป (default tier)")


# ── Fix loop ceiling (#585 round 3) ──────────────────────────────────────────
MAX_FIX_LOOP_ATTEMPTS: int = 2


@dataclass(frozen=True)
class FixLoopDecision:
    """Decision on whether a fix loop can proceed automatically or must stop."""

    allowed: bool
    attempt: int
    max_attempts: int
    action: str  # "auto_retry" | "ask_user" | "propose"
    reason: str
    user_question: str | None = None


def check_fix_loop_ceiling(
    scope: str,
    attempt: int,
    failure_signature: str = "",
) -> FixLoopDecision:
    """Check if a fix loop for the same issue/failure can auto-retry or must stop.

    Rules (#585 round 3):
    - scope="tiny" or "normal":
      - attempt <= MAX_FIX_LOOP_ATTEMPTS (rounds 1 & 2): auto-chain/auto-retry allowed.
      - attempt > MAX_FIX_LOOP_ATTEMPTS (round 3+): ceiling reached, stop immediately
        and ask user 1 short question.
    - scope="deep":
      - requires user proposal/confirmation before fix loop.
    """
    norm_scope = (scope or "normal").strip().lower()
    if norm_scope not in SCOPE_TIERS:
        norm_scope = "normal"

    if norm_scope == "deep":
        return FixLoopDecision(
            allowed=False,
            attempt=attempt,
            max_attempts=MAX_FIX_LOOP_ATTEMPTS,
            action="propose",
            reason="งานระดับ deep ต้อง propose/ขอ confirm จาก user ก่อน fix loop เสมอ",
            user_question="งานระดับ deep ล้มเหลว — ต้องการให้แก้ต่อหรือไม่?",
        )

    if attempt <= MAX_FIX_LOOP_ATTEMPTS:
        return FixLoopDecision(
            allowed=True,
            attempt=attempt,
            max_attempts=MAX_FIX_LOOP_ATTEMPTS,
            action="auto_retry",
            reason=(
                f"fix loop อัตโนมัติรอบที่ {attempt}/{MAX_FIX_LOOP_ATTEMPTS} "
                f"สำหรับ {norm_scope} — ยิงกลับ role เดิมได้ทันที"
            ),
            user_question=None,
        )

    # Ceiling reached (attempt > MAX_FIX_LOOP_ATTEMPTS)
    sig_info = f" '{failure_signature}'" if failure_signature else ""
    return FixLoopDecision(
        allowed=False,
        attempt=attempt,
        max_attempts=MAX_FIX_LOOP_ATTEMPTS,
        action="ask_user",
        reason=(
            f"ถึงเพดาน fix loop ({MAX_FIX_LOOP_ATTEMPTS} รอบ) สำหรับเรื่องเดียวกัน{sig_info} "
            "— หยุดวนลูปและถาม user"
        ),
        user_question=f"แก้เรื่องนี้ล้มเหลวครบ {MAX_FIX_LOOP_ATTEMPTS} รอบแล้ว — ต้องการให้แก้ต่อด้วยแนวทางไหน หรือข้ามไปก่อน?",
    )


class FixLoopTracker:
    """Tracks repeated failures on the same issue/signature to enforce fix loop ceiling (#585)."""

    def __init__(self, max_attempts: int = MAX_FIX_LOOP_ATTEMPTS) -> None:
        self.max_attempts = max_attempts
        self._attempts: dict[str, int] = {}

    def record_failure(self, signature: str = "", scope: str = "normal") -> FixLoopDecision:
        """Record a failure for *signature* and return the fix loop decision."""
        sig = (signature or "default").strip()
        self._attempts[sig] = self._attempts.get(sig, 0) + 1
        attempt = self._attempts[sig]
        return check_fix_loop_ceiling(scope, attempt, failure_signature=sig)

    def get_attempts(self, signature: str = "") -> int:
        """Return number of recorded failures for *signature*."""
        return self._attempts.get((signature or "default").strip(), 0)

    def can_auto_retry(self, signature: str = "", scope: str = "normal") -> bool:
        """Check if next attempt can be auto-retried."""
        sig = (signature or "default").strip()
        current = self._attempts.get(sig, 0)
        return check_fix_loop_ceiling(scope, current + 1, failure_signature=sig).allowed

    def reset(self, signature: str | None = None) -> None:
        """Reset attempt count for a signature, or all signatures if None."""
        if signature is None:
            self._attempts.clear()
        else:
            self._attempts.pop((signature or "default").strip(), None)
