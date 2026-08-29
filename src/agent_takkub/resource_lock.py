"""Advisory per-project resource locks — `takkub lock` / `takkub unlock` (#430).

Two panes in the same project building into the same output dir (`web/.next`
from a `devops` `npm run dist` and a `qa` `npm --prefix web run build`, seen
live 2026-08-29) starve each other on the build lock for 20+ minutes and
look "hung" to the watchdog. Nothing in the cockpit knew they were fighting.

This is the smallest thing that fixes the coordination gap: a named,
project-scoped lock file under ``RUNTIME_DIR/locks/<project>/<name>.json``
that a pane takes before touching a shared resource and releases after. It
is *advisory* — nothing stops a pane that never calls `takkub lock` — but
the role docs make wrapping shared builds/tests in it the rule, and the lock
carries the holder's role so a blocked pane can tell Lead exactly who it is
waiting on instead of guessing from CPU graphs.

Pure leaf (stdlib only): `cli.py` calls it directly, no orchestrator socket
— a lock must still be inspectable/releasable when the cockpit is wedged
(the exact situation #430 describes).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_TTL_S = 30 * 60  # a build that holds a lock for >30 min is stuck, not busy
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class LockError(ValueError):
    pass


@dataclass(frozen=True)
class LockInfo:
    name: str
    holder: str
    pid: int
    acquired_ts: float
    ttl_s: float
    note: str = ""

    @property
    def expires_ts(self) -> float:
        return self.acquired_ts + self.ttl_s

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_ts

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.acquired_ts)


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise LockError(
            f"invalid lock name {name!r} — letters/digits/._- only (e.g. web-build, db-migrate)"
        )
    return name


def _project_dir(runtime_dir: Path, project: str | None) -> Path:
    ns = (project or "default").strip() or "default"
    ns = re.sub(r"[^A-Za-z0-9._-]", "_", ns)
    return runtime_dir / "locks" / ns


def lock_path(runtime_dir: Path, project: str | None, name: str) -> Path:
    return _project_dir(runtime_dir, project) / f"{validate_name(name)}.json"


def read_lock(path: Path) -> LockInfo | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return LockInfo(
            name=str(data.get("name") or path.stem),
            holder=str(data.get("holder") or "?"),
            pid=int(data.get("pid") or 0),
            acquired_ts=float(data.get("acquired_ts") or 0.0),
            ttl_s=float(data.get("ttl_s") or DEFAULT_TTL_S),
            note=str(data.get("note") or ""),
        )
    except (TypeError, ValueError):
        return None


def _write_exclusive(path: Path, info: LockInfo) -> bool:
    """Atomic create — False when someone else holds the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(asdict(info), fh, ensure_ascii=False)
    return True


def try_acquire(
    runtime_dir: Path,
    project: str | None,
    name: str,
    holder: str,
    *,
    ttl_s: float = DEFAULT_TTL_S,
    note: str = "",
    pid: int | None = None,
    now: float | None = None,
) -> tuple[bool, LockInfo]:
    """One attempt. Returns ``(True, mine)`` or ``(False, theirs)``. A stale
    (expired) lock is reclaimed in place; re-acquiring your own lock just
    refreshes it."""
    path = lock_path(runtime_dir, project, name)
    now = now if now is not None else time.time()
    mine = LockInfo(
        name=validate_name(name),
        holder=holder or "?",
        pid=int(pid if pid is not None else os.getpid()),
        acquired_ts=now,
        ttl_s=float(ttl_s),
        note=note,
    )
    for _attempt in range(3):
        if _write_exclusive(path, mine):
            return True, mine
        current = read_lock(path)
        if current is None or current.expired(now) or current.holder == mine.holder:
            # corrupt / stale / already ours → replace
            try:
                path.unlink()
            except OSError:
                pass
            continue
        return False, current
    current = read_lock(path)
    return False, current or mine


def acquire(
    runtime_dir: Path,
    project: str | None,
    name: str,
    holder: str,
    *,
    ttl_s: float = DEFAULT_TTL_S,
    wait_s: float = 0.0,
    note: str = "",
    poll_s: float = 2.0,
    sleep=time.sleep,
) -> tuple[bool, LockInfo, float]:
    """Acquire, optionally blocking up to *wait_s*. Returns ``(ok, info,
    waited_s)`` — on failure *info* is the current holder."""
    start = time.time()
    while True:
        ok, info = try_acquire(runtime_dir, project, name, holder, ttl_s=ttl_s, note=note)
        waited = time.time() - start
        if ok or waited >= wait_s:
            return ok, info, waited
        sleep(min(poll_s, max(0.05, wait_s - waited)))


def release(
    runtime_dir: Path, project: str | None, name: str, holder: str, *, force: bool = False
) -> tuple[bool, str]:
    path = lock_path(runtime_dir, project, name)
    current = read_lock(path)
    if current is None:
        if path.exists():
            path.unlink(missing_ok=True)
            return True, f"removed corrupt lock '{name}'"
        return True, f"lock '{name}' was not held (no-op)"
    if current.holder != holder and not force and not current.expired():
        return False, (
            f"lock '{name}' is held by {current.holder} (pid {current.pid}, "
            f"{int(current.age_s())}s) — only the holder (or lead --force) may release it"
        )
    path.unlink(missing_ok=True)
    return True, f"released lock '{name}'" + (
        "" if current.holder == holder else f" (was held by {current.holder})"
    )


def list_locks(runtime_dir: Path, project: str | None) -> list[LockInfo]:
    d = _project_dir(runtime_dir, project)
    if not d.is_dir():
        return []
    out: list[LockInfo] = []
    for p in sorted(d.glob("*.json")):
        info = read_lock(p)
        if info is not None:
            out.append(info)
    return out


def format_lock_line(info: LockInfo, now: float | None = None) -> str:
    state = "STALE" if info.expired(now) else "held"
    line = f"{info.name:20s} {state:5s} by {info.holder} (pid {info.pid}) {int(info.age_s(now))}s"
    if info.note:
        line += f" — {info.note}"
    return line
