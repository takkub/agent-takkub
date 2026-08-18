"""`takkub ma` — the cockpit's own maintenance sweep.

One command that walks the standing checklist instead of the operator
remembering it: open issues, open PRs and their CI, what the running cockpit's
own `events.log` says went wrong recently, and whether this checkout is in a
state where a fix could actually be shipped.

Deliberately **read-only**. Steps 4 and 5 of the operator's checklist ("fix
what the checks found", "push, wait for CI, publish") are judgement work that
belongs to Lead, not to a CLI verb — so this command's last section hands Lead
an ordered plan built from what it actually found, rather than pretending a
script can decide which findings are worth acting on.

Import-boundary note: this module stays a leaf (config + subprocess only) so
`cli.py` can call it without pulling the orchestrator engine into the CLI
process — the `cli-ipc-boundary` contract in `pyproject.toml`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW
from .config import EVENTS_LOG

# Events that mean "something went wrong", grouped by how loudly they should be
# read. Names are taken from what the running cockpit actually writes (verified
# against a live events.log), not from guessing at the emitter side.
_SEVERE_EVENTS: dict[str, str] = {
    "stuck_pane_recover": "watchdog ฆ่า+respawn pane ที่คิดว่าค้าง",
    "stuck_recover_respawn": "respawn หลัง stuck-recover",
    "no_content_pane_recover": "pane ไม่พ่นอะไรเลยจน watchdog เข้าแทรก",
    "no_content_pane_respawned": "respawn เพราะ pane ไม่มี content",
    "task_delivery_failed": "ส่งใบงานไม่สำเร็จ",
    "delivery_boot_timeout_failed": "ใบงานตายเพราะ pane boot ไม่ทัน",
    "task_deliver_auth_failure": "ส่งใบงานไม่ได้เพราะ auth ของ provider",
    "auth_failure_warned": "provider auth มีปัญหา",
    "rate_limit_detected": "ชน rate limit",
    "verify_failed": "QA/verify รายงาน FAIL",
    "verify_blocked": "QA/verify รายงานติด blocker (ต้องให้เจ้าของทำ)",
    "pane_limit_parked": "pane ถูกพักเพราะชนเพดาน",
}
_WARN_EVENTS: dict[str, str] = {
    "main_thread_stall": "UI ค้าง (main thread ติด I/O)",
    "task_delivery_expired": "ใบงานหมดอายุก่อนถึงมือ",
    "delivery_stale_reaped": "ใบงานค้างถูกเก็บกวาด",
    "delivery_unconfirmed": "ส่งใบงานแล้วยืนยันไม่ได้",
    "task_deliver_boot_stall": "pane boot ช้าจนใบงานต้องรอ",
    "task_deliver_timeout_no_session": "ส่งใบงานไม่ได้ ไม่มี session",
    "task_deliver_blocked_on_prompt": "ใบงานติด prompt ที่ค้างอยู่",
    "close_kills_live_children": "ปิด pane ทั้งที่ยังมี process ทำงานอยู่",
    "ready_marker_possibly_stale": "อ่านสถานะ ready ของ pane ไม่ชัด",
    "stuck_recover_deferred_live_children": "watchdog เลื่อนการ respawn เพราะงานยังเดิน",
    "stuck_recover_live_children_grace_expired": "หมดเวลาผ่อนผัน แล้ว respawn ทั้งที่ยังมี process",
    "delivery_kept_undelivered_on_send": "send แทรกใบงานที่ยังไม่ถึงมือ",
}
# Stall durations at or above this are worth naming individually rather than
# just counting — below it, a stall is background noise on a busy box.
_STALL_CALLOUT_MS = 2000


@dataclass
class Check:
    """One checklist line: what was looked at, and what came back."""

    key: str
    title: str
    status: str  # "ok" | "attention" | "skip" | "error"
    summary: str
    details: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)


@dataclass
class MaintenanceReport:
    checks: list[Check]
    actions: list[str]
    since_hours: float

    @property
    def needs_attention(self) -> list[Check]:
        return [c for c in self.checks if c.status == "attention"]

    def to_dict(self) -> dict:
        return {
            "since_hours": self.since_hours,
            "checks": [
                {
                    "key": c.key,
                    "title": c.title,
                    "status": c.status,
                    "summary": c.summary,
                    "details": c.details,
                    "data": c.data,
                }
                for c in self.checks
            ],
            "actions": self.actions,
        }


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 60.0) -> tuple[bool, str]:
    """Run *cmd*, returning `(ok, output)`. Never raises — a maintenance sweep
    that dies because one tool is missing is worse than one that reports it."""
    exe = shutil.which(cmd[0])
    if exe is None:
        return False, f"ไม่พบคำสั่ง {cmd[0]} บนเครื่องนี้"
    try:
        proc = subprocess.run(
            [exe, *cmd[1:]],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            # 0 on non-Windows; keeps the pythonw-hosted GUI from flashing a
            # conhost window every time this sweep shells out.
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, f"{cmd[0]} ไม่ตอบใน {timeout:.0f}s"
    except OSError as exc:
        return False, f"{cmd[0]} รันไม่ได้: {exc}"
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return False, (proc.stderr or out or f"exit {proc.returncode}").strip()
    return True, out


# ── 1. issues ────────────────────────────────────────────────────────────────


def check_issues(repo_dir: Path, limit: int = 50) -> Check:
    ok, out = _run(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,createdAt,labels",
        ],
        cwd=repo_dir,
    )
    if not ok:
        return Check("issues", "Issue ที่เปิดค้าง", "error", out)
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError:
        return Check("issues", "Issue ที่เปิดค้าง", "error", "อ่านผลจาก gh ไม่ได้")
    if not rows:
        return Check("issues", "Issue ที่เปิดค้าง", "ok", "ไม่มี issue เปิดค้าง")
    now = datetime.now().astimezone()
    details = []
    for row in rows:
        age = ""
        created = str(row.get("createdAt") or "")
        try:
            days = (now - datetime.fromisoformat(created.replace("Z", "+00:00"))).days
            age = f" · ค้าง {days} วัน" if days > 0 else " · วันนี้"
        except ValueError:
            pass
        details.append(f"#{row.get('number')} {str(row.get('title') or '')[:80]}{age}")
    return Check(
        "issues",
        "Issue ที่เปิดค้าง",
        "attention",
        f"{len(rows)} ใบ",
        details,
        {"numbers": [r.get("number") for r in rows]},
    )


# ── 2. pull requests ─────────────────────────────────────────────────────────


def check_prs(repo_dir: Path, limit: int = 30) -> Check:
    ok, out = _run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,isDraft,mergeable,statusCheckRollup,author",
        ],
        cwd=repo_dir,
    )
    if not ok:
        return Check("prs", "Pull request ที่เปิดค้าง", "error", out)
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError:
        return Check("prs", "Pull request ที่เปิดค้าง", "error", "อ่านผลจาก gh ไม่ได้")
    if not rows:
        return Check("prs", "Pull request ที่เปิดค้าง", "ok", "ไม่มี PR เปิดค้าง")
    details, red = [], 0
    for row in rows:
        checks = row.get("statusCheckRollup") or []
        failed = [
            c
            for c in checks
            if str(c.get("conclusion") or "").upper() in {"FAILURE", "TIMED_OUT", "CANCELLED"}
        ]
        pending = [c for c in checks if not c.get("conclusion")]
        if failed:
            red += 1
            ci = f"CI แดง {len(failed)} job"
        elif pending:
            ci = f"CI กำลังรัน ({len(pending)})"
        elif checks:
            ci = "CI เขียว"
        else:
            ci = "ไม่มี CI"
        draft = " · draft" if row.get("isDraft") else ""
        mergeable = str(row.get("mergeable") or "")
        conflict = " · มี conflict" if mergeable == "CONFLICTING" else ""
        details.append(
            f"#{row.get('number')} {str(row.get('title') or '')[:60]} — {ci}{draft}{conflict}"
        )
    summary = f"{len(rows)} ใบ" + (f" · CI แดง {red} ใบ" if red else "")
    return Check("prs", "Pull request ที่เปิดค้าง", "attention", summary, details)


# ── 3. runtime log ───────────────────────────────────────────────────────────


def _parse_ts(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def scan_events(log_path: Path, since_hours: float = 24.0, now: datetime | None = None) -> Check:
    """Read the cockpit's own event log and report what went wrong recently."""
    if not log_path.is_file():
        return Check("logs", "Log ของ cockpit ที่รันอยู่", "skip", f"ไม่มีไฟล์ {log_path}")
    cutoff = (now or datetime.now()) - timedelta(hours=since_hours)
    severe: Counter[str] = Counter()
    warn: Counter[str] = Counter()
    worst_stall = 0
    stall_callouts: list[str] = []
    total = 0
    try:
        with log_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(rec.get("ts"))
                if ts is None or ts < cutoff:
                    continue
                total += 1
                name = str(rec.get("event") or "")
                if name in _SEVERE_EVENTS:
                    severe[name] += 1
                elif name in _WARN_EVENTS:
                    warn[name] += 1
                if name == "main_thread_stall":
                    ms = int(rec.get("duration_ms") or 0)
                    worst_stall = max(worst_stall, ms)
                    if ms >= _STALL_CALLOUT_MS:
                        stall_callouts.append(
                            f"{ts:%H:%M} UI ค้าง {ms / 1000:.1f}s (pane {rec.get('active_panes')})"
                        )
    except OSError as exc:
        return Check("logs", "Log ของ cockpit ที่รันอยู่", "error", f"อ่าน log ไม่ได้: {exc}")

    if total == 0:
        return Check(
            "logs",
            "Log ของ cockpit ที่รันอยู่",
            "ok",
            f"ไม่มี event ใน {since_hours:.0f} ชม.ล่าสุด (cockpit ไม่ได้รัน?)",
        )
    details = [f"🔴 {_SEVERE_EVENTS[k]} ×{v}" for k, v in severe.most_common()]
    details += [f"🟡 {_WARN_EVENTS[k]} ×{v}" for k, v in warn.most_common()]
    details += stall_callouts[-5:]
    if worst_stall:
        details.append(f"UI ค้างนานสุด {worst_stall / 1000:.1f}s")
    status = "attention" if (severe or warn) else "ok"
    summary = (
        f"{total} event / {since_hours:.0f} ชม. — "
        f"หนัก {sum(severe.values())} · เตือน {sum(warn.values())}"
        if status == "attention"
        else f"{total} event / {since_hours:.0f} ชม. — ไม่มีสัญญาณผิดปกติ"
    )
    return Check(
        "logs",
        "Log ของ cockpit ที่รันอยู่",
        status,
        summary,
        details,
        {"severe": dict(severe), "warn": dict(warn), "worst_stall_ms": worst_stall},
    )


# ── 4. repo shippability ─────────────────────────────────────────────────────


def check_repo(repo_dir: Path) -> Check:
    """Can a fix actually be shipped from this checkout right now?"""
    details: list[str] = []
    problems = 0

    ok, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
    if not ok:
        return Check("repo", "สภาพ repo (พร้อม ship ไหม)", "error", branch)
    details.append(f"branch: {branch}")

    ok, dirty = _run(["git", "status", "--porcelain"], cwd=repo_dir)
    if ok and dirty:
        problems += 1
        details.append(f"ยังไม่ commit {len(dirty.splitlines())} ไฟล์")
    elif ok:
        details.append("working tree สะอาด")

    _run(["git", "fetch", "--quiet"], cwd=repo_dir, timeout=45)
    ok, counts = _run(
        ["git", "rev-list", "--left-right", "--count", f"{branch}...origin/{branch}"], cwd=repo_dir
    )
    if ok and counts:
        parts = counts.split()
        if len(parts) == 2:
            ahead, behind = parts
            if ahead != "0":
                problems += 1
                details.append(f"ยังไม่ push {ahead} commit")
            if behind != "0":
                problems += 1
                details.append(f"ตามหลัง origin {behind} commit")
            if ahead == "0" and behind == "0":
                details.append("ตรงกับ origin")

    ok, ci = _run(
        [
            "gh",
            "run",
            "list",
            "--branch",
            branch,
            "--limit",
            "1",
            "--json",
            "conclusion,status,workflowName",
        ],
        cwd=repo_dir,
    )
    if ok:
        try:
            rows = json.loads(ci or "[]")
        except json.JSONDecodeError:
            rows = []
        if rows:
            row = rows[0]
            conclusion = str(row.get("conclusion") or row.get("status") or "?")
            details.append(f"CI ล่าสุดของ {branch}: {conclusion}")
            if conclusion.lower() not in {"success", "completed"}:
                problems += 1

    return Check(
        "repo",
        "สภาพ repo (พร้อม ship ไหม)",
        "attention" if problems else "ok",
        "พร้อม ship" if not problems else f"ติด {problems} เรื่อง",
        details,
        {"branch": branch},
    )


# ── plan ─────────────────────────────────────────────────────────────────────


def build_actions(checks: list[Check]) -> list[str]:
    """The ordered plan, built only from what was actually found.

    Steps 4-5 of the operator checklist (fix / push / CI / publish) live here
    as instructions rather than automation on purpose: deciding WHICH findings
    deserve a fix is the judgement this command exists to inform, not replace.

    Numbering is applied last so the list never shows gaps when a section came
    back clean — a plan that jumps from 1 to 4 reads like something was lost.
    """
    by_key = {c.key: c for c in checks}
    steps: list[str] = []

    logs = by_key.get("logs")
    if logs is not None and logs.status == "attention":
        severe = bool((logs.data or {}).get("severe"))
        if severe:
            steps.append(
                "อ่าน 🔴 ใน 'Log ของ cockpit' ก่อน — เป็นของที่พังกับระบบจริงในช่วงนี้ "
                "หลักฐานอยู่ครบใน events.log แล้ว ไม่ต้องรอให้ใครมารายงาน"
            )
        else:
            steps.append(
                "ดู 🟡 ใน 'Log ของ cockpit' — ยังไม่ถึงขั้นพัง แต่ตัวที่นับได้เยอะผิดปกติ "
                "มักเป็นบั๊กที่กำลังก่อตัว (เทียบกับรอบก่อนด้วย `--since-hours`)"
            )
    prs = by_key.get("prs")
    if prs is not None and prs.status == "attention":
        steps.append("เคลียร์ PR: CI แดง = ดู log ก่อนตัดสิน · CI เขียว = review แล้ว merge")
    issues = by_key.get("issues")
    if issues is not None and issues.status == "attention":
        steps.append("เลือก issue ที่จะปิดรอบนี้ — พิสูจน์ก่อนแก้ ทุกใบต้องมีหลักฐาน")
    steps.append("แก้ตามที่เลือก + เขียนเทสกันถอย + รัน full suite ครั้งเดียวตอนท้าย")
    repo = by_key.get("repo")
    if repo is not None and repo.status == "attention":
        steps.append("commit + push (repo ยังไม่ตรงกับ origin — ดูรายละเอียดข้างบน)")
    else:
        steps.append("commit + push")
    steps.append("รอ CI เขียวทั้ง matrix (windows + macos) แล้วค่อย publish — ห้าม publish ก่อน")
    return [f"{i}. {s}" for i, s in enumerate(steps, start=1)]


def run_maintenance(
    repo_dir: Path,
    *,
    since_hours: float = 24.0,
    include_network: bool = True,
    log_path: Path | None = None,
) -> MaintenanceReport:
    checks: list[Check] = []
    if include_network:
        checks.append(check_issues(repo_dir))
        checks.append(check_prs(repo_dir))
    else:
        checks.append(Check("issues", "Issue ที่เปิดค้าง", "skip", "ข้าม (--no-net)"))
        checks.append(Check("prs", "Pull request ที่เปิดค้าง", "skip", "ข้าม (--no-net)"))
    checks.append(scan_events(log_path or EVENTS_LOG, since_hours=since_hours))
    checks.append(
        check_repo(repo_dir)
        if include_network
        else Check("repo", "สภาพ repo (พร้อม ship ไหม)", "skip", "ข้าม (--no-net)")
    )
    return MaintenanceReport(checks, build_actions(checks), since_hours)


_STATUS_GLYPH = {"ok": "✅", "attention": "⚠️ ", "skip": "⏭️ ", "error": "❌"}


def render_report(report: MaintenanceReport) -> str:
    lines = [
        "",
        "═══ takkub ma — maintenance sweep ═══",
        f"ช่วงเวลาที่ดู log: {report.since_hours:.0f} ชั่วโมงล่าสุด · {time.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    for index, check in enumerate(report.checks, start=1):
        glyph = _STATUS_GLYPH.get(check.status, "•")
        lines.append(f"{glyph} {index}. {check.title} — {check.summary}")
        for detail in check.details:
            lines.append(f"      · {detail}")
        lines.append("")
    attention = report.needs_attention
    lines.append("─── ทำต่อ ───")
    if not attention:
        lines.append("ทุกอย่างเขียว ไม่มีอะไรต้องทำ")
    else:
        lines.extend(report.actions)
    lines.append("")
    return "\n".join(lines)
