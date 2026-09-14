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
            r"\b(?:auth|authentication|authorization|oauth|jwt|login|logout|signin|signup"
            r"|password|permission|privilege|otp|2fa|mfa|rate[\s_-]?limit)\b"
            r"|(?:bypass|skip|disable|remove)\s*(?:verif|valid|signature|sanitiz|auth|check)"
            r"|(?:verif|valid|signature|sanitiz|auth|check).*(?:bypass|skip|disable|remove)"
            r"|(?:admin|role|session)[\s\-_]*(?:permission|access|based|privilege|panel|timeout|expiry|cookie|hijack|fixation)"
            r"|(?:permission|access|based|privilege|panel|timeout|expiry|cookie|hijack|fixation)[\s\-_]*(?:admin|role|session)"
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


def strip_budget(task_text: str) -> str:
    """Return *task_text* without a leading system-injected budget block (#585).

    The budget block is scaffolding the orchestrator prepends, not task content,
    so any length/threshold decision about the task itself must measure the body
    (see `orchestrator_text._task_handoff_pointer`: a ~300-char budget block used
    to push every short task over TASK_HANDOFF_THRESHOLD, turning a direct paste
    into a read-this-file pointer — an extra hop and extra tokens per assign).
    """
    text = task_text or ""
    for block in BUDGET_BLOCKS.values():
        if text.startswith(block):
            return text[len(block) :].lstrip(chr(13) + chr(10))
    return text


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


# ── Section-aware signal region (#602) ───────────────────────────────────────
# A classifier that keyword-matches the *whole* task text treats words that are
# merely mentioned (context/facts headings, variable & file names in backticks,
# text after a prohibition like "ห้าม") as if the task will touch them. That
# over-classifies plain UI/report/e2e/nginx fixes into "deep". Signals are
# therefore read from the region describing what the task will DO, not from
# everything the text happens to mention.
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")
_CONTEXT_HEADING_WORDS: frozenset[str] = frozenset(
    {
        "ข้อเท็จจริง",
        "หลักฐาน",
        "อาการ",
        "บริบท",
        "สาเหตุ",
        "สถานะปัจจุบัน",
        "ข้อมูลเดิม",
        "context",
        "contexts",
        "background",
        "backgrounds",
        "fact",
        "facts",
        "evidence",
        "symptom",
        "symptoms",
        "repro",
        "reproduction",
        "cause",
        "current",
    }
)
# Thai words must not use \b (Python regex treats adjacent Thai chars as \w,
# so there is no boundary between Thai words — same note as _DEEP_PATTERNS).
_FORBIDDEN_TAIL_RE = re.compile(r"ห้าม|ไม่ต้อง|อย่า|\b(?:never|don['’]t)\b", re.I)
_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)
_BACKTICK_RE = re.compile(r"`[^`]*`")


def _heading_is_context(line: str) -> bool:
    """True if *line* is a markdown heading whose first word is a context/facts label."""
    m = _HEADING_RE.match(line)
    if not m:
        return False
    head = line[m.end() :].lstrip(chr(35)).strip().split(None, 1)[0]
    return head.rstrip(":：").lower() in _CONTEXT_HEADING_WORDS


def _action_region(task_text: str) -> str:
    """Return the part of *task_text* describing what the task will do.

    Body lines under a known context/facts heading (ข้อเท็จจริง, หลักฐาน, อาการ,
    บริบท, ...) are a mention, not an action — a deep keyword there does not
    count (#602). Everything else (ทำ/TODO/checklist/prose) is the action region.
    """
    out: list[str] = []
    in_context = False
    for line in (task_text or "").split("\n"):
        # Only headings leave/enter a context block; bare lines inside one stay put.
        if _HEADING_RE.match(line):
            in_context = _heading_is_context(line)
        if in_context:
            continue
        out.append(line)
    return "\n".join(out)


def _clean_analysis(text: str) -> str:
    """Strip references/prohibitions so deep keywords don't count by mention.

    - drop fenced code and inline backtick contents (variable/file names in
      backticks are references, not actions),
    - cut the tail of each line from the first "ห้าม/ไม่ต้อง/อย่า/never/don't"
      (a prohibition is a boundary, not an action).
    """
    text = _FENCED_RE.sub(" ", text or "")
    text = _BACKTICK_RE.sub(" ", text)
    lines: list[str] = []
    for line in text.split("\n"):
        m = _FORBIDDEN_TAIL_RE.search(line)
        if m:
            line = line[: m.start()]
        lines.append(line)
    return "\n".join(lines)


# ── Explicit scope marker (#620) ──────────────────────────────────────────────
# A task's first line sometimes already states its own tier ("scope=deep",
# "scope: tiny", or a bare "(deep)"/"(scope normal)" aside in a heading) —
# that is Lead's own explicit call, made with full knowledge of the work,
# and must win over every keyword-derived signal below it, deep-category
# detection included. Restricted to the first line only so a later mention
# of the word "scope" in prose (e.g. "scope of this change") never matches.
_SCOPE_MARKER_RE = re.compile(
    r"\bscope\s*[:=]\s*(tiny|normal|deep)\b"
    r"|\(\s*(?:scope\s+)?(tiny|normal|deep)\s*\)",
    re.I,
)


def _explicit_scope_marker(text: str) -> str | None:
    """Return the tier named by an explicit scope marker on *text*'s first
    line, or None if there isn't one."""
    first_line = (text or "").split("\n", 1)[0]
    m = _SCOPE_MARKER_RE.search(first_line)
    if not m:
        return None
    tier = (m.group(1) or m.group(2) or "").lower()
    return tier if tier in SCOPE_TIERS else None


def _deep_categories(text: str) -> tuple[set[str], tuple[str, str] | None]:
    """Return `(set of matched deep category names, first (name, sample))`."""
    names: set[str] = set()
    first: tuple[str, str] | None = None
    for signal_name, pattern in _DEEP_PATTERNS:
        m = pattern.search(text)
        if m:
            names.add(signal_name)
            if first is None:
                first = (signal_name, m.group(0))
    return names, first


def classify(task_text: str) -> ScopeDecision:
    """Classify *task_text* into a ScopeDecision ("tiny" | "normal" | "deep").

    Precedence (#602):
      1. deep signal inside the *action region* (what the task says it will
         do/fix, excluding context/facts headings, backtick references, and
         text after "ห้าม/ไม่ต้อง/อย่า/never/don't") -> deep.
         If only >= 2 *distinct* deep categories exist anywhere in the cleaned
         text, that is also deep (multi-risk task even if phrased in passing).
      2. tiny signals match explicit user size limits (unless forbidden verbs match)
      3. normal is the fallback for everything else
    """
    text = strip_budget((task_text or "").strip())
    if not text:
        return ScopeDecision("normal", "ข้อความเปล่า (default tier)")

    marker = _explicit_scope_marker(text)
    if marker:
        return ScopeDecision(marker, f"ตาม scope={marker} ในข้อความ")

    analyzed = _clean_analysis(_action_region(text))
    full_analyzed = _clean_analysis(text)

    # 1. Deep check — action-region signals win; >= 2 distinct categories anywhere also count.
    action_cats, action_first = _deep_categories(analyzed)
    if action_cats:
        name, sample = action_first or ("deep", "")
        return ScopeDecision(
            "deep",
            f"ตรวจพบ signal deep: '{sample}' ({name}) ในส่วนที่ต้องทำ — ชนะทุก signal",
        )

    full_cats, full_first = _deep_categories(full_analyzed)
    if len(full_cats) >= 2:
        names = ", ".join(sorted(full_cats))
        return ScopeDecision(
            "deep",
            f"พบ {len(full_cats)} signal deep ต่างหมวด ({names}) — ถือเป็นงานระดับ deep",
        )
    if full_cats:
        name, sample = full_first or ("deep", "")
        return ScopeDecision(
            "normal",
            f"งานทั่วไป — signal '{sample}' ({name}) อยู่ในส่วนบริบท/อ้างอิง/หลังข้อห้าม ไม่ถือเป็น deep",
        )

    # 2. Forbidden tiny check (blocks tiny tier from refactor/rewrite/move/etc.)
    if not _FORBIDDEN_TINY_RE.search(analyzed):
        for signal_name, pattern in _TINY_KEYWORD_PATTERNS:
            match = pattern.search(analyzed)
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
