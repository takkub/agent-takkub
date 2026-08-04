"""Read-only spawn-queue wedge diagnostics (#141).

`takkub doctor`'s default checks are pure-logic — no orchestrator TCP (see
doctor.py's module docstring). That's exactly why doctor could report "all
checks passed" while 4 `takkub assign` calls sat wedged in the spawn arbiter's
FIFO queue: the queue and the `spawn_in_progress` arbiter flag live only in
the running Orchestrator's in-memory state (spawn_engine.py's PaneRegistry),
which a pure-logic check can never see.

This module is the live-diagnostics data plane for `takkub doctor --live`. It
polls Orchestrator's EXISTING spawn-engine attributes (`_spawn_queue`,
`_spawn_in_progress` — both owned by spawn_engine.SpawnEngineMixin) on a
timer and remembers *when* each state was first observed, so an "age" can be
reported even though spawn_engine.py itself stores no timestamps. It never
writes to Orchestrator/PaneRegistry state — only its own private bookkeeping.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from PyQt6.QtCore import QObject, QTimer

# Resolution of the age estimate — how often the background poll samples
# orchestrator state to catch a False→True (or empty→non-empty) transition.
_POLL_INTERVAL_MS = 2_000


@dataclass(frozen=True)
class SpawnQueueSnapshot:
    queue_depth: int
    spawn_in_progress: bool
    spawn_in_progress_age_s: float | None
    oldest_queued_age_s: float | None


class SpawnQueueHealthMonitor(QObject):
    """Polls one Orchestrator's spawn-queue state and tracks transition ages.

    Read-only: only ever reads `orch._spawn_queue` / `orch._spawn_in_progress`
    (existing spawn_engine.py properties) — never assigns to them.
    """

    def __init__(
        self,
        orchestrator: object,
        parent: QObject | None = None,
        interval_ms: int = _POLL_INTERVAL_MS,
    ) -> None:
        super().__init__(parent)
        self._orch = orchestrator
        self._in_progress_since: float | None = None
        self._queue_head_key: object = None
        self._queue_head_since: float | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        self._poll()

    def _poll(self) -> None:
        # Diagnostic-only: never let a malformed/mocked orchestrator (test
        # doubles included) crash the poll timer or the Qt main loop.
        try:
            now = time.monotonic()
            in_progress = bool(getattr(self._orch, "_spawn_in_progress", False))
            if in_progress:
                if self._in_progress_since is None:
                    self._in_progress_since = now
            else:
                self._in_progress_since = None

            queue = getattr(self._orch, "_spawn_queue", None)
            depth = len(queue) if queue is not None else 0
            if depth > 0:
                head = queue[0]
                if head != self._queue_head_key:
                    self._queue_head_key = head
                    self._queue_head_since = now
            else:
                self._queue_head_key = None
                self._queue_head_since = None
        except Exception:
            pass

    def snapshot(self) -> SpawnQueueSnapshot:
        """Best-effort fresh sample, then report queue depth + tracked ages."""
        self._poll()
        now = time.monotonic()
        queue = getattr(self._orch, "_spawn_queue", None)
        depth = len(queue) if queue is not None else 0
        return SpawnQueueSnapshot(
            queue_depth=depth,
            spawn_in_progress=bool(getattr(self._orch, "_spawn_in_progress", False)),
            spawn_in_progress_age_s=(
                round(now - self._in_progress_since, 1)
                if self._in_progress_since is not None
                else None
            ),
            oldest_queued_age_s=(
                round(now - self._queue_head_since, 1)
                if self._queue_head_since is not None
                else None
            ),
        )
