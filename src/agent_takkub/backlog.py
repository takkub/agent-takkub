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
    _atomic_write(_store_path(project), json.dumps(store, ensure_ascii=False, indent=2))


def list_items(project: str, *, status: str | None = None) -> list[dict]:
    """Items newest-first, optionally filtered by status ('open' = any
    non-terminal)."""
    items = load(project)["items"]
    if status == "open":
        items = [it for it in items if it.get("status") in _OPEN_STATUSES]
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
    the item to `review` (owner confirms) instead of closing it silently."""
    if not ledger_task_id:
        return None
    store = load(project)
    for it in store["items"]:
        if it.get("ledger_task_id") == ledger_task_id and it.get("status") == "doing":
            it["status"] = "review"
            it["updated_ts"] = _now()
            _save(project, store)
            return it
    return None


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
