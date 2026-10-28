"""Cross-project read-only report for the PM pane (`takkub pm summary`).

The Lead sees one project; the owner's PM question is about all of them —
"what did every project do in the last N days, and what is still left open?".
Everything here is a plain read of data the cockpit already keeps per project:

- task ledger      runtime/tasks/<project>/.ledger-state.json   (task_ledger)
- backlog cards    runtime/backlog/<project>/backlog.json        (backlog)
- done reports     runtime/sessions/<YYYY-MM-DD>/<project>/*.md  (done notes)

Never writes, never talks to the cockpit server, never touches a project's own
repo — the PM pane may run while the cockpit is busy, or with no cockpit at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import backlog, config, task_ledger

# Ledger row statuses (task_ledger): working = assigned, no done yet.
_OPEN_ROW = "working"
_FAILED_ROW = "fail"
_DONE_ROWS = frozenset({"ok"})
_NOTE_FIELD_RE = re.compile(
    r"\b(STATUS|REMAINING|BLOCKED|NEXT)\s*:\s*(.+?)(?=;\s*[A-Z_]{4,}\s*:|$)", re.S
)
_SKILL_TAG_RE = re.compile(r"\[skill:[^\]]*\]")
_POINTER_RE = re.compile(r"อ่านไฟล์นี้:\s*\"([^\"]+)\"")
_LEAD_TAGS_RE = re.compile(r"^(?:\s*\[[^\]]*\]\s*)+")


def headline(text: str, limit: int = 140) -> str:
    """One readable line for a task/card. Most briefs are just a pointer
    (`อ่านไฟล์นี้: "<brief.md>"`) — follow it and use the brief's first line;
    keep the pointer when the file is gone (scratchpads get cleaned)."""
    text = text or ""
    m = _POINTER_RE.search(text)
    if m:
        try:
            for line in Path(m.group(1)).read_text(encoding="utf-8", errors="replace").splitlines():
                line = _LEAD_TAGS_RE.sub("", line).strip().lstrip("#").strip()
                if line and not line.startswith(("---", "อ่านไฟล์นี้")):
                    text = line
                    break
        except OSError:
            pass
    text = " ".join(_LEAD_TAGS_RE.sub("", text).split()).lstrip("#").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class TaskRow:
    day: str
    role: str
    status: str
    summary: str
    assigned: str = ""
    done: str = ""


@dataclass
class DoneNote:
    day: str
    time: str
    role: str
    path: str
    status: str = ""
    remaining: str = ""


@dataclass
class ProjectReport:
    name: str
    open_tasks: list[TaskRow] = field(default_factory=list)
    failed_tasks: list[TaskRow] = field(default_factory=list)
    done_count: int = 0
    other_count: int = 0
    open_cards: list[dict] = field(default_factory=list)
    notes: list[DoneNote] = field(default_factory=list)
    last_day: str = ""

    @property
    def has_activity(self) -> bool:
        return bool(
            self.open_tasks
            or self.failed_tasks
            or self.done_count
            or self.other_count
            or self.notes
        )

    @property
    def has_pending(self) -> bool:
        return bool(
            self.open_tasks
            or self.failed_tasks
            or self.open_cards
            or any(n.remaining for n in self.notes)
        )


def _window(days: int, today: date | None = None) -> tuple[str, str]:
    today = today or date.today()
    days = max(1, int(days))
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()


def _sessions_root() -> Path:
    return config.RUNTIME_DIR / "sessions"


def all_projects() -> list[str]:
    """Every project the cockpit knows: registered ones plus any that only
    have a ledger left behind (a project removed from the registry still has
    history worth reporting)."""
    names: set[str] = set()
    try:
        names.update(config.list_project_names())
    except Exception:
        pass
    try:
        names.update(task_ledger.list_projects_with_ledger())
    except Exception:
        pass
    from .pm_project import is_pm_project

    return sorted(n for n in names if n and not is_pm_project(n))


def _note_fields(text: str) -> dict[str, str]:
    body = text.split("## Note", 1)[1] if "## Note" in text else text
    body = body.split("\n## ", 1)[0].strip()
    out: dict[str, str] = {}
    for m in _NOTE_FIELD_RE.finditer(body):
        key = m.group(1).upper()
        if key not in out:
            out[key] = " ".join(_SKILL_TAG_RE.sub("", m.group(2)).split())[:240]
    if "STATUS" not in out and body:
        out["STATUS"] = " ".join(body.split())[:240]
    return out


def _collect_notes(project: str, start: str, end: str) -> list[DoneNote]:
    root = _sessions_root()
    notes: list[DoneNote] = []
    try:
        days = sorted(p.name for p in root.iterdir() if p.is_dir() and start <= p.name <= end)
    except OSError:
        return notes
    for day in days:
        pdir = root / day / project
        try:
            files = sorted(pdir.glob("*.md"))
        except OSError:
            continue
        for f in files:
            stem = f.stem  # <role>-HHMMSS
            role, _, hhmmss = stem.rpartition("-")
            if not role or not hhmmss.isdigit():
                continue
            try:
                fields = _note_fields(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            remaining = _SKILL_TAG_RE.sub("", fields.get("REMAINING", "")).strip(" ;,")
            if remaining.lower().rstrip(".") in {"", "none", "ไม่มี", "-", "n/a", "no", "nothing"}:
                remaining = ""
            notes.append(
                DoneNote(
                    day=day,
                    time=f"{hhmmss[:2]}:{hhmmss[2:4]}",
                    role=role,
                    path=str(f),
                    status=_SKILL_TAG_RE.sub("", fields.get("STATUS", "")).strip(" ;,"),
                    remaining=remaining,
                )
            )
    return notes


def _row_headline(project: str, row: dict) -> str:
    """Readable title of a ledger row: the full task text from its detail
    file (the row summary is cut at ~100 chars, often mid-pointer)."""
    text = str(row.get("summary") or "")
    rel = row.get("detail_rel")
    if rel:
        try:
            body = (config.RUNTIME_DIR / "tasks" / project / str(rel)).read_text(
                encoding="utf-8", errors="replace"
            )
            if body.startswith("---"):
                body = body.split("---", 2)[-1]
            text = body.strip() or text
        except OSError:
            pass
    return headline(text)


def _card_line(card: dict) -> str:
    label = backlog.STATUS_LABELS.get(card.get("status", ""), card.get("status", ""))
    reason = f" ({card['reason']})" if card.get("reason") else ""
    age = backlog.aging_days(card)
    aged = f" · ดอง {age}d" if age > 0 else ""
    title = headline(card.get("detail") or card.get("title") or "")
    return f"[{card.get('id', '?')}] {label}{reason} · {title}{aged}"


def project_report(project: str, days: int, today: date | None = None) -> ProjectReport:
    start, end = _window(days, today)
    rep = ProjectReport(name=project)
    try:
        state = task_ledger.load_state(project)
    except Exception:
        state = {"groups": []}
    for group in state.get("groups", []):
        day = str(group.get("date") or "")
        for feat in group.get("features", []):
            for row in feat.get("rows", []):
                status = str(row.get("status") or "")
                task = TaskRow(
                    day=day,
                    role=str(row.get("role") or "?"),
                    status=status,
                    summary=" ".join(str(row.get("summary") or "").split())[:160],
                    assigned=str(row.get("assign_hhmmss") or ""),
                    done=str(row.get("done_hhmmss") or ""),
                )
                # A task still open is pending whatever day it began on.
                if status == _OPEN_ROW:
                    task.summary = _row_headline(project, row)
                    rep.open_tasks.append(task)
                    rep.last_day = max(rep.last_day, day)
                    continue
                if not (start <= day <= end):
                    continue
                rep.last_day = max(rep.last_day, day)
                if status == _FAILED_ROW:
                    task.summary = _row_headline(project, row)
                    rep.failed_tasks.append(task)
                elif status in _DONE_ROWS:
                    rep.done_count += 1
                else:
                    rep.other_count += 1
    try:
        rep.open_cards = [
            it
            for it in backlog.load(project).get("items", [])
            if it.get("status") in backlog._OPEN_STATUSES
        ]
    except Exception:
        rep.open_cards = []
    rep.notes = _collect_notes(project, start, end)
    if rep.notes:
        rep.last_day = max(rep.last_day, rep.notes[-1].day)
    return rep


def build(
    days: int = 2, project: str | None = None, today: date | None = None
) -> list[ProjectReport]:
    names = [project] if project else all_projects()
    reports = [project_report(n, days, today) for n in names]
    if project:
        return reports
    reports = [r for r in reports if r.has_activity or r.open_cards]
    reports.sort(key=lambda r: (r.last_day, len(r.open_tasks)), reverse=True)
    return reports


def render(
    reports: list[ProjectReport],
    days: int,
    *,
    pending_only: bool = False,
    today: date | None = None,
) -> str:
    start, end = _window(days, today)
    if pending_only:
        reports = [r for r in reports if r.has_pending]
    head = f"# PM summary · {start} → {end} ({days} วัน) · {len(reports)} โปรเจค"
    if not reports:
        return head + "\n\nไม่มีโปรเจคที่มีงานในช่วงนี้" + (" ที่ค้างอยู่" if pending_only else "")
    lines = [head, ""]
    lines.append("| โปรเจค | ล่าสุด | done | ค้าง (กำลังทำ) | fail | backlog เปิด |")
    lines.append("|---|---|---|---|---|---|")
    for r in reports:
        lines.append(
            f"| {r.name} | {r.last_day or '-'} | {r.done_count} | {len(r.open_tasks)} | "
            f"{len(r.failed_tasks)} | {len(r.open_cards)} |"
        )
    for r in reports:
        lines += ["", f"## {r.name}"]
        if r.open_tasks:
            lines.append("**งานที่ยังไม่ส่ง done (ledger: working)**")
            lines += [f"- {t.day} {t.assigned} [{t.role}] {t.summary}" for t in r.open_tasks]
        if r.failed_tasks:
            lines.append("**งานที่ FAILED ในช่วงนี้**")
            lines += [
                f"- {t.day} {t.done or t.assigned} [{t.role}] {t.summary}" for t in r.failed_tasks
            ]
        left = [n for n in r.notes if n.remaining]
        if left:
            lines.append("**done แล้วแต่รายงานว่ายังเหลือ (REMAINING)**")
            lines += [f"- {n.day} {n.time} [{n.role}] {n.remaining}" for n in left[-8:]]
        if r.open_cards:
            lines.append("**backlog ที่ยังเปิด**")
            lines += [f"- {_card_line(c)}" for c in r.open_cards[:10]]
            if len(r.open_cards) > 10:
                lines.append(
                    f"- … อีก {len(r.open_cards) - 10} ใบ (`takkub pm backlog --project {r.name}`)"
                )
        if not pending_only and r.notes:
            lines.append("**done ล่าสุด**")
            lines += [
                f"- {n.day} {n.time} [{n.role}] {n.status[:160]} · `{n.path}`" for n in r.notes[-5:]
            ]
        if not r.has_pending and pending_only is False and not r.notes:
            lines.append("- (ไม่มีงานค้าง)")
    return "\n".join(lines)


def projects_table() -> str:
    """`takkub pm projects`: every known project with its folders."""
    from .config import _project_dict, get_open_tabs

    try:
        open_tabs = set(get_open_tabs())
    except Exception:
        open_tabs = set()
    lines = ["| โปรเจค | เปิดอยู่ | folder |", "|---|---|---|"]
    for name in all_projects():
        try:
            paths = _project_dict(name).get("paths", {}) or {}
        except Exception:
            paths = {}
        folders = ", ".join(str(p) for p in paths.values()) or "-"
        lines.append(f"| {name} | {'✓' if name in open_tabs else ''} | {folders} |")
    return "\n".join(lines)


def parse_day(text: str) -> str:
    return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
