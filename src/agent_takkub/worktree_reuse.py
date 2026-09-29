"""Reuse a role's own unmerged worktree for a fix-loop re-assign.

`assign --isolation worktree --backlog <same card>` used to mint a brand-new,
empty worktree every round while the real work sat in the previous one. This
registry remembers (project, backlog card, base role) -> the worktree created
for it, so the next isolated assign for the same card lands in that checkout
instead. A worktree that was merged, cleaned or deleted is never reused.

Storage: ``RUNTIME_DIR/worktree_reuse/<project>.json`` (own domain folder,
atomic write). Everything here is best-effort: any failure means "create new".
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .backlog import _atomic_write
from .config import RUNTIME_DIR
from .path_safe import safe_segment

_MAX_ENTRIES = 100
_MAX_AGE_S = 30 * 86400
_LOCK = threading.Lock()


def _path(project: str) -> Path:
    return RUNTIME_DIR / "worktree_reuse" / f"{safe_segment(project)}.json"


def _key(backlog_id: str, base_role: str) -> str:
    return f"{backlog_id}|{base_role}"


def _load(project: str) -> dict:
    try:
        raw = json.loads(_path(project).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def record(project: str, backlog_id: str, base_role: str, info) -> None:
    """Remember *info* (a WorktreeInfo) as the worktree for this card+role."""
    if not backlog_id:
        return
    try:
        with _LOCK:
            data = _load(project)
            data[_key(backlog_id, base_role)] = {"info": info.as_dict(), "ts": time.time()}
            now = time.time()
            for k in [k for k, v in data.items() if now - v.get("ts", 0) > _MAX_AGE_S]:
                data.pop(k, None)
            for k in sorted(data, key=lambda k: data[k].get("ts", 0), reverse=True)[_MAX_ENTRIES:]:
                data.pop(k, None)
            _atomic_write(_path(project), json.dumps(data, ensure_ascii=False, indent=1))
    except Exception:
        pass


def forget(project: str, backlog_id: str, base_role: str) -> None:
    """Drop the entry (``assign --fresh-worktree``)."""
    try:
        with _LOCK:
            data = _load(project)
            if data.pop(_key(backlog_id, base_role), None) is not None:
                _atomic_write(_path(project), json.dumps(data, ensure_ascii=False, indent=1))
    except Exception:
        pass


def find(project: str, backlog_id: str, base_role: str, mgr=None):
    """The still-live, unmerged WorktreeInfo recorded for this card+role, or
    None. "Live" = checkout dir still exists and its branch is still there;
    "unmerged" = it has commits beyond the repo HEAD, or it is still an
    untouched checkout at its own base commit (a merged branch has a tip that
    is neither ahead of HEAD nor equal to its base — never reused)."""
    if not backlog_id:
        return None
    try:
        from .worktree_manager import WorktreeInfo, WorktreeManager

        entry = _load(project).get(_key(backlog_id, base_role))
        if not entry:
            return None
        info = WorktreeInfo.from_dict(entry["info"])
        if not (Path(info.path) / ".git").exists():
            return None
        mgr = mgr or WorktreeManager()
        tip = mgr._run(["-C", info.path, "rev-parse", "--verify", "-q", "HEAD"], None)
        if not tip.ok:
            return None
        branch = mgr._run(["-C", info.path, "rev-parse", "--abbrev-ref", "HEAD"], None)
        if not branch.ok or branch.stdout.strip() != info.branch:
            return None
        if (
            tip.stdout.strip() != info.base_sha
            and mgr.commits_ahead(info.git_root, info.branch) <= 0
        ):
            return None  # merged into base
        return info
    except Exception:
        return None
