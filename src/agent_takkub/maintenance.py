"""`takkub ma` — the cockpit's own maintenance sweep.

One command that walks the standing checklist instead of the operator
remembering it: open issues, open PRs and their CI, open code-scanning alerts
(GitHub Security tab — never surfaced by `gh issue list`), what the running cockpit's
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
import os
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW
from .config import EVENTS_LOG

# #464: default `⚠ บ่อย` (too frequent) callout threshold for the Lead noise
# report below — a `lead_notice` kind averaging more than this many
# occurrences per hour gets flagged with the ready-to-run `takkub issue`
# command. Override with the env var when a project's normal traffic runs
# hotter/cooler than this default.
_DEFAULT_NOISE_PER_HOUR = 4.0
_NOISE_TOP_N = 10

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
    "task_deliver_account_pending": "ส่งใบงานไม่ได้เพราะ provider ยังไม่ผ่าน account verification (ไม่ใช่ auth)",
    "account_pending_warned": "provider ติด account verification gate (#346)",
    "rate_limit_detected": "ชน rate limit",
    "verify_failed": "QA/verify รายงาน FAIL",
    "verify_blocked": "QA/verify รายงานติด blocker (ต้องให้เจ้าของทำ)",
    "pane_limit_parked": "pane ถูกพักเพราะชนเพดาน",
    "ready_marker_stale_prolonged": (
        "pane เงียบต่อเนื่องหลาย cooldown ไม่มี marker จับได้เลย (#343 — น่าจะ provider ค้าง/ล่ม)"
    ),
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
    "pane_guard_denied": "pane_guard บล็อกคำสั่ง (#466)",
    "watchdog_quiet_but_alive": (
        "marker จับ pane ไม่ได้ แต่ liveness (#468) เห็น transcript/child process ยังทำงาน — "
        "ไม่ส่ง notice ให้ Lead"
    ),
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


# ── 2b. code scanning (GitHub security tab) ──────────────────────────────────

_CS_TITLE = "Code scanning alert (GitHub security)"
# Ranking so the loudest finding leads the list — GitHub returns newest-first,
# which buries a `critical` behind ten fresh `note`s.
_CS_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _gh_repo_slug(repo_dir: Path) -> str | None:
    ok, out = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], cwd=repo_dir
    )
    return out.strip() if ok and out.strip() else None


def check_code_scanning(repo_dir: Path, limit: int = 100) -> Check:
    """Open alerts under the repo's Security → Code scanning tab.

    These never show up in `gh issue list` and CI stays green while they pile
    up, so without this section the sweep would happily say "พร้อม ship" over a
    `high`-severity CodeQL finding nobody has looked at (the case that
    prompted it: alert #43, an overly-permissive `os.open` mode)."""
    slug = _gh_repo_slug(repo_dir)
    if slug is None:
        return Check(
            "code_scanning", _CS_TITLE, "error", "หา owner/repo ของ remote ไม่ได้ (gh repo view)"
        )
    ok, out = _run(
        [
            "gh",
            "api",
            f"repos/{slug}/code-scanning/alerts?state=open&per_page={limit}",
        ],
        cwd=repo_dir,
    )
    if not ok:
        low = out.lower()
        if "no analysis found" in low or "not enabled" in low or "404" in low:
            return Check("code_scanning", _CS_TITLE, "skip", "repo นี้ยังไม่ได้เปิด code scanning")
        return Check("code_scanning", _CS_TITLE, "error", out)
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError:
        return Check("code_scanning", _CS_TITLE, "error", "อ่านผลจาก gh api ไม่ได้")
    if not isinstance(rows, list):
        return Check("code_scanning", _CS_TITLE, "error", "gh api ตอบรูปแบบที่ไม่รู้จัก")
    if not rows:
        return Check("code_scanning", _CS_TITLE, "ok", "ไม่มี alert เปิดค้าง")

    def _sev(row: dict) -> str:
        rule = row.get("rule") or {}
        return str(rule.get("security_severity_level") or rule.get("severity") or "").lower()

    rows.sort(key=lambda r: (_CS_SEVERITY_RANK.get(_sev(r), 9), -int(r.get("number") or 0)))
    counts = Counter(_sev(r) or "unknown" for r in rows)
    details: list[str] = []
    for row in rows:
        rule = row.get("rule") or {}
        loc = ((row.get("most_recent_instance") or {}).get("location")) or {}
        where = ""
        if loc.get("path"):
            where = f" — {loc.get('path')}"
            if loc.get("start_line"):
                where += f":{loc.get('start_line')}"
        tool = ((row.get("tool") or {}).get("name")) or ""
        details.append(
            f"#{row.get('number')} [{_sev(row) or '?'}] "
            f"{str(rule.get('description') or rule.get('id') or '')[:70]}{where}"
            + (f" · {tool}" if tool else "")
            + f" · {row.get('html_url') or ''}"
        )
    ordered = [
        f"{k} {counts[k]}" for k in sorted(counts, key=lambda k: _CS_SEVERITY_RANK.get(k, 9))
    ]
    summary = f"{len(rows)} ใบ ({' · '.join(ordered)})"
    return Check(
        "code_scanning",
        _CS_TITLE,
        "attention",
        summary,
        details,
        {"numbers": [r.get("number") for r in rows], "by_severity": dict(counts)},
    )


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
    stall_top_frames: Counter[str] = Counter()  # #452: closest frame of ≥2s stalls
    stall_top_busy_thread: Counter[str] = Counter()  # #452 follow-up: culprit thread
    recover_reasons: Counter[str] = Counter()  # #422: `<event>:<reason>` buckets
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
                    if name.endswith("_pane_recover"):
                        recover_reasons[str(rec.get("reason") or "unclassified")] += 1
                elif name in _WARN_EVENTS:
                    warn[name] += 1
                if name == "main_thread_stall":
                    ms = int(rec.get("duration_ms") or 0)
                    worst_stall = max(worst_stall, ms)
                    if ms >= _STALL_CALLOUT_MS:
                        stall_callouts.append(
                            f"{ts:%H:%M} UI ค้าง {ms / 1000:.1f}s (pane {rec.get('active_panes')})"
                        )
                        busy_threads = rec.get("busy_threads")
                        if isinstance(busy_threads, list) and busy_threads:
                            stall_top_busy_thread[str(busy_threads[0])] += 1
                        stack = rec.get("stack")
                        if isinstance(stack, list) and stack:
                            stall_top_frames[str(stack[0])] += 1
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
    if recover_reasons:
        details.append(
            "   เหตุผล recovery: "
            + " · ".join(f"{k} ×{v}" for k, v in recover_reasons.most_common())
        )
    details += [f"🟡 {_WARN_EVENTS[k]} ×{v}" for k, v in warn.most_common()]
    details += stall_callouts[-5:]
    if stall_top_busy_thread:
        entry, count = stall_top_busy_thread.most_common(1)[0]
        thread_name, sep, frame = entry.partition(": ")
        if not sep:
            thread_name, frame = "?", entry
        details.append(
            f"ส่วนใหญ่ค้างเพราะ thread {thread_name} ที่ {frame}"
            f" (×{count}/{sum(stall_top_busy_thread.values())})"
        )
    elif stall_top_frames:
        frame, count = stall_top_frames.most_common(1)[0]
        details.append(f"ส่วนใหญ่ค้างที่ {frame} (×{count}/{sum(stall_top_frames.values())})")
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


# ── 3b. unsent local issues ──────────────────────────────────────────────────


def check_local_issue_backlog() -> Check:
    """Cockpit bugs that never made it to GitHub and are sitting on disk (#297).

    The fallback store warns on stderr, which nothing in a GUI-hosted cockpit
    ever shows — so a backlog could grow indefinitely while looking like
    everything had been reported. Surfacing it here makes it something the
    operator actually sees.
    """
    from .config import DATA_HOME

    store = DATA_HOME / ".takkub_issues.json"
    if not store.is_file():
        return Check("local_issues", "Issue ที่ค้างในเครื่อง (ยังไม่ถึง GitHub)", "ok", "ไม่มี")
    try:
        rows = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Check(
            "local_issues",
            "Issue ที่ค้างในเครื่อง (ยังไม่ถึง GitHub)",
            "error",
            f"อ่าน {store} ไม่ได้: {exc}",
        )
    if not isinstance(rows, list):
        rows = []
    open_rows = [r for r in rows if isinstance(r, dict) and r.get("status") == "open"]
    if not open_rows:
        return Check("local_issues", "Issue ที่ค้างในเครื่อง (ยังไม่ถึง GitHub)", "ok", "ไม่มี")
    details = [f"#{r.get('number')} {str(r.get('title') or '')[:70]}" for r in open_rows[:10]]
    details.append(f"ไฟล์: {store}")
    return Check(
        "local_issues",
        "Issue ที่ค้างในเครื่อง (ยังไม่ถึง GitHub)",
        "attention",
        f"{len(open_rows)} ใบ — ไม่มีใครนอกเครื่องนี้เห็น",
        details,
        {"count": len(open_rows)},
    )


# ── 3c. Lead noise (24h) ──────────────────────────────────────────────────────


def _noise_threshold_per_hour(explicit: float | None) -> float:
    if explicit is not None:
        return explicit
    try:
        return float(os.environ.get("TAKKUB_NOISE_PER_HOUR", _DEFAULT_NOISE_PER_HOUR))
    except ValueError:
        return _DEFAULT_NOISE_PER_HOUR


def _other_cockpit_events_log(this_log: Path) -> Path | None:
    """The other cockpit's events.log, when this checkout can run two at once
    (a dev checkout + a packaged/prod install each get their own DATA_HOME —
    see #two-cockpits-two-data-homes) — mirrors `cli._instance_banner`'s own
    dev/prod pairing so a kind noisy only on the OTHER instance still shows
    up here. `None` when there is no meaningful "other" (this IS the paired
    path, or already the same file)."""
    from .config import DATA_HOME
    from .config import REPO_ROOT as _REPO_ROOT

    is_dev = DATA_HOME == _REPO_ROOT
    other = (
        (Path.home() / ".agent-takkub" / "runtime" / "events.log")
        if is_dev
        else (Path(_REPO_ROOT) / "runtime" / "events.log")
    )
    return None if other == this_log else other


def check_lead_noise(
    log_path: Path,
    since_hours: float = 24.0,
    *,
    threshold_per_hour: float | None = None,
    now: datetime | None = None,
) -> Check:
    """Count every `lead_notice` event (#464) by `kind` over *since_hours* —
    the countable half of the Lead noise audit. `_notify_lead` logs one of
    these on every call it makes (see `lead_inbox._log_lead_notice`), so this
    is a direct tally of what actually interrupted Lead, not a guess. Reads
    BOTH cockpits' events.log when both exist, since a Lead pane can be alive
    in either one."""
    candidates = {log_path, _other_cockpit_events_log(log_path)}
    logs = [p for p in candidates if p is not None and p.is_file()]
    if not logs:
        return Check("lead_noise", "Lead noise (24h)", "skip", f"ไม่มีไฟล์ {log_path}")

    cutoff = (now or datetime.now()) - timedelta(hours=since_hours)
    counts: Counter[str] = Counter()
    emitters: dict[str, Counter[str]] = {}
    previews: dict[str, str] = {}
    total = 0
    for path in logs:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("event") != "lead_notice":
                        continue
                    ts = _parse_ts(rec.get("ts"))
                    if ts is None or ts < cutoff:
                        continue
                    kind = str(rec.get("kind") or "unknown")
                    counts[kind] += 1
                    total += 1
                    emitters.setdefault(kind, Counter())[str(rec.get("emitter") or "?")] += 1
                    preview = str(rec.get("preview") or "")
                    if preview:
                        previews[kind] = preview
        except OSError:
            continue

    if total == 0:
        return Check(
            "lead_noise",
            "Lead noise (24h)",
            "ok",
            f"ไม่มี lead_notice ใน {since_hours:.0f} ชม.ล่าสุด",
        )

    threshold = _noise_threshold_per_hour(threshold_per_hour)
    hours = max(since_hours, 1e-9)
    details: list[str] = []
    loud: list[tuple[str, str, int]] = []
    for kind, count in counts.most_common(_NOISE_TOP_N):
        rate = count / hours
        top_emitters = emitters.get(kind)
        emitter = top_emitters.most_common(1)[0][0] if top_emitters else "?"
        preview = previews.get(kind, "")
        preview_note = f' — "{preview}"' if preview else ""
        is_loud = rate > threshold
        if is_loud:
            loud.append((kind, emitter, count))
        flag = " ⚠ บ่อย" if is_loud else ""
        details.append(f"{kind} ×{count} ({rate:.1f}/ชม.) @ {emitter}{preview_note}{flag}")
    for kind, emitter, count in loud:
        details.append(
            f'  → takkub issue "notice {kind} ที่ {emitter} บอกบ่อย '
            f'({count}/{since_hours:.0f}h) ไม่จำเป็น เพราะ <ระบุเหตุผล>"'
        )

    status = "attention" if loud else "ok"
    summary = f"{total} notice / {since_hours:.0f} ชม. ({len(counts)} kind)"
    if loud:
        summary += f" — {len(loud)} kind เกิน {threshold:g}/ชม."
    return Check(
        "lead_noise",
        "Lead noise (24h)",
        status,
        summary,
        details,
        {
            "counts": dict(counts),
            "threshold_per_hour": threshold,
            "loud_kinds": [k for k, _e, _c in loud],
        },
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
    scanning = by_key.get("code_scanning")
    if scanning is not None and scanning.status == "attention":
        steps.append(
            "ปิด code scanning alert: อ่านโค้ดตรงบรรทัดที่ชี้ก่อน — fix จริงถ้าเป็นช่องโหว่ "
            "หรือ dismiss พร้อมเหตุผลบน GitHub ถ้าเป็น false positive (ห้ามปล่อยค้างเงียบ)"
        )
    backlog = by_key.get("local_issues")
    if backlog is not None and backlog.status == "attention":
        steps.append("ส่ง issue ที่ค้างในเครื่องขึ้น GitHub ก่อน — ตอนนี้ยังไม่มีใครนอกเครื่องนี้เห็น")
    noise = by_key.get("lead_noise")
    if noise is not None and noise.status == "attention":
        steps.append(
            "อ่าน 'Lead noise' — kind ที่ขึ้น ⚠ บ่อย เปิดใบด้วยคำสั่งที่พิมพ์ไว้ให้แล้ว "
            "ชี้ emitter ตรงๆ ห้ามแค่บ่น (ดู docs/lead/role-and-workflow.md)"
        )
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
    noise_threshold_per_hour: float | None = None,
) -> MaintenanceReport:
    checks: list[Check] = []
    if include_network:
        checks.append(check_issues(repo_dir))
        checks.append(check_prs(repo_dir))
        checks.append(check_code_scanning(repo_dir))
    else:
        checks.append(Check("issues", "Issue ที่เปิดค้าง", "skip", "ข้าม (--no-net)"))
        checks.append(Check("prs", "Pull request ที่เปิดค้าง", "skip", "ข้าม (--no-net)"))
        checks.append(Check("code_scanning", _CS_TITLE, "skip", "ข้าม (--no-net)"))
    checks.append(scan_events(log_path or EVENTS_LOG, since_hours=since_hours))
    checks.append(check_local_issue_backlog())
    checks.append(
        check_lead_noise(
            log_path or EVENTS_LOG,
            since_hours=since_hours,
            threshold_per_hour=noise_threshold_per_hour,
        )
    )
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
