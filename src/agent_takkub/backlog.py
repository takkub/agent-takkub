"""Project backlog (#684): cross-session store of work that has been noticed
but not yet done.

Distinct from the Task Ledger (`task_ledger.py`): the ledger records work that
is *running* (written on assign/done/fail), so anything "found but never
assigned" — the biggest, longest-dormant pile — has no home there and is lost
when the Lead's session ends. The backlog is that home: it survives sessions,
the Lead fills it via CLI as things are noticed, and the owner sees it as
cards, ticks items off, and fires an assign straight from a card.

Storage mirrors `task_ledger`'s conventions (one JSON per project under
`RUNTIME_DIR`, atomic temp+replace, stat-validated cached reads) but is its
own domain folder (`RUNTIME_DIR/backlog/<project>/backlog.json`) per the V2
"1 domain 1 folder" rule.

A backlog item links back to the ledger once it is acted on: `assign_item`
stamps `ledger_task_id`, and when the pane reports done the item flips to
`review` (not silently `done`) so the owner confirms it.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import pathlib
import re
import time
import uuid
from datetime import datetime

from .cached_read import read_cached
from .config import RUNTIME_DIR
from .path_safe import safe_segment

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Status vocabulary (Thai labels are the owner-facing chips in the UI).
#   todo     ยังไม่ทำ      — noticed, not started
#   doing    กำลังทำ       — an assign is live for it
#   review   รอยืนยัน      — pane reported done; owner confirms
#   waiting  รอเจ้าของ     — needs an owner decision before it can proceed
#   blocked  ติดอยู่        — stuck on something external (reason required)
#   deferred พักไว้         — owner parked it deliberately
#   done     เสร็จ         — confirmed complete
#   wont     ตั้งใจไม่ทำ    — decided not to do (reason required)
STATUSES = ("todo", "doing", "review", "waiting", "blocked", "deferred", "done", "wont")
_OPEN_STATUSES = frozenset({"todo", "doing", "review", "waiting", "blocked"})
_TERMINAL_STATUSES = frozenset({"done", "wont"})
STATUS_LABELS = {
    "todo": "ยังไม่ทำ",
    "doing": "กำลังทำ",
    "review": "รอยืนยัน",
    "waiting": "รอเจ้าของ",
    "blocked": "ติดอยู่",
    "deferred": "พักไว้",
    "done": "เสร็จ",
    "wont": "ตั้งใจไม่ทำ",
}
IMPACTS = ("customer", "internal")
SEVERITIES = ("low", "med", "high")

_REPLACE_RETRIES = 8
_REPLACE_RETRY_SLEEP_S = 0.025


def _backlog_dir(project: str) -> pathlib.Path:
    return RUNTIME_DIR / "backlog" / safe_segment(project)


def _store_path(project: str) -> pathlib.Path:
    return _backlog_dir(project) / "backlog.json"


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _atomic_write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    # Same Windows os.replace-over-open-handle retry as task_ledger._atomic_write.
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_RETRY_SLEEP_S * (attempt + 1))


def _empty_store() -> dict:
    return {"schema_version": SCHEMA_VERSION, "items": []}


def load(project: str) -> dict:
    """Return the project's backlog store (deep-copied so callers may mutate)."""
    try:
        raw = copy.deepcopy(read_cached(_store_path(project), json.loads, missing=_empty_store()))
    except (OSError, ValueError):
        return _empty_store()
    if not isinstance(raw, dict):
        return _empty_store()
    raw.setdefault("schema_version", SCHEMA_VERSION)
    items = raw.get("items")
    if not isinstance(items, list):
        raw["items"] = []
    return raw


def _save(project: str, store: dict) -> None:
    store["schema_version"] = SCHEMA_VERSION
    path = _store_path(project)
    _atomic_write(path, json.dumps(store, ensure_ascii=False, indent=2))
    # cached_read's contract (#658): its ≤3s no-stat fast path makes an
    # in-process writer's save invisible to its own next read unless the
    # writer invalidates. Caught live 2026-09-20: `backlog block` then an
    # immediate `backlog list --status blocked` returned 0 items.
    from .cached_read import invalidate

    invalidate(path)


def list_items(project: str, *, status: str | None = None) -> list[dict]:
    """Items newest-first, optionally filtered by status ('open' = active
    work, 'pending' = everything not done/wont incl. deferred)."""
    items = load(project)["items"]
    if status == "open":
        items = [it for it in items if it.get("status") in _OPEN_STATUSES]
    elif status == "pending":
        items = [it for it in items if it.get("status") in _PENDING_STATUSES]
    elif status:
        items = [it for it in items if it.get("status") == status]
    return sorted(items, key=lambda it: (it.get("seq", 0), it.get("created_ts", 0.0)), reverse=True)


def get_item(project: str, item_id: str) -> dict | None:
    for it in load(project)["items"]:
        if it.get("id") == item_id:
            return it
    return None


def add_item(
    project: str,
    title: str,
    *,
    detail: str = "",
    source: str = "",
    files: list[str] | None = None,
    impact: str = "internal",
    severity: str = "med",
    status: str = "todo",
) -> dict:
    """Append a new backlog item and return it."""
    title = (title or "").strip()
    if not title:
        raise ValueError("backlog item needs a title")
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    now = _now()
    store = load(project)
    # Monotonic per-store sequence: two items added in the same clock tick
    # (time.time() resolution) would otherwise tie on created_ts and sort
    # non-deterministically. seq breaks the tie and survives reload.
    next_seq = max((it.get("seq", 0) for it in store["items"]), default=0) + 1
    item = {
        "id": _new_id(),
        "seq": next_seq,
        "title": title,
        "detail": (detail or "").strip(),
        "source": (source or "").strip(),
        "files": [f.strip() for f in (files or []) if f.strip()],
        "impact": impact if impact in IMPACTS else "internal",
        "severity": severity if severity in SEVERITIES else "med",
        "status": status,
        "reason": "",
        "ledger_task_id": None,
        "created_ts": now,
        "updated_ts": now,
    }
    store["items"].append(item)
    _save(project, store)
    return item


def _update(project: str, item_id: str, **fields) -> dict | None:
    store = load(project)
    for it in store["items"]:
        if it.get("id") == item_id:
            it.update(fields)
            it["updated_ts"] = _now()
            _save(project, store)
            return it
    return None


def set_status(project: str, item_id: str, status: str, *, reason: str = "") -> dict | None:
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    return _update(project, item_id, status=status, reason=(reason or "").strip())


def mark_done(project: str, item_id: str) -> dict | None:
    return set_status(project, item_id, "done")


def block(project: str, item_id: str, reason: str) -> dict | None:
    return set_status(project, item_id, "blocked", reason=reason)


def defer(project: str, item_id: str) -> dict | None:
    return set_status(project, item_id, "deferred")


def assign_item(project: str, item_id: str, ledger_task_id: str | None) -> dict | None:
    """Mark an item as being worked on and link it to the ledger row created
    by the assign it triggered."""
    return _update(project, item_id, status="doing", ledger_task_id=ledger_task_id)


def on_ledger_done(project: str, ledger_task_id: str) -> dict | None:
    """When a pane reports done for a task that a backlog item triggered, flip
    the item to `review` (owner confirms) instead of closing it silently.

    #714: an item assigned to several panes (shard fan-out) keeps one link per
    role and only flips once every linked task has reported done."""
    if not ledger_task_id:
        return None
    store = load(project)
    for it in store["items"]:
        if it.get("status") != "doing":
            continue
        links = it.get("links") or []
        hit = next((ln for ln in links if ln.get("task_id") == ledger_task_id), None)
        if hit is None and it.get("ledger_task_id") != ledger_task_id:
            continue
        if hit is not None:
            hit["done"] = True
        if all(ln.get("done") for ln in links if ln.get("role") != "lead"):
            it["status"] = "review"
        it["updated_ts"] = _now()
        _save(project, store)
        return it
    return None


# ── #714: backlog is the mandatory entry point for new work ──────────────────
# `takkub assign` without a card gets one created from the task text; a card
# records which roles it was handed to (`links`), and orchestrator.assign binds
# each link to the ledger task id it mints so `on_ledger_done` can flip it.

# Everything not closed — `deferred` included: a parked item is still work the
# owner has not let go of, and the pending report must show it.
_PENDING_STATUSES = frozenset({"todo", "doing", "review", "waiting", "blocked", "deferred"})
_AUTO_TITLE_MAX = 90
# Two assigns with the same task text inside this window (shard fan-out sends
# one request per shard; a client retry resends) share one auto-created card.
_AUTO_REUSE_WINDOW_S = 120.0
_ROLE_TAG_RE = re.compile(r"^\s*\[[^\]]{0,80}\]\s*")


def pending_items(project: str) -> list[dict]:
    """Every item not yet done/wont, oldest first (the longest-waiting work
    leads the pending report)."""
    items = [it for it in load(project)["items"] if it.get("status") in _PENDING_STATUSES]
    return sorted(items, key=lambda it: (it.get("created_ts", 0.0), it.get("seq", 0)))


def has_active_item(project: str) -> bool:
    """True when at least one item is `doing` — the Lead direct-edit gate."""
    return any(it.get("status") == "doing" for it in load(project)["items"])


def title_from_task(task: str) -> str:
    """A card title from a free-form task spec: first meaningful line, leading
    `[ROLE: …]`-style tags stripped, capped at `_AUTO_TITLE_MAX` chars."""
    for raw in (task or "").splitlines():
        line = raw.strip()
        while True:
            stripped = _ROLE_TAG_RE.sub("", line, count=1)
            if stripped == line:
                break
            line = stripped.strip()
        line = line.lstrip("#>*- ").strip()
        if line:
            if len(line) > _AUTO_TITLE_MAX:
                line = line[: _AUTO_TITLE_MAX - 1].rstrip() + "…"
            return line
    return "(งานไม่มีชื่อ)"


def _add_link(item: dict, role: str) -> None:
    links = item.setdefault("links", [])
    links.append({"role": role, "task_id": None, "done": False, "ts": _now()})


def link_assign(project: str, item_id: str, role: str) -> dict | None:
    """Mark *item_id* as handed to *role* (status → doing). The ledger task id
    is bound later by `bind_task_id`, from inside orchestrator.assign."""
    store = load(project)
    for it in store["items"]:
        if it.get("id") == item_id:
            _add_link(it, role)
            it["status"] = "doing"
            it["reason"] = ""
            it["updated_ts"] = _now()
            _save(project, store)
            return it
    return None


def ensure_for_assign(
    project: str, role: str, task: str, backlog_id: str | None = None
) -> tuple[dict, bool]:
    """The card an assign runs under. With *backlog_id* that card (must exist
    and not be closed); without one a card is created from *task* — or reused
    when the same task text was auto-carded moments ago (shard fan-out,
    client retry). Returns `(item, is_new_work)`: new work = a card that was
    not already `doing`, i.e. the moment the pending report is owed."""
    backlog_id = (backlog_id or "").strip()
    if backlog_id:
        item = get_item(project, backlog_id)
        if item is None:
            raise ValueError(f"ไม่พบ backlog id {backlog_id}")
        if item.get("status") in _TERMINAL_STATUSES:
            raise ValueError(
                f"backlog [{backlog_id}] ปิดไปแล้ว ({item.get('status')}) — "
                "เปิดใหม่ด้วย `takkub backlog status <id> todo` หรือไม่ใส่ --backlog ให้สร้างใบใหม่"
            )
        was_doing = item.get("status") == "doing"
        linked = link_assign(project, backlog_id, role) or item
        return linked, not was_doing
    fingerprint = (task or "").strip()
    now = _now()
    for it in load(project)["items"]:
        if (
            it.get("auto_task") == fingerprint
            and it.get("status") == "doing"
            and now - it.get("created_ts", 0.0) <= _AUTO_REUSE_WINDOW_S
        ):
            linked = link_assign(project, it["id"], role) or it
            return linked, False
    item = add_item(
        project,
        title_from_task(task),
        detail=(task or "").strip(),
        source=f"auto · takkub assign → {role}",
        status="doing",
    )
    store = load(project)
    for it in store["items"]:
        if it.get("id") == item["id"]:
            it["auto_task"] = fingerprint
            _add_link(it, role)
            _save(project, store)
            return it, True
    return item, True


def bind_task_id(project: str, role: str, task_id: str) -> dict | None:
    """Called by orchestrator.assign once it has minted *task_id* for *role*.
    Fills the newest unbound link for that role; with none unbound (a quota
    reroute / queue re-dispatch re-assigning the same work) the newest open
    link for the role moves to the new id so done still finds the card."""
    if not task_id:
        return None
    store = load(project)
    doing = [it for it in store["items"] if it.get("status") == "doing"]
    doing.sort(key=lambda it: it.get("updated_ts", 0.0), reverse=True)
    target = None
    for want_unbound in (True, False):
        for it in doing:
            for ln in reversed(it.get("links") or []):
                if ln.get("role") != role or ln.get("done"):
                    continue
                if want_unbound and ln.get("task_id"):
                    continue
                target = (it, ln)
                break
            if target:
                break
        if target:
            break
    if target is None:
        return None
    it, ln = target
    if ln.get("task_id") == task_id:
        return it
    ln["task_id"] = task_id
    it["updated_ts"] = _now()
    _save(project, store)
    return it


def start_lead_work(project: str, *, item_id: str = "", title: str = "") -> tuple[dict, bool]:
    """`takkub backlog start` — the Lead is about to do work itself. Returns
    `(item, is_new_work)` like `ensure_for_assign`."""
    item_id = (item_id or "").strip()
    if item_id:
        item = get_item(project, item_id)
        if item is None:
            raise ValueError(f"ไม่พบ backlog id {item_id}")
        if item.get("status") in _TERMINAL_STATUSES:
            raise ValueError(f"backlog [{item_id}] ปิดไปแล้ว ({item.get('status')})")
        was_doing = item.get("status") == "doing"
        return (link_assign(project, item_id, "lead") or item), not was_doing
    if not (title or "").strip():
        raise ValueError("backlog start ต้องมี <id> หรือ --title")
    item = add_item(project, title, source="lead ทำเอง", status="doing")
    return (link_assign(project, item["id"], "lead") or item), True


def pending_report(project: str, *, exclude_ids: tuple[str, ...] = (), limit: int = 10) -> str:
    """The "งานค้างก่อนเริ่มงานใหม่" block: every pending item except the one
    just started, oldest first. Empty string when nothing else is pending."""
    items = [it for it in pending_items(project) if it.get("id") not in exclude_ids]
    if not items:
        return ""
    now = _now()
    lines = [f"📋 งานค้างใน backlog {len(items)} ใบ (ก่อนเริ่มงานใหม่):"]
    for it in items[:limit]:
        lines.append("  " + render_line(it, now=now))
    if len(items) > limit:
        lines.append(
            f"  … อีก {len(items) - limit} ใบ — ดูทั้งหมด: takkub backlog list --status pending"
        )
    return "\n".join(lines)


def remove(project: str, item_id: str) -> bool:
    store = load(project)
    before = len(store["items"])
    store["items"] = [it for it in store["items"] if it.get("id") != item_id]
    if len(store["items"]) != before:
        _save(project, store)
        return True
    return False


def progress(project: str) -> tuple[int, int]:
    """(#done, #total) across non-`wont` items — the owner's "10/22" glance."""
    items = [it for it in load(project)["items"] if it.get("status") != "wont"]
    done = sum(1 for it in items if it.get("status") == "done")
    return done, len(items)


def age_days(item: dict, *, now: float | None = None) -> int:
    now = now if now is not None else _now()
    return max(0, int((now - item.get("created_ts", now)) // 86400))


# ── Markdown-table import (`takkub backlog import`) ──────────────────────────
# Audit reports are already markdown tables with the right columns; don't make
# the Lead retype 22 rows. Parse a GitHub-flavoured pipe table, map columns by
# header name (fuzzy, Thai + English), and skip the separator row.

_HEADER_ALIASES = {
    "title": ("title", "หัวข้อ", "งาน", "item", "รายการ", "ประเด็น"),
    "detail": ("detail", "รายละเอียด", "description", "หมายเหตุ", "note", "notes"),
    "files": ("file", "files", "ไฟล์", "location", "ตำแหน่ง", "path"),
    "severity": ("severity", "ความรุนแรง", "sev", "priority", "ระดับ"),
    "impact": ("impact", "ผลกระทบ", "ใครเจอ", "audience"),
    "status": ("status", "สถานะ"),
}
_SEV_WORDS = {
    "high": "high",
    "สูง": "high",
    "critical": "high",
    "crit": "high",
    "med": "med",
    "medium": "med",
    "กลาง": "med",
    "ปานกลาง": "med",
    "low": "low",
    "ต่ำ": "low",
    "minor": "low",
}
_IMPACT_WORDS = {
    "customer": "customer",
    "ลูกค้า": "customer",
    "external": "customer",
    "prod": "customer",
    "internal": "internal",
    "ภายใน": "internal",
    "dev": "internal",
}


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    # Split on unescaped pipes only.
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells)


def _match_header(name: str) -> str | None:
    low = name.strip().lower()
    for field, aliases in _HEADER_ALIASES.items():
        if any(a in low for a in aliases):
            return field
    return None


def parse_markdown_table(text: str) -> list[dict]:
    """Parse the first pipe table in *text* into item kwargs dicts. Returns []
    when no table with a recognizable title column is found."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    # Find a header row (has pipes) immediately followed by a separator row.
    for i in range(len(lines) - 1):
        if "|" not in lines[i]:
            continue
        header = _split_row(lines[i])
        sep = _split_row(lines[i + 1])
        if not _is_separator(sep) or len(sep) != len(header):
            continue
        colmap = {idx: _match_header(h) for idx, h in enumerate(header)}
        if "title" not in colmap.values():
            continue
        out: list[dict] = []
        for row_line in lines[i + 2 :]:
            if "|" not in row_line:
                break
            cells = _split_row(row_line)
            if _is_separator(cells):
                continue
            fields: dict = {}
            for idx, val in enumerate(cells):
                field = colmap.get(idx)
                if not field or not val:
                    continue
                if field == "files":
                    fields["files"] = [
                        p.strip(" `") for p in re.split(r"[,\s]+", val) if p.strip(" `")
                    ]
                elif field == "severity":
                    fields["severity"] = _SEV_WORDS.get(val.strip().lower(), "med")
                elif field == "impact":
                    fields["impact"] = _IMPACT_WORDS.get(val.strip().lower(), "internal")
                else:
                    fields[field] = val.strip(" `")
            if fields.get("title"):
                out.append(fields)
        return out
    return []


def import_markdown(project: str, text: str, *, source: str = "") -> list[dict]:
    """Parse a markdown table and add each row as a backlog item. Returns the
    items created."""
    created: list[dict] = []
    for fields in parse_markdown_table(text):
        title = fields.pop("title")
        if source and "source" not in fields:
            fields["source"] = source
        created.append(add_item(project, title, **fields))
    return created


def list_projects_with_backlog() -> list[str]:
    root = RUNTIME_DIR / "backlog"
    try:
        return sorted(p.name for p in root.iterdir() if (p / "backlog.json").is_file())
    except OSError:
        return []


def render_line(item: dict, *, now: float | None = None) -> str:
    """One-line CLI rendering for `takkub backlog list`."""
    age = age_days(item, now=now)
    label = STATUS_LABELS.get(item.get("status", ""), item.get("status", ""))
    sev = item.get("severity", "")
    imp = "🧑‍💼" if item.get("impact") == "customer" else ""
    aged = f" · ดอง {age}d" if age > 0 else ""
    reason = f" ({item['reason']})" if item.get("reason") else ""
    return f"[{item.get('id', '?')}] {label}{reason} · {sev}{imp} · {item.get('title', '')}{aged}"


def _fmt_ts(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"
