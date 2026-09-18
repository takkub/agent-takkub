"""One shared background thread pool for short, fire-and-forget probes the Qt
main thread must not block on (#658).

`threading.Thread(...).start()` from the main thread is itself a stall under
GIL contention: `start()` waits for the new thread to signal it is running,
and with N PTY reader threads competing for the GIL that wait was captured
at ~1 s (prod 2026-09-18, `agent_pane._refresh_token_meter`). A pool's
workers already exist, so `submit()` is a queue append.

Stdlib-only leaf — importable from any layer.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

_MAX_WORKERS = 4
_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
    """Run `fn(*args, **kwargs)` on the shared pool. Exceptions are captured
    on the returned Future (never propagate to the caller's thread)."""
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(
                    max_workers=_MAX_WORKERS, thread_name_prefix="cockpit-bg"
                )
    return _pool.submit(fn, *args, **kwargs)
