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


_pending: dict[Any, Future] = {}


def call_bounded(key: Any, fn: Callable[[], Any], *, timeout: float, default: Any = None) -> Any:
    """`fn()` on the pool, waited on for at most `timeout` seconds (#1044).

    For file/socket reads the Qt main thread does on a timer or inside
    `done()`: normally microseconds, but 5-18 s each while the disk is wedged
    (prod 2026-11-20: 159 stalls in 20 min, every stack ending in `stat` /
    `read_text`). On timeout `default` is returned and the call keeps running;
    the next call with the same `key` picks up its result (or waits on it
    again) instead of starting another — so a stuck disk holds one worker per
    key, never a pile. An exception from `fn` returns `default` as well.

    `key` must identify the question being asked (include the arguments): a
    late result is handed to the next caller with that key.
    """
    with _lock:
        fut = _pending.get(key)
    if fut is None:
        fut = submit(fn)
        with _lock:
            _pending[key] = fut
    try:
        value = fut.result(timeout=timeout)
    except TimeoutError:
        return default
    except Exception:
        value = default
    with _lock:
        if _pending.get(key) is fut:
            del _pending[key]
    return value
