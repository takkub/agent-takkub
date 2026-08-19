"""In-memory process tracker (docs/v2/07_SCHEDULER_RESOURCE_RUNTIME.md
§12-14). Pure and platform-agnostic on purpose: liveness/crash detection is
injected as an `is_alive` callable so this module never imports
psutil/ctypes/`job_object_manager` directly — the caller wraps
`job_object_manager`/PaneState read-only and supplies liveness, keeping
`core` importable standalone (`core-is-bottom-layer` import-linter
contract)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum


class ProcessStatus(StrEnum):
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    DONE = "done"
    CRASHED = "crashed"


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    pid: int
    agent_id: str
    project_id: str
    task_id: str
    pane_id: str
    provider_id: str | None
    account_id: str | None
    started_at: float
    status: ProcessStatus = ProcessStatus.RUNNING


class ProcessRegistry:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._records: dict[int, ProcessRecord] = {}

    def register(
        self,
        *,
        pid: int,
        agent_id: str,
        project_id: str,
        task_id: str,
        pane_id: str,
        provider_id: str | None = None,
        account_id: str | None = None,
    ) -> ProcessRecord:
        record = ProcessRecord(
            pid=pid,
            agent_id=agent_id,
            project_id=project_id,
            task_id=task_id,
            pane_id=pane_id,
            provider_id=provider_id,
            account_id=account_id,
            started_at=self._clock(),
        )
        with self._lock:
            self._records[pid] = record
        return record

    def unregister(self, pid: int) -> ProcessRecord | None:
        with self._lock:
            return self._records.pop(pid, None)

    def mark(self, pid: int, status: ProcessStatus) -> ProcessRecord | None:
        with self._lock:
            current = self._records.get(pid)
            if current is None:
                return None
            updated = replace(current, status=status)
            self._records[pid] = updated
            return updated

    def get(self, pid: int) -> ProcessRecord | None:
        with self._lock:
            return self._records.get(pid)

    def for_project(self, project_id: str) -> list[ProcessRecord]:
        with self._lock:
            return [record for record in self._records.values() if record.project_id == project_id]

    def snapshot(self) -> list[ProcessRecord]:
        with self._lock:
            return list(self._records.values())

    def detect_stale(self, is_alive: Callable[[int], bool]) -> list[ProcessRecord]:
        """Crash-recovery inspect (§14): RUNNING records whose pid no longer
        answers `is_alive` are marked INTERRUPTED (never silently dropped)
        and returned, so the caller can decide resume-vs-mark-interrupted
        against a loaded checkpoint."""
        newly_interrupted: list[ProcessRecord] = []
        with self._lock:
            for pid, record in list(self._records.items()):
                if record.status != ProcessStatus.RUNNING:
                    continue
                try:
                    alive = is_alive(pid)
                except Exception:
                    alive = False
                if not alive:
                    updated = replace(record, status=ProcessStatus.INTERRUPTED)
                    self._records[pid] = updated
                    newly_interrupted.append(updated)
        return newly_interrupted
