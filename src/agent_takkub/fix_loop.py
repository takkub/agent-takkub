"""Persistent fix-loop ledger (#762): the code that makes the two-attempt
ceiling in `task_scope.check_fix_loop_ceiling` real.

A *failure sequence* is one recurring failure of one piece of work. Identity is
project + stable work identity (backlog card > issue ref > task-text hash) +
a normalized failure signature — never the role, provider or raw note, so the
count survives a role/provider switch, a pane restart and the verify hop.

Storage: ``RUNTIME_DIR/fix_loop/<project>/sequences.json`` (own domain folder,
atomic write, default-on, nothing machine-specific).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass

from .backlog import _atomic_write
from .config import RUNTIME_DIR
from .path_safe import safe_segment
from .task_scope import MAX_FIX_LOOP_ATTEMPTS, FixLoopDecision, check_fix_loop_ceiling

SCHEMA_VERSION = 1
_MAX_SEQUENCES = 200
_MAX_AGE_S = 30 * 86400
_MATCH_JACCARD = 0.5
_LOCK = threading.Lock()
# Roles whose clean `done` means "verified" — only these close a sequence (a
# fixer's own done is the attempt, not the proof it worked).
VERIFY_ROLES = frozenset({"qa", "reviewer", "critic", "tester", "security"})
# Read-only diagnosis is the sanctioned way out of a stopped loop, so these
# roles are never refused at the ceiling.
CEILING_EXEMPT_ROLES = VERIFY_ROLES | frozenset({"analyst"})

_REF_TAG = re.compile(r"\[ref [^\]]*\]")
BRIEF_MARK = "## ประวัติความพยายามแก้ก่อนหน้า"
_NOISE = re.compile(r"0x[0-9a-f]+|[0-9a-f]{7,40}|\d+", re.I)
_WORD = re.compile(r"[a-z_][a-z0-9_./\\-]{2,}", re.I)
_STOP = frozenset({"the", "and", "for", "with", "not", "failed", "fail", "error", "test", "tests"})


def _path(project: str):
    return RUNTIME_DIR / "fix_loop" / safe_segment(project) / "sequences.json"


def identity_for(backlog_id: str | None, task_text: str) -> str:
    """Stable work identity: backlog card > Lead's issue ref > hash of Lead's own
    task text (orchestrator preamble and our own ledger block excluded, so the
    same task hashes the same on every attempt)."""
    bid = (backlog_id or "").strip()
    if bid:
        return f"backlog:{bid}"
    from .notice_facts import extract_issue_ref, lead_owned_text

    ref = extract_issue_ref(task_text)
    if ref:
        return f"ref:{ref}"
    text = (lead_owned_text(task_text) or task_text or "").split(BRIEF_MARK)[0]
    norm = _NOISE.sub("", " ".join(text.lower().split()))
    return "task:" + hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:12]


def _ref_of(task_text: str) -> str:
    from .notice_facts import extract_issue_ref

    return extract_issue_ref(task_text) or ""


def _task_tokens(task_text: str) -> list[str]:
    from .notice_facts import lead_owned_text

    text = (lead_owned_text(task_text) or task_text or "").split(BRIEF_MARK)[0]
    text = _NOISE.sub(" ", text.lower())
    return sorted({t.strip("./\\-") for t in _WORD.findall(text)} - _STOP)


def signature_tokens(note: str) -> list[str]:
    """Salient, order-free tokens of a failure note (paths, identifiers, error
    names) with numbers/hashes/ref tags stripped so a reworded or re-timed
    report of the same failure still matches."""
    text = _NOISE.sub(" ", _REF_TAG.sub(" ", (note or "").lower()))
    toks = [t.strip("./\\-") for t in _WORD.findall(text)]
    return sorted({t for t in toks if len(t) > 2 and t not in _STOP})[:40]


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def _load(project: str) -> dict:
    try:
        raw = json.loads(_path(project).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("sequences"), dict):
            return raw
    except (OSError, ValueError):
        pass
    return {"schema_version": SCHEMA_VERSION, "sequences": {}}


def _save(project: str, store: dict) -> None:
    seqs = store["sequences"]
    now = time.time()
    for k in [k for k, s in seqs.items() if now - s.get("updated_ts", 0) > _MAX_AGE_S]:
        seqs.pop(k, None)
    if len(seqs) > _MAX_SEQUENCES:
        keep = sorted(seqs, key=lambda k: seqs[k].get("updated_ts", 0), reverse=True)
        for k in keep[_MAX_SEQUENCES:]:
            seqs.pop(k, None)
    _atomic_write(_path(project), json.dumps(store, ensure_ascii=False, indent=1))


def _first_line(note: str, limit: int = 160) -> str:
    line = next((ln.strip() for ln in (note or "").splitlines() if ln.strip()), "")
    return line[:limit]


@dataclass(frozen=True)
class FixLoopOutcome:
    attempt: int
    decision: FixLoopDecision
    stopped: bool  # tiny/normal ceiling hit — automatic dispatch must stop
    text: str  # Lead-facing block (prior attempts + retry counter / stop notice)


def _render_attempts(seq: dict, upto_last: bool = True) -> str:
    rows = seq["attempts"]
    lines = []
    for i, a in enumerate(rows, 1):
        ts = time.strftime("%H:%M", time.localtime(a.get("ts", 0)))
        who = a.get("role", "?") + (f"/{a['provider']}" if a.get("provider") else "")
        lines.append(f"  {i}. [{ts}] {who} — {a.get('summary', '')}")
    return "\n".join(lines)


def _render(seq: dict, decision: FixLoopDecision, scope: str, evidence: str) -> tuple[str, bool]:
    n = decision.attempt
    prior = _render_attempts(seq)
    if decision.action == "ask_user":
        text = (
            f"🛑 **FIX-LOOP CEILING** — เรื่องเดียวกันล้มเหลวเป็นครั้งที่ {n} "
            f"(เพดาน {MAX_FIX_LOOP_ATTEMPTS} รอบ, scope={scope}) — "
            "**`takkub assign` เรื่องนี้ถูกระบบปฏิเสธจนกว่า user อนุมัติ** แจ้ง user ก่อน\n"
            f"หลักฐานล่าสุด: {evidence}\n"
            f"ความพยายามที่ผ่านมา:\n{prior}\n"
            "ทางวินิจฉัยใหม่ (เลือกอย่างใดอย่างหนึ่ง แล้วให้ user confirm):\n"
            "  • assign reviewer/critic แบบ read-only หา root cause ก่อน (ห้ามแก้) "
            "โดยแนบตารางความพยายามข้างบน\n"
            "  • เปลี่ยน role/provider ที่ต่างจากเดิม พร้อม ledger ข้างบนใน brief "
            "(ห้ามเริ่มเดาใหม่จากศูนย์)\n"
            "  • ตัดขอบเขต/ขอข้อมูลเพิ่มจาก user (log, repro, env จริง)\n"
            "user อนุมัติให้ลองต่อแล้วเท่านั้น: "
            '`takkub assign ... --ack-ceiling "<เหตุผลที่ user อนุมัติ>"` (บันทึกลง events.log)'
        )
        return text, True
    if decision.action == "propose":
        text = (
            f"🔁 fix-loop ครั้งที่ {n} ของเรื่องเดียวกัน (scope=deep → propose + ขอ confirm เสมอ)\n"
            f"ความพยายามที่ผ่านมา:\n{prior}"
        )
        return text, False
    text = (
        f"🔁 fix-loop รอบที่ {n}/{MAX_FIX_LOOP_ATTEMPTS} ของเรื่องเดียวกัน (scope={scope}) — "
        f"รอบที่ {MAX_FIX_LOOP_ATTEMPTS + 1} จะถูกหยุด\n"
        f"ความพยายามที่ผ่านมา:\n{prior}"
    )
    return text, False


def record_failure(
    project: str,
    identity: str,
    note: str,
    *,
    role: str,
    provider: str = "",
    scope: str = "normal",
    attempt_token: str = "",
    task_text: str = "",
) -> FixLoopOutcome:
    """Count one failed attempt and return the ceiling decision. A repeat
    *attempt_token* (same assignment / same shard group) is one attempt, not
    several — shard fan-out and duplicate `done --fail` calls don't inflate it."""
    tokens = signature_tokens(note)
    with _LOCK:
        store = _load(project)
        seqs = store["sequences"]
        best_key, best = None, 0.0
        for k, s in seqs.items():
            if s.get("identity") != identity:
                continue
            j = _jaccard(tokens, s.get("tokens", []))
            if j >= _MATCH_JACCARD and j >= best:
                best_key, best = k, j
        if best_key is None:
            best_key = hashlib.sha1(f"{identity}|{time.time()}|{len(seqs)}".encode()).hexdigest()[
                :10
            ]
            seqs[best_key] = {"identity": identity, "tokens": tokens, "attempts": []}
        seq = seqs[best_key]
        row = {
            "ts": time.time(),
            "role": role,
            "provider": provider,
            "token": attempt_token,
            "summary": _first_line(note),
        }
        same = (
            attempt_token and seq["attempts"] and seq["attempts"][-1].get("token") == attempt_token
        )
        if same:
            seq["attempts"][-1] = row
        else:
            seq["attempts"].append(row)
        seq["tokens"] = sorted(set(seq["tokens"]) | set(tokens))[:60]
        if task_text:
            merged = set(seq.get("task_tokens", [])) | set(_task_tokens(task_text))
            seq["task_tokens"] = sorted(merged)[:120]
            seq["ref"] = _ref_of(task_text)
        seq["updated_ts"] = time.time()
        attempt = len(seq["attempts"])
        decision = check_fix_loop_ceiling(scope, attempt, failure_signature=_first_line(note, 60))
        seq["stopped"] = decision.action == "ask_user"
        try:
            _save(project, store)
        except OSError:
            pass  # ledger trouble never blocks the failure notice itself
    text, stopped = _render(seq, decision, decision_scope(scope), _first_line(note, 300))
    return FixLoopOutcome(attempt, decision, stopped, text)


def decision_scope(scope: str) -> str:
    s = (scope or "normal").strip().lower()
    return s if s in ("tiny", "normal", "deep") else "normal"


def reset(project: str, identity: str) -> int:
    """A passed verification closes every sequence of *identity*; returns how
    many were cleared. Other identities are untouched."""
    with _LOCK:
        store = _load(project)
        seqs = store["sequences"]
        drop = [k for k, s in seqs.items() if s.get("identity") == identity]
        for k in drop:
            seqs.pop(k, None)
        if drop:
            try:
                _save(project, store)
            except OSError:
                pass
        return len(drop)


def brief_block(project: str, identity: str) -> str:
    """Attempt ledger for a reassigned pane's task brief (provider-neutral
    plain text). Empty when this identity has no recorded failures."""
    with _LOCK:
        seqs = [s for s in _load(project)["sequences"].values() if s.get("identity") == identity]
    seqs = [s for s in seqs if s.get("attempts")]
    if not seqs:
        return ""
    parts = [
        f"{BRIEF_MARK} (fix-loop ledger — อย่าเริ่มเดาใหม่จากศูนย์)",
        f"เพดาน {MAX_FIX_LOOP_ATTEMPTS} รอบต่อเรื่องเดียวกัน — ถ้ายังไม่ผ่านให้รายงาน "
        "`done --fail` พร้อมหลักฐานใหม่ อย่าวนแก้ทางเดิม",
    ]
    for s in seqs:
        parts.append(_render_attempts(s))
    return "\n".join(parts)


def match_identity(project: str, task_text: str) -> str:
    """Identity of an existing failure sequence this (card-less) task text
    continues — same issue ref, or Jaccard-similar wording — else "". Lets a
    re-assign without --backlog keep counting instead of restarting at 0."""
    ref = _ref_of(task_text)
    toks = _task_tokens(task_text)
    best_id, best = "", 0.0
    for s in _load(project)["sequences"].values():
        if not s.get("attempts"):
            continue
        if ref and s.get("ref") == ref:
            score = 1.0
        elif toks and s.get("task_tokens"):
            score = _jaccard(toks, s["task_tokens"])
            if score < _MATCH_JACCARD:
                continue
        else:
            continue
        if score > best:
            best_id, best = s.get("identity", ""), score
    return best_id


def ceiling_refusal(project: str, identity: str, role: str) -> str:
    """Refusal text when *identity* sits at the tiny/normal ceiling and *role*
    is a fixer; "" when the assign may proceed (verify/diagnosis roles, deep
    scope and untouched work are never refused)."""
    if role in CEILING_EXEMPT_ROLES:
        return ""
    hit = [
        s
        for s in _load(project)["sequences"].values()
        if s.get("identity") == identity and s.get("stopped")
    ]
    if not hit:
        return ""
    seq = max(hit, key=lambda s: s.get("updated_ts", 0))
    return (
        f"🛑 assign ถูกปฏิเสธ (fix-loop ceiling, #762): เรื่องนี้ล้มเหลวซ้ำ "
        f"{len(seq['attempts'])} ครั้งแล้ว (เพดาน {MAX_FIX_LOOP_ATTEMPTS} รอบ)\n"
        f"ความพยายามที่ผ่านมา:\n{_render_attempts(seq)}\n"
        "ทางใหม่: assign reviewer/critic แบบ read-only หา root cause, เปลี่ยนวิธี/ขอข้อมูลเพิ่มจาก user "
        '— หรือถ้า user อนุมัติให้ลองต่อ ใส่ --ack-ceiling "<เหตุผล>"'
    )
