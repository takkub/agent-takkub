"""Task Ledger (A7): a markdown-first log of every `takkub assign` in a project.

One `INDEX.md` per project (`RUNTIME_DIR/tasks/<project>/INDEX.md`), grouped
📅 date+goal → ### feature → checkbox row per assignment, each row linking to
a per-task detail file. Every assign writes a row (not just long tasks) so a
role that never calls `takkub done` leaves a visible unfinished `[~]` row
behind instead of disappearing silently.

**Deliberately its own module** (not folded into `orchestrator_text.py`'s
`_task_handoff_pointer`): that mechanism exists solely to dodge the PTY
paste-swallow bug for long tasks (#22/#26) and must keep working unmodified.
This module's own per-task detail file always writes — short or long task —
and carries frontmatter/status the pointer file doesn't, so it uses a
`-ledger` filename suffix in the same `RUNTIME_DIR/tasks/<project>/<date>/`
directory (reusing the directory/date convention from
``orchestrator_text._task_handoff_dir``) to guarantee it never collides with
a pointer file written for the same role in the same second.

State is tracked in a small JSON sidecar (``.ledger-state.json``, not
rendered) so ``mark_done`` never has to parse markdown back into structured
data — ``INDEX.md`` is a pure regenerated view of the JSON, written
atomically (temp file + ``os.replace``) alongside every mutation.

**Design note left for Lead (per task spec — flagging, not guessing):**
auto-respawn replay calls ``assign()`` again with the same task after a pane
crash/restart, which (by design here) appends a FRESH ledger row rather than
reusing/deduping the previous one — the stale `[~]` row is left exactly as
the "unfinished nag" rule intends. If auto-respawn churn turns out to be
frequent enough to clutter INDEX.md with duplicate rows for the same logical
assignment, that's a follow-up call for Lead (no obviously-correct dedup key
without risking hiding a genuinely-different reassignment).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from datetime import datetime

from .config import RUNTIME_DIR

logger = logging.getLogger(__name__)

_FALLBACK_GOAL = "(ไม่ระบุเป้าหมาย)"
_FALLBACK_FEATURE = "งานทั่วไป"

_ROW_SYMBOL = {"working": "~", "ok": "x", "fail": "!", "closed": "-"}
_VALID_STATUSES = ("ok", "fail", "closed")


def _ledger_dir(project: str) -> pathlib.Path:
    return RUNTIME_DIR / "tasks" / project


def _state_path(project: str) -> pathlib.Path:
    return _ledger_dir(project) / ".ledger-state.json"


def _index_path(project: str) -> pathlib.Path:
    return _ledger_dir(project) / "INDEX.md"


def _display_path(p: str) -> str:
    return str(p).replace(os.sep, "/")


def _atomic_write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _load_state(project: str) -> dict:
    try:
        return json.loads(_state_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"groups": [], "open": {}}


def _save_state(project: str, state: dict) -> None:
    _atomic_write(_state_path(project), json.dumps(state, ensure_ascii=False, indent=2))


def _find_group(state: dict, date: str, goal: str) -> dict | None:
    for g in state.get("groups", []):
        if g["date"] == date and g["goal"] == goal:
            return g
    return None


def _find_or_create_group(state: dict, date: str, goal: str) -> dict:
    g = _find_group(state, date, goal)
    if g is not None:
        return g
    g = {"date": date, "goal": goal, "features": []}
    state.setdefault("groups", []).insert(0, g)  # newest group on top
    return g


def _find_feature(group: dict, feature: str) -> dict | None:
    for f in group.get("features", []):
        if f["name"] == feature:
            return f
    return None


def _find_or_create_feature(group: dict, feature: str) -> dict:
    f = _find_feature(group, feature)
    if f is not None:
        return f
    f = {"name": feature, "rows": []}
    group.setdefault("features", []).append(f)
    return f


def create_assignment(
    project: str,
    role: str,
    cwd: str | None,
    task: str,
    goal: str | None,
    feature: str | None,
    provider: str,
) -> str:
    """Record a fresh assignment: per-task detail `.md` + an upserted `INDEX.md` row.

    Called on every assign (write-on-assign rule), not just long tasks.
    Never raises — a write failure degrades to returning a Lead-facing
    warning string while the caller's assign proceeds unaffected. Returns
    `""` on success.
    """
    now = datetime.now()
    date = now.strftime("%Y-%m-%d")
    hhmmss = now.strftime("%H%M%S")
    goal_text = (goal or "").strip() or _FALLBACK_GOAL
    feature_text = (feature or "").strip() or _FALLBACK_FEATURE
    role = role.strip()
    cwd_disp = _display_path(cwd) if cwd else "—"
    summary = task.strip().splitlines()[0].strip() if task.strip() else ""
    if len(summary) > 100:
        summary = summary[:100].rstrip() + "…"

    detail_name = f"{hhmmss}-{role}-ledger.md"
    detail_rel = f"{date}/{detail_name}"

    warning = ""
    detail_written = True
    try:
        _atomic_write(
            _ledger_dir(project) / date / detail_name,
            f"---\n"
            f"date: {date}\n"
            f"role: {role}\n"
            f"cwd: {cwd_disp}\n"
            f"project: {project}\n"
            f"goal: {goal_text}\n"
            f"feature: {feature_text}\n"
            f"provider: {provider}\n"
            f"status: working\n"
            f"assign_ts: {now.strftime('%H:%M:%S')}\n"
            f"---\n\n{task}\n",
        )
    except OSError as exc:
        detail_written = False
        warning = f"⚠️ [ledger] เขียน detail file ของ {role} ไม่สำเร็จ: {exc}"
        logger.warning("task_ledger detail write failed for %s/%s: %s", project, role, exc)

    row = {
        "role": role,
        "cwd": cwd_disp,
        "summary": summary,
        "status": "working",
        "assign_hhmmss": now.strftime("%H:%M:%S"),
        "done_hhmmss": None,
        "detail_rel": detail_rel if detail_written else None,
    }

    state = _load_state(project)
    group = _find_or_create_group(state, date, goal_text)
    feat = _find_or_create_feature(group, feature_text)
    feat["rows"].append(row)
    state.setdefault("open", {})[role] = {
        "date": date,
        "goal": goal_text,
        "feature": feature_text,
        "row_index": len(feat["rows"]) - 1,
    }

    try:
        _save_state(project, state)
        _regen_index(project, state)
    except OSError as exc:
        w2 = f"⚠️ [ledger] เขียน INDEX.md ของ {project} ไม่สำเร็จ: {exc}"
        logger.warning("task_ledger INDEX write failed for %s: %s", project, exc)
        warning = f"{warning}\n{w2}" if warning else w2

    return warning


def _flip_detail_status(path: pathlib.Path, status: str) -> None:
    text = path.read_text(encoding="utf-8")
    new_text = text.replace("status: working\n", f"status: {status}\n", 1)
    if new_text != text:
        _atomic_write(path, new_text)


def mark_done(project: str, role: str, status: str, ts: datetime | None = None) -> str:
    """Flip the currently-open ledger row for *role* to *status*.

    `status` is one of ``"ok"`` (clean done), ``"fail"`` (`takkub done
    --fail`), or ``"closed"`` (pane closed without ever calling done).
    Looks up the role's open row via the state's `open` index (set by
    `create_assignment`) — a role with no open row (ledger write failed
    earlier, or this is a second close after done already flipped it) is a
    no-op, never a crash. Returns `""` on success, else a Lead-facing
    warning.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    ts = ts or datetime.now()
    state = _load_state(project)
    open_map = state.get("open", {})
    ptr = open_map.pop(role, None)
    if ptr is None:
        return ""

    group = _find_group(state, ptr["date"], ptr["goal"])
    feat = _find_feature(group, ptr["feature"]) if group is not None else None
    rows = feat["rows"] if feat is not None else []
    idx = ptr["row_index"]
    if not (0 <= idx < len(rows)):
        return ""

    rows[idx]["status"] = status
    rows[idx]["done_hhmmss"] = ts.strftime("%H:%M:%S")

    warning = ""
    detail_rel = rows[idx].get("detail_rel")
    if detail_rel:
        try:
            _flip_detail_status(_ledger_dir(project) / detail_rel, status)
        except OSError as exc:
            warning = f"⚠️ [ledger] อัปเดต detail file ของ {role} ไม่สำเร็จ: {exc}"
            logger.warning("task_ledger detail flip failed for %s/%s: %s", project, role, exc)

    try:
        _save_state(project, state)
        _regen_index(project, state)
    except OSError as exc:
        w2 = f"⚠️ [ledger] เขียน INDEX.md ของ {project} ไม่สำเร็จ: {exc}"
        logger.warning("task_ledger INDEX write failed for %s: %s", project, exc)
        warning = f"{warning}\n{w2}" if warning else w2

    return warning


def _status_suffix(row: dict) -> str:
    status = row["status"]
    done_hhmm = row.get("done_hhmmss") or ""
    if status == "working":
        return "⏳ กำลังทำ"
    if status == "ok":
        return f"✅ done `{done_hhmm}`"
    if status == "fail":
        return f"❌ FAILED `{done_hhmm}`"
    if status == "closed":
        return f"➖ ปิด `{done_hhmm}`"
    return ""


def _feature_emoji(feat: dict) -> str:
    statuses = {r["status"] for r in feat["rows"]}
    if not statuses:
        return "⏳"
    if "working" in statuses:
        return "🔨"
    if "fail" in statuses:
        return "⚠️"
    if statuses <= {"ok", "closed"}:
        return "✅"
    return "⏳"


def _render_group(group: dict) -> str:
    rows_all = [r for f in group["features"] for r in f["rows"]]
    total = len(rows_all)
    done_ct = sum(1 for r in rows_all if r["status"] == "ok")
    working_ct = sum(1 for r in rows_all if r["status"] == "working")
    lines = [
        f"## 📅 {group['date']} — 🎯 เป้าหมาย: {group['goal']}",
        "",
        f"`progress: {done_ct}/{total} เสร็จ · {working_ct} กำลังทำ`",
        "",
    ]
    for i, feat in enumerate(group["features"], start=1):
        lines.append(f"### {_feature_emoji(feat)} {i}. {feat['name']}")
        for row in feat["rows"]:
            sym = _ROW_SYMBOL.get(row["status"], " ")
            link = (
                f" → [{pathlib.Path(row['detail_rel']).name}]({row['detail_rel']})"
                if row.get("detail_rel")
                else ""
            )
            lines.append(
                f"- [{sym}] `{row['assign_hhmmss']}` **{row['role']}** · {row['cwd']} · "
                f"{row['summary']}{link} — {_status_suffix(row)}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _regen_index(project: str, state: dict) -> None:
    header = (
        f"# 📋 Task Ledger — {project}\n\n"
        "> สารบัญงานทั้งหมด · เปิดไฟล์เดียวเห็นว่า **สั่งอะไร · ใครทำ · เสร็จยัง** · "
        "คลิกชื่อไฟล์อ่าน detail เต็ม\n"
        "> สถานะ: `[ ]` รอคิว · `[~]` กำลังทำ · `[x]` เสร็จ · `[!]` FAILED · `[-]` ปิด/ยกเลิก\n\n"
        "---\n\n"
    )
    body = "\n---\n\n".join(_render_group(g) for g in state.get("groups", []))
    _atomic_write(_index_path(project), header + body)
