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
    # #322: an agent that hit its provider usage limit and is waiting for the
    # window to reset — distinct from PAUSED (an operator/scheduler decision
    # via pause()) so a plain resume() call can never wake a pane still
    # mid-limit, and so a watchdog/scheduler reading `.state` can tell "we
    # chose to hold this back" apart from "the provider is refusing work
    # right now" without a side-channel flag. Mirrors the pre-V2 watchdog
    # behavior (`PaneState.rate_limited_until` + `limit_autoresume.py`'s
    # park/wake) as a first-class scheduler state instead of a
    # watchdog-only special case (issue #322 V2-alignment ask).
    LIMIT_WAIT = "limit_wait"
    CANCELLED = "cancelled"
    DONE = "done"


class AgentRuntimeControl:
    """One instance per running agent/task. `pause()`/`resume()` only flip a
    flag that `may_dispatch_new()` gates — they never touch a process that's
    already running (§10/§11: graceful, not a kill)."""

    def __init__(self, *, checkpoint_fn: Callable[[], None] | None = None) -> None:
        self._state = RunState.RUNNING
        self._checkpoint_fn = checkpoint_fn
        self._limit_reset_at: float | None = None

    @property
    def state(self) -> RunState:
        return self._state

    @property
    def limit_reset_at(self) -> float | None:
        """Epoch the usage window is expected to reset, while `state` is
        LIMIT_WAIT; None otherwise (including after resume_from_limit())."""
        return self._limit_reset_at

    def may_dispatch_new(self) -> bool:
        return self._state == RunState.RUNNING

    def pause(self) -> None:
        if self._state == RunState.RUNNING:
            self._state = RunState.PAUSED

    def resume(self) -> None:
        if self._state == RunState.PAUSED:
            self._state = RunState.RUNNING

    def park_for_limit(self, reset_at: float) -> None:
        """Enter LIMIT_WAIT — called the instant the provider's usage-limit
        banner is detected (signal (a)/(b) in `limit_autoresume.py`). A
        no-op from CANCELLED/DONE (terminal states) or if already parked, so
        a repeated detection tick can call this unconditionally."""
        if self._state in (RunState.RUNNING, RunState.PAUSED):
            self._state = RunState.LIMIT_WAIT
            self._limit_reset_at = reset_at

    def resume_from_limit(self) -> None:
        """Leave LIMIT_WAIT once the window has reset — whether the CLI
        auto-continued on its own (Claude 2.1.234+) or cockpit's own wake
        nudge drove it. A no-op unless currently LIMIT_WAIT, so a caller
        never accidentally resumes a plain operator pause()."""
        if self._state == RunState.LIMIT_WAIT:
            self._state = RunState.RUNNING
            self._limit_reset_at = None

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
