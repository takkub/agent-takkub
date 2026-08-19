"""Bounded, single-holder `mb` escape hatch for browser-role shards (#304).

`pane_guard.py` hard-denies a browser-role shard (`qa#N` / `critic#N` /
`designer#N`) from touching `mb` unconditionally — every shard is meant to
get its own Playwright MCP + isolated browser profile instead (#92). But
when that MCP genuinely never connects (#146: root cause unproven — leading
hypothesis is concurrent MCP cold-start timing under N-way shard spawn, see
docs/audit/2026-08-04-issue-146-playwright-shards.md) the shard has no way
to open a browser at all: the sanctioned path is dead and the only fallback
is blocked. That is #304's "ทางตัน" (dead end).

`mb`'s own client is hardcoded to one CDP endpoint (127.0.0.1:9222 — see
`pane_guard.py`'s `_MB_INVOKE` comment and docs/reviews/2026-07-24-123-mb-
native-chrome.md's "ข้อจำกัดคงเดิม") — there is no per-shard port, so two
shards driving `mb` concurrently WOULD drive the same Chrome tab, exactly
the collision #92 exists to prevent. Loosening the guard to a blanket allow
would reintroduce that bug under a different name. Instead this module hands
out one single, explicit, time-boxed grant at a time: a shard calls
`request()` (via `takkub mcp-fallback request`) only after it has already
confirmed via ToolSearch that the browser MCP tools are unavailable — never
speculatively. If nothing else currently holds the grant it gets one;
otherwise it's denied with the current holder + remaining seconds, never a
guess. `pane_guard.classify()` consults `is_granted()` through a caller-
supplied callback in its one existing mb-shard-deny branch only — no
unconditional per-command I/O, so pane_guard.py itself stays I/O-free
(see its module docstring).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

# Long enough for one `mb` action (go/shot/snap/click) to complete; short
# enough that a forgotten/stuck grant self-clears without needing an
# explicit release. Not the same "3 minutes" this issue's point 4 is about
# shortening — this is a grant DURATION, not a retry-wait.
DEFAULT_TTL_S = 180


def _lock_path() -> Path:
    # Global, not per-project: CDP 9222 is one endpoint per cockpit instance
    # (docs/reviews/2026-07-24-123-mb-native-chrome.md) regardless of which
    # project's shard is asking, so the mutex must be too.
    return config.RUNTIME_DIR / "mcp-fallback-mb-lock.json"


@dataclass(frozen=True, slots=True)
class FallbackGrant:
    granted: bool
    role: str
    holder: str | None
    expires_at: float | None
    reason: str


def _read_lock() -> dict | None:
    path = _lock_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_lock(data: dict) -> None:
    config.ensure_runtime()
    _lock_path().write_text(json.dumps(data), encoding="utf-8")


def request(role: str, project: str, reason: str = "", ttl_s: int = DEFAULT_TTL_S) -> FallbackGrant:
    """Request the single, time-boxed `mb` fallback grant for *role*.

    Grants immediately if nothing holds it or the previous holder's grant
    expired; refreshes if *role* already holds it; otherwise denies with the
    current holder so the reason is concrete, never a guess.
    """
    now = time.time()
    lock = _read_lock()
    if lock and lock.get("holder") != role and lock.get("expires_at", 0) > now:
        remaining = int(lock["expires_at"] - now)
        return FallbackGrant(
            granted=False,
            role=role,
            holder=lock.get("holder"),
            expires_at=lock.get("expires_at"),
            reason=(
                f"mb ถูกใช้อยู่โดย {lock.get('holder')} อีก {remaining}s ถึงจะปล่อย "
                "(กันชนกันบน CDP 9222 ตัวเดียว #92) — รอแล้วลองใหม่ หรือใช้ Playwright MCP ต่อ"
            ),
        )
    expires_at = now + max(1, int(ttl_s))
    _write_lock(
        {
            "holder": role,
            "project": project,
            "granted_at": now,
            "expires_at": expires_at,
            "reason": reason,
        }
    )
    return FallbackGrant(granted=True, role=role, holder=role, expires_at=expires_at, reason=reason)


def is_granted(role: str) -> bool:
    """Whether *role* currently holds the (unexpired) `mb` fallback grant."""
    lock = _read_lock()
    if not lock or lock.get("holder") != role:
        return False
    return lock.get("expires_at", 0) > time.time()


def status() -> dict | None:
    """Current holder info, or `None` if nothing is held or it expired."""
    lock = _read_lock()
    if not lock or lock.get("expires_at", 0) <= time.time():
        return None
    return lock
