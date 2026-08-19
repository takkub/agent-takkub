"""pause()/checkpoint()/resume()/cancel() interface for one agent runtime
(docs/v2/07_SCHEDULER_RESOURCE_RUNTIME.md §11). `checkpoint()` calls the
injected `checkpoint_fn` — normally a `core.conversation` checkpoint call —
ONLY when TAKKUB_V2_SCHEDULER is on; flag off makes it a no-op so wiring
this in ahead of a real caller changes nothing."""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum

from .flag import v2_scheduler_enabled

_log = logging.getLogger(__name__)


class RunState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    DONE = "done"


class AgentRuntimeControl:
    """One instance per running agent/task. `pause()`/`resume()` only flip a
    flag that `may_dispatch_new()` gates — they never touch a process that's
    already running (§10/§11: graceful, not a kill)."""

    def __init__(self, *, checkpoint_fn: Callable[[], None] | None = None) -> None:
        self._state = RunState.RUNNING
        self._checkpoint_fn = checkpoint_fn

    @property
    def state(self) -> RunState:
        return self._state

    def may_dispatch_new(self) -> bool:
        return self._state == RunState.RUNNING

    def pause(self) -> None:
        if self._state == RunState.RUNNING:
            self._state = RunState.PAUSED

    def resume(self) -> None:
        if self._state == RunState.PAUSED:
            self._state = RunState.RUNNING

    def cancel(self) -> None:
        if self._state not in (RunState.CANCELLED, RunState.DONE):
            self._state = RunState.CANCELLED

    def mark_done(self) -> None:
        if self._state != RunState.CANCELLED:
            self._state = RunState.DONE

    def checkpoint(self) -> bool:
        if not v2_scheduler_enabled() or self._checkpoint_fn is None:
            return False
        try:
            self._checkpoint_fn()
            return True
        except Exception:
            _log.exception("core.scheduling.runtime_control checkpoint_fn failed (fail-open)")
            return False
