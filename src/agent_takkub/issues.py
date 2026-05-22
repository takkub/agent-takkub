"""Issue tracker for agent-takkub cockpit bugs.

Storage: docs/issues/<YYYYMMDD-NNN>.md (YAML frontmatter + markdown body).
Pure file-based — no orchestrator dependency, works offline.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# Relative to repo root (cwd when takkub is invoked from project root).
DEFAULT_ISSUES_DIR = Path("docs/issues")

_SEVERITY_VALUES = ("low", "med", "high")
_STATUS_OPEN = "open"
_STATUS_CLOSED = "closed"

_ID_RE = re.compile(r"^(\d{8})-(\d{3})$")


def _validate_id(issue_id: str) -> None:
    if not _ID_RE.match(issue_id):
        raise ValueError(f"invalid issue ID {issue_id!r} — expected YYYYMMDD-NNN")


# ── helpers ──────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    """Local time as ISO-8601 with UTC offset (no microseconds)."""
    now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%dT%H:%M:%S%z")


def _today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def _parse_file(path: Path) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body_str) from an issue file.

    Raises ValueError on malformed frontmatter.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"malformed frontmatter in {path}: file must start with '---'")
    parts = text.split("---", 2)
    # parts[0] == '' (before first ---), parts[1] == yaml block, parts[2] == body
    if len(parts) < 3:
        raise ValueError(f"malformed frontmatter in {path}: missing closing '---'")
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed frontmatter in {path}: {exc}") from exc
    if not isinstance(fm, dict):
        raise ValueError(f"malformed frontmatter in {path}: expected mapping, got {type(fm)}")
    body = parts[2].lstrip("\n")
    return fm, body


def _write_file(path: Path, fm: dict[str, Any], body: str) -> None:
    """Serialise frontmatter + body to path."""
    # Use yaml.dump with allow_unicode so Thai chars survive round-trips.
    fm_text = yaml.dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    path.write_text(f"---\n{fm_text}---\n\n{body}", encoding="utf-8")


# ── ID generation ─────────────────────────────────────────────────────────────


def next_id(issues_dir: Path, date_str: str | None = None) -> str:
    """Return the next available YYYYMMDD-NNN for today.

    Scans existing files to find the highest NNN used today, then increments.
    Handles ID collision (e.g. concurrent writes) by finding the first unused slot.
    """
    ds = date_str or _today_str()
    prefix = f"{ds}-"
    used: set[int] = set()
    if issues_dir.exists():
        for f in issues_dir.glob(f"{prefix}*.md"):
            m = _ID_RE.match(f.stem)
            if m and m.group(1) == ds:
                used.add(int(m.group(2)))
    n = 1
    while n in used:
        n += 1
    return f"{ds}-{n:03d}"


def _reserve_issue_path(issues_dir: Path, date_str: str) -> tuple[str, Path]:
    """Atomically reserve a new issue file slot. Returns (issue_id, path)."""
    issues_dir.mkdir(parents=True, exist_ok=True)
    for n in range(1, 1000):
        issue_id = f"{date_str}-{n:03d}"
        path = issues_dir / f"{issue_id}.md"
        try:
            path.open("x", encoding="utf-8").close()
            return issue_id, path
        except FileExistsError:
            continue
    raise RuntimeError(f"no issue ID available for {date_str}")


# ── commands ──────────────────────────────────────────────────────────────────


def new_issue(
    title: str,
    body: str,
    *,
    severity: str = "med",
    noticed_in: str | None = None,
    role: str | None = None,
    tags: list[str] | None = None,
    issues_dir: Path = DEFAULT_ISSUES_DIR,
) -> tuple[str, Path]:
    """Create a new issue file. Returns (id, path)."""
    if not title.strip():
        raise ValueError("title must not be empty")
    if severity not in _SEVERITY_VALUES:
        raise ValueError(f"severity must be one of {_SEVERITY_VALUES}, got {severity!r}")

    issue_id, path = _reserve_issue_path(issues_dir, _today_str())

    fm: dict[str, Any] = {
        "id": issue_id,
        "title": title,
        "status": _STATUS_OPEN,
        "severity": severity,
        "created_at": _now_iso(),
    }
    if noticed_in:
        fm["noticed_in"] = noticed_in
    if role:
        fm["role"] = role
    if tags:
        fm["tags"] = tags

    _write_file(path, fm, body)
    return issue_id, path


def list_issues(
    *,
    filter_open: bool = False,
    filter_closed: bool = False,
    noticed_in: str | None = None,
    role: str | None = None,
    severity: str | None = None,
    issues_dir: Path = DEFAULT_ISSUES_DIR,
) -> list[dict[str, Any]]:
    """Return list of issue dicts matching filters (sorted by id)."""
    if not issues_dir.exists():
        return []

    results = []
    for path in sorted(issues_dir.glob("*.md")):
        try:
            fm, _ = _parse_file(path)
        except ValueError as exc:
            print(f"warn: {path.name}: {exc}", file=sys.stderr)
            continue

        status = fm.get("status", _STATUS_OPEN)

        if filter_open and not filter_closed and status != _STATUS_OPEN:
            continue
        if filter_closed and not filter_open and status != _STATUS_CLOSED:
            continue
        if noticed_in and fm.get("noticed_in") != noticed_in:
            continue
        if role and fm.get("role") != role:
            continue
        if severity and fm.get("severity") != severity:
            continue

        results.append(fm)

    return results


def close_issue(
    issue_id: str,
    *,
    note: str = "",
    issues_dir: Path = DEFAULT_ISSUES_DIR,
) -> Path:
    """Close an issue. Returns the file path. Raises ValueError on errors."""
    _validate_id(issue_id)
    path = issues_dir / f"{issue_id}.md"
    if not path.exists():
        raise ValueError(f"issue {issue_id!r} not found")

    fm, body = _parse_file(path)

    if fm.get("status") == _STATUS_CLOSED:
        raise ValueError(f"issue {issue_id!r} is already closed")

    fm["status"] = _STATUS_CLOSED
    fm["closed_at"] = _now_iso()
    if note:
        fm["closed_note"] = note

    _write_file(path, fm, body)
    return path


def show_issue(issue_id: str, *, issues_dir: Path = DEFAULT_ISSUES_DIR) -> str:
    """Return raw file content. Raises ValueError if not found."""
    _validate_id(issue_id)
    path = issues_dir / f"{issue_id}.md"
    if not path.exists():
        raise ValueError(f"issue {issue_id!r} not found")
    return path.read_text(encoding="utf-8")


# ── CLI entry points (called from cli.py) ────────────────────────────────────


def _resolve_issues_dir(args_issues_dir: str | None) -> Path:
    if args_issues_dir:
        return Path(args_issues_dir)
    return DEFAULT_ISSUES_DIR


def cmd_issue_new(args: Any) -> dict:
    """Handler for `takkub issue new`."""
    title: str = args.title
    body: str = args.body or ""

    if not body:
        # No --body and no TTY → cannot open $EDITOR safely in a pane
        if not sys.stdin.isatty():
            return {
                "ok": False,
                "msg": 'no --body provided and no TTY — pass --body "<text>" explicitly',
            }
        # TTY: open $EDITOR
        import subprocess
        import tempfile

        editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "notepad"))
        with tempfile.NamedTemporaryFile(
            suffix=".md", delete=False, mode="w", encoding="utf-8"
        ) as f:
            tmppath = f.name
        try:
            ret = subprocess.call([*shlex.split(editor), tmppath])
            if ret != 0:
                return {"ok": False, "msg": f"editor exited with code {ret}"}
            body = Path(tmppath).read_text(encoding="utf-8")
        finally:
            try:
                os.unlink(tmppath)
            except OSError:
                pass

    tags = [t.strip() for t in args.tag.split(",")] if getattr(args, "tag", None) else None
    issues_dir = _resolve_issues_dir(getattr(args, "issues_dir", None))

    try:
        issue_id, path = new_issue(
            title,
            body,
            severity=getattr(args, "severity", "med") or "med",
            noticed_in=getattr(args, "noticed_in", None),
            role=getattr(args, "role", None),
            tags=tags or None,
            issues_dir=issues_dir,
        )
    except ValueError as exc:
        return {"ok": False, "msg": str(exc)}

    print(f"{issue_id}  {path}")
    return {"ok": True, "msg": f"created {issue_id}"}


def _safe_col(val: str, width: int) -> str:
    return val.replace("\n", " ").replace("\r", "")[:width]


def _safe_print(text: str, **kwargs) -> None:
    try:
        print(text, **kwargs)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc), **kwargs)


def cmd_issue_list(args: Any) -> dict:
    """Handler for `takkub issue list`."""
    issues_dir = _resolve_issues_dir(getattr(args, "issues_dir", None))
    try:
        items = list_issues(
            filter_open=getattr(args, "open", False),
            filter_closed=getattr(args, "closed", False),
            noticed_in=getattr(args, "noticed_in", None),
            role=getattr(args, "role", None),
            severity=getattr(args, "severity", None),
            issues_dir=issues_dir,
        )
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}

    if not items:
        _safe_print("(no issues)")
        return {"ok": True, "msg": "0 issue(s)"}

    # Table header
    _safe_print(f"{'ID':<18} {'SEV':<5} {'STATUS':<8} {'ROLE':<12} {'NOTICED_IN':<14} TITLE")
    _safe_print("-" * 80)
    for fm in items:
        _safe_print(
            f"{fm.get('id', ''):<18} "
            f"{fm.get('severity', ''):<5} "
            f"{fm.get('status', ''):<8} "
            f"{fm.get('role', ''):<12} "
            f"{fm.get('noticed_in', ''):<14} "
            f"{_safe_col(fm.get('title', ''), 50)}"
        )

    return {"ok": True, "msg": f"{len(items)} issue(s)"}


def cmd_issue_close(args: Any) -> dict:
    """Handler for `takkub issue close`."""
    issues_dir = _resolve_issues_dir(getattr(args, "issues_dir", None))
    try:
        path = close_issue(
            args.id,
            note=getattr(args, "note", "") or "",
            issues_dir=issues_dir,
        )
    except ValueError as exc:
        return {"ok": False, "msg": str(exc)}

    print(f"closed: {path}")
    return {"ok": True, "msg": f"closed {args.id}"}


def cmd_issue_show(args: Any) -> dict:
    """Handler for `takkub issue show`."""
    issues_dir = _resolve_issues_dir(getattr(args, "issues_dir", None))
    try:
        content = show_issue(args.id, issues_dir=issues_dir)
    except ValueError as exc:
        return {"ok": False, "msg": str(exc)}

    _safe_print(content, end="")
    return {"ok": True, "msg": ""}
