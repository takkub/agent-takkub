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

**Orphan/double-count fix (A7-followup):** ``state["open"]`` is keyed by
``role`` alone. If ``create_assignment`` is called again for a role that
still has an open (``working``) row — a fresh re-assign before the previous
task ever called ``takkub done`` — the old open pointer used to be silently
overwritten, leaving the first row stuck at ``[~]`` forever (an orphan that
also double-counts against ``progress: done/total``). Fixed by having
``create_assignment`` resolve any stale open row for the role to a terminal
``superseded`` marker before opening the new one, so a role has at most one
open row at any time.

This does **not** touch crash-respawn replay: ``spawn_engine._auto_respawn``
re-sends the cached ``last_assigned_task`` via ``_send_when_ready`` directly
— it never calls ``create_assignment`` (only ``Orchestrator.assign`` /
``_assign_dispatch`` do, for a genuine fresh assign) — so the "unfinished
nag" row from a crashed pane is untouched by this fix. No ``is_replay`` flag
is needed: the two code paths already never collide.
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

_ROW_SYMBOL = {"working": "~", "ok": "x", "fail": "!", "closed": "-", "superseded": ">"}
_VALID_STATUSES = ("ok", "fail", "closed")
_TERMINAL_STATUSES = frozenset({"ok", "closed", "superseded"})


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


def load_state(project: str) -> dict:
    """Public loader for the ledger's JSON state — the Task Tree dock (A8)
    reads this directly instead of parsing INDEX.md markdown, so the tree
    view can never drift from the ledger's own structured data."""
    return _load_state(project)


def index_path(project: str) -> pathlib.Path:
    """Public accessor for *project*'s rendered INDEX.md path (A8's ↗ button)."""
    return _index_path(project)


def list_projects_with_ledger() -> list[str]:
    """Every project namespace with a ledger on disk — the startup reconcile
    scan (issue #166) walks this list rather than requiring a project arg."""
    base = RUNTIME_DIR / "tasks"
    try:
        return sorted(
            p.name for p in base.iterdir() if p.is_dir() and (p / ".ledger-state.json").is_file()
        )
    except OSError:
        return []


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


def _derive_summary(task: str) -> str:
    """Pick the task's meaningful line for the ledger row summary.

    Every task spec starts with a `[ROLE: ...]` declaration line and ends
    with a "รายงานกลับด้วย takkub done" trailer — neither is useful as a
    row summary. Skip both and take the first real content line instead.
    Falls back to the raw first line if the task is declaration-only.
    """
    stripped = task.strip()
    if not stripped:
        return ""
    lines = stripped.splitlines()
    summary = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.lower().startswith("[role:"):
            continue
        if line.startswith("รายงานกลับด้วย"):
            continue
        summary = line
        break
    if not summary:
        summary = lines[0].strip()
    if len(summary) > 100:
        summary = summary[:100].rstrip() + "…"
    return summary


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
    hhmmss = now.strftime("%H%M%S%f")
    goal_text = (goal or "").strip() or _FALLBACK_GOAL
    feature_text = (feature or "").strip() or _FALLBACK_FEATURE
    role = role.strip()
    cwd_disp = _display_path(cwd) if cwd else "—"
    summary = _derive_summary(task)

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
    # Orphan/double-count fix: a re-assign to a role that still has an open
    # (never-`done`) row must resolve that stale row first, else its open
    # pointer is clobbered below and the old row is stuck at `[~]` forever
    # while also double-counting against `progress: done/total`.
    _, stale_warning = _resolve_open_row(state, project, role, "superseded", now)
    if stale_warning:
        warning = f"{warning}\n{stale_warning}" if warning else stale_warning

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


def _open_row(state: dict, ptr: dict) -> tuple[dict, list[dict]] | tuple[None, None]:
    """Resolve an `open[role]` pointer to its live row dict (read-only)."""
    group = _find_group(state, ptr["date"], ptr["goal"])
    feat = _find_feature(group, ptr["feature"]) if group is not None else None
    rows = feat["rows"] if feat is not None else []
    idx = ptr["row_index"]
    if not (0 <= idx < len(rows)):
        return None, None
    return rows[idx], rows


def _resolve_open_row(
    state: dict, project: str, role: str, status: str, ts: datetime, reason: str | None = None
) -> tuple[bool, str]:
    """Pop *role*'s open-row pointer (if any) and flip that row to *status*.

    Shared by `mark_done` (external ok/fail/closed) and `create_assignment`'s
    stale-open resolution (a fresh re-assign superseding a still-open row
    left by a previous assignment that never called `takkub done`), and by
    `reconcile_orphaned`/`close_role` (issue #166). Mutates `state` in place
    but does not persist it — callers save + regen the index themselves, and
    only when this returns `True` (a row actually changed), matching the
    prior no-op-on-missing-pointer behavior. *reason*, when given, is
    stamped on the row (e.g. `"orphaned"`) so the rendered index can explain
    *why* it closed, distinct from an ordinary `takkub done`/close.
    """
    open_map = state.get("open", {})
    ptr = open_map.pop(role, None)
    if ptr is None:
        return False, ""

    row, _rows = _open_row(state, ptr)
    if row is None:
        return False, ""

    row["status"] = status
    row["done_hhmmss"] = ts.strftime("%H:%M:%S")
    if reason:
        row["reason"] = reason

    warning = ""
    detail_rel = row.get("detail_rel")
    if detail_rel:
        try:
            _flip_detail_status(_ledger_dir(project) / detail_rel, status)
        except OSError as exc:
            warning = f"⚠️ [ledger] อัปเดต detail file ของ {role} ไม่สำเร็จ: {exc}"
            logger.warning("task_ledger detail flip failed for %s/%s: %s", project, role, exc)

    return True, warning


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
    changed, warning = _resolve_open_row(state, project, role, status, ts)
    if not changed:
        return ""

    try:
        _save_state(project, state)
        _regen_index(project, state)
    except OSError as exc:
        w2 = f"⚠️ [ledger] เขียน INDEX.md ของ {project} ไม่สำเร็จ: {exc}"
        logger.warning("task_ledger INDEX write failed for %s: %s", project, exc)
        warning = f"{warning}\n{w2}" if warning else w2

    return warning


def _orphan_candidates(state: dict, live_roles: frozenset[str], today: str) -> list[str]:
    """Roles whose open row is safe to auto-reconcile (issue #166).

    A row qualifies only when BOTH hold:
      1. its assign *date* is strictly before *today* — a same-day row is
         left alone because `spawn_engine._auto_respawn` can still bring
         that exact pane back a few seconds after a restart (it re-sends
         the cached task directly and never calls `create_assignment`, so a
         respawned pane's row legitimately stays "working" and must not be
         closed out from under it); and
      2. *role* has no live pane right now (`live_roles`) — belt-and-
         suspenders for the rare case a stale-dated row's pane is somehow
         still alive (cockpit left running across midnight).
    Two cockpit instances never collide here: each instance's RUNTIME_DIR
    (and therefore its `.ledger-state.json`) is already instance-scoped, so
    this only ever sees its own process's panes.
    """
    return [
        role
        for role, ptr in state.get("open", {}).items()
        if ptr["date"] < today and role not in live_roles
    ]


def preview_reconcile(
    project: str, live_roles: frozenset[str], today: str | None = None
) -> list[dict]:
    """Read-only preview of what `reconcile_orphaned` would close — for
    `takkub task reconcile --dry-run`. Never mutates state on disk."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    state = _load_state(project)
    out = []
    for role in _orphan_candidates(state, live_roles, today):
        ptr = state["open"][role]
        row, _rows = _open_row(state, ptr)
        out.append(
            {
                "role": role,
                "date": ptr["date"],
                "summary": row.get("summary", "") if row else "",
            }
        )
    return out


def reconcile_orphaned(
    project: str, live_roles: frozenset[str], today: str | None = None
) -> tuple[list[str], str]:
    """Close every ledger row still marked "working" whose owning session is
    provably gone (issue #166 — a row used to stick at "working" forever
    once the cockpit process that owned it exited, since `mark_done` is only
    ever called from live pane-close/done handlers). See
    `_orphan_candidates` for the safety gate. Rows are flipped to `"closed"`
    with `reason="orphaned"` (never deleted — history stays visible in
    INDEX.md, tagged distinctly from an ordinary close). Returns the list of
    role names actually closed, plus any non-fatal write warning."""
    ts = datetime.now()
    today = today or ts.strftime("%Y-%m-%d")
    state = _load_state(project)
    candidates = _orphan_candidates(state, live_roles, today)
    if not candidates:
        return [], ""

    closed: list[str] = []
    warnings: list[str] = []
    for role in candidates:
        changed, warning = _resolve_open_row(state, project, role, "closed", ts, reason="orphaned")
        if changed:
            closed.append(role)
        if warning:
            warnings.append(warning)

    if closed:
        try:
            _save_state(project, state)
            _regen_index(project, state)
        except OSError as exc:
            w2 = f"⚠️ [ledger] เขียน INDEX.md ของ {project} ไม่สำเร็จ: {exc}"
            logger.warning("task_ledger INDEX write failed for %s: %s", project, exc)
            warnings.append(w2)

    return closed, "\n".join(warnings)


def close_role(
    project: str, role: str, live_roles: frozenset[str], force: bool = False
) -> tuple[bool, str]:
    """Manually close *role*'s open ledger row (`takkub task close --role`).

    Unlike `reconcile_orphaned` this is an explicit, user-directed action, so
    it isn't date-gated — but it still refuses a role with a currently live
    pane (real work in progress) unless *force* is passed, so it can never
    be used to accidentally paper over an active task."""
    state = _load_state(project)
    ptr = state.get("open", {}).get(role)
    if ptr is None:
        return False, f"no open ledger row for role '{role}'"
    if role in live_roles and not force:
        return (
            False,
            f"'{role}' has a live pane right now — close the pane first "
            f"(`takkub close --role {role}`), or pass --force to override",
        )

    ts = datetime.now()
    changed, warning = _resolve_open_row(state, project, role, "closed", ts, reason="manual")
    if not changed:
        return False, f"no open ledger row for role '{role}'"

    try:
        _save_state(project, state)
        _regen_index(project, state)
    except OSError as exc:
        w2 = f"⚠️ [ledger] เขียน INDEX.md ของ {project} ไม่สำเร็จ: {exc}"
        logger.warning("task_ledger INDEX write failed for %s: %s", project, exc)
        warning = f"{warning}\n{w2}" if warning else w2

    msg = f"closed ledger row for '{role}'"
    if warning:
        msg = f"{msg}\n{warning}"
    return True, msg


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
        reason = row.get("reason")
        tag = " (orphaned — session ไม่มีแล้ว)" if reason == "orphaned" else ""
        return f"➖ ปิด{tag} `{done_hhmm}`"
    if status == "superseded":
        return f"🔁 แทนที่ด้วยงานใหม่ `{done_hhmm}`"
    return ""


def _feature_emoji(feat: dict) -> str:
    statuses = {r["status"] for r in feat["rows"]}
    if not statuses:
        return "⏳"
    if "working" in statuses:
        return "🔨"
    if "fail" in statuses:
        return "⚠️"
    if statuses <= _TERMINAL_STATUSES:
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
        "> สถานะ: `[ ]` รอคิว · `[~]` กำลังทำ · `[x]` เสร็จ · `[!]` FAILED · "
        "`[-]` ปิด/ยกเลิก · `[>]` แทนที่ด้วยงานใหม่ (re-assign ก่อน done)\n\n"
        "---\n\n"
    )
    body = "\n---\n\n".join(_render_group(g) for g in state.get("groups", []))
    _atomic_write(_index_path(project), header + body)
