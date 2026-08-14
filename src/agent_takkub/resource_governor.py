"""Application-wide resource admission and fair waiting queue."""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import psutil

from . import performance_settings


class ResourceClass(StrEnum):
    LIGHT = "light"
    NORMAL = "normal"
    HEAVY = "heavy"
    BROWSER = "browser"
    BUILD = "build"
    TEST = "test"
    PACKAGE_INSTALL = "package_install"


@dataclass(frozen=True, slots=True)
class GovernorLimits:
    max_heavy_global: int = 4
    max_heavy_per_project: int = 2
    max_browser_global: int = 2
    max_build_global: int = 2
    max_test_global: int = 2
    max_package_install_global: int = 1
    cpu_pause_percent: float = 85.0
    cpu_resume_percent: float = 65.0
    min_available_ram_percent: float = 20.0
    resume_ram_percent: float = 25.0

    @classmethod
    def from_environment(cls) -> GovernorLimits:
        persisted = performance_settings.load()

        def _int(name: str, default: int) -> int:
            try:
                return max(1, int(os.environ.get(name, default)))
            except (TypeError, ValueError):
                return default

        def _float(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default

        return cls(
            max_heavy_global=_int("TAKKUB_MAX_HEAVY_GLOBAL", persisted.max_heavy_global),
            max_heavy_per_project=_int(
                "TAKKUB_MAX_HEAVY_PER_PROJECT", persisted.max_heavy_per_project
            ),
            max_browser_global=_int("TAKKUB_MAX_BROWSER_GLOBAL", persisted.max_browser_global),
            max_build_global=_int("TAKKUB_MAX_BUILD_GLOBAL", persisted.max_build_global),
            max_test_global=_int("TAKKUB_MAX_TEST_GLOBAL", persisted.max_test_global),
            max_package_install_global=_int(
                "TAKKUB_MAX_PACKAGE_INSTALL_GLOBAL", persisted.max_package_install_global
            ),
            cpu_pause_percent=_float("TAKKUB_CPU_PAUSE_PERCENT", persisted.cpu_pause_percent),
            cpu_resume_percent=_float("TAKKUB_CPU_RESUME_PERCENT", persisted.cpu_resume_percent),
            min_available_ram_percent=_float(
                "TAKKUB_MIN_AVAILABLE_RAM_PERCENT", persisted.min_available_ram_percent
            ),
            resume_ram_percent=_float("TAKKUB_RESUME_RAM_PERCENT", persisted.resume_ram_percent),
        )


@dataclass(frozen=True, slots=True)
class ResourceToken:
    token_id: str
    project_id: str
    pane_id: str
    task_id: str
    resource_class: ResourceClass
    acquired_at: float


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    allowed: bool
    reason: str = ""
    retry_after_ms: int = 1000
    token: ResourceToken | None = None


@dataclass(slots=True)
class QueuedTask:
    queue_id: str
    project_id: str
    pane_id: str
    task_id: str
    resource_class: ResourceClass
    queued_at: float
    on_admitted: Callable[[ResourceToken], None] | None = None


_HEAVY_CLASSES = {
    ResourceClass.HEAVY,
    ResourceClass.BROWSER,
    ResourceClass.BUILD,
    ResourceClass.TEST,
    ResourceClass.PACKAGE_INSTALL,
}


class ResourceGovernor:
    """Non-blocking admission controller with hysteresis and project fairness.

    ``sample`` uses ``psutil.cpu_percent(interval=None)`` exclusively, so it is
    safe to call from a Qt timer. Waiting callbacks are invoked by ``dispatch_waiting``
    in the caller's thread; the cockpit calls it from the GUI thread.
    """

    def __init__(
        self,
        limits: GovernorLimits | None = None,
        *,
        sampler: Callable[[], tuple] | None = None,
        clock: Callable[[], float] = time.time,
        event_sink: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.limits = limits or GovernorLimits.from_environment()
        self._sampler = sampler or self._sample_psutil
        self._clock = clock
        self._event_sink = event_sink
        self._cpu_percent = 0.0
        self._available_ram_percent = 100.0
        self._process_count = 0
        self._available_memory_bytes = 0
        self._total_memory_bytes = 0
        self._overloaded = False
        self._tokens: dict[str, ResourceToken] = {}
        self._waiting: OrderedDict[str, deque[QueuedTask]] = OrderedDict()
        self._project_cursor = 0
        self._lock = threading.RLock()
        # Warm psutil's non-blocking CPU sampler; the first value is undefined.
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

    @staticmethod
    def _sample_psutil() -> tuple[float, float, int, int, int]:
        cpu = float(psutil.cpu_percent(interval=None))
        vm = psutil.virtual_memory()
        available_pct = 100.0 * float(vm.available) / max(1.0, float(vm.total))
        try:
            processes = len(psutil.pids())
        except Exception:
            processes = 0
        return cpu, available_pct, processes, int(vm.available), int(vm.total)

    def _emit(self, event: str, **details: object) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(event, details)
        except Exception:
            pass

    def update_limits(self, limits: GovernorLimits) -> None:
        """Apply new limits without dropping active tokens or queued work."""
        with self._lock:
            self.limits = limits
        self._emit(
            "resource_limits_updated",
            limits={field: getattr(limits, field) for field in limits.__dataclass_fields__},
        )

    def sample(self) -> dict:
        try:
            sampled = self._sampler()
            cpu, ram, processes = sampled[:3]
            available_bytes = int(sampled[3]) if len(sampled) >= 5 else self._available_memory_bytes
            total_bytes = int(sampled[4]) if len(sampled) >= 5 else self._total_memory_bytes
        except Exception:
            cpu, ram, processes = (
                self._cpu_percent,
                self._available_ram_percent,
                self._process_count,
            )
            available_bytes = self._available_memory_bytes
            total_bytes = self._total_memory_bytes
        with self._lock:
            self._cpu_percent = max(0.0, min(100.0, float(cpu)))
            self._available_ram_percent = max(0.0, min(100.0, float(ram)))
            self._process_count = max(0, int(processes))
            self._available_memory_bytes = max(0, available_bytes)
            self._total_memory_bytes = max(0, total_bytes)
            if self._overloaded:
                if (
                    self._cpu_percent <= self.limits.cpu_resume_percent
                    and self._available_ram_percent >= self.limits.resume_ram_percent
                ):
                    self._overloaded = False
            elif (
                self._cpu_percent >= self.limits.cpu_pause_percent
                or self._available_ram_percent < self.limits.min_available_ram_percent
            ):
                self._overloaded = True
            return self.snapshot()

    def overloaded(self) -> bool:
        with self._lock:
            return self._overloaded

    def _counts(self) -> tuple[int, dict[str, int], dict[ResourceClass, int]]:
        heavy = 0
        by_project: dict[str, int] = {}
        by_class: dict[ResourceClass, int] = {}
        for token in self._tokens.values():
            by_class[token.resource_class] = by_class.get(token.resource_class, 0) + 1
            if token.resource_class in _HEAVY_CLASSES:
                heavy += 1
                by_project[token.project_id] = by_project.get(token.project_id, 0) + 1
        return heavy, by_project, by_class

    def _denial_reason(self, project_id: str, resource_class: ResourceClass) -> str:
        if resource_class in {ResourceClass.LIGHT, ResourceClass.NORMAL}:
            return ""
        if self._overloaded:
            if self._available_ram_percent < self.limits.min_available_ram_percent:
                return "memory_low"
            return "cpu_high"
        heavy, by_project, by_class = self._counts()
        if heavy >= self.limits.max_heavy_global:
            return "heavy_global_limit"
        if by_project.get(project_id, 0) >= self.limits.max_heavy_per_project:
            return "heavy_project_limit"
        class_limits = {
            ResourceClass.BROWSER: self.limits.max_browser_global,
            ResourceClass.BUILD: self.limits.max_build_global,
            ResourceClass.TEST: self.limits.max_test_global,
            ResourceClass.PACKAGE_INSTALL: self.limits.max_package_install_global,
        }
        limit = class_limits.get(resource_class)
        if limit is not None and by_class.get(resource_class, 0) >= limit:
            return f"{resource_class.value}_global_limit"
        return ""

    def request_slot(
        self,
        *,
        project_id: str,
        pane_id: str,
        resource_class: ResourceClass,
        task_id: str,
    ) -> AdmissionDecision:
        with self._lock:
            reason = self._denial_reason(project_id, resource_class)
            if reason:
                self._emit(
                    "resource_gate_block",
                    project_id=project_id,
                    pane_id=pane_id,
                    task_id=task_id,
                    resource_class=resource_class.value,
                    reason=reason,
                )
                return AdmissionDecision(False, reason)
            token = ResourceToken(
                uuid.uuid4().hex,
                project_id,
                pane_id,
                task_id,
                resource_class,
                self._clock(),
            )
            self._tokens[token.token_id] = token
        self._emit(
            "resource_slot_acquired",
            token_id=token.token_id,
            project_id=project_id,
            pane_id=pane_id,
            task_id=task_id,
            resource_class=resource_class.value,
        )
        return AdmissionDecision(True, token=token)

    def release_slot(self, token: ResourceToken | str | None) -> bool:
        if token is None:
            return False
        token_id = token if isinstance(token, str) else token.token_id
        with self._lock:
            released = self._tokens.pop(token_id, None)
        if released is not None:
            self._emit(
                "resource_slot_released",
                token_id=released.token_id,
                project_id=released.project_id,
            )
        return released is not None

    def enqueue(
        self,
        *,
        project_id: str,
        pane_id: str,
        task_id: str,
        resource_class: ResourceClass,
        on_admitted: Callable[[ResourceToken], None] | None = None,
    ) -> str:
        item = QueuedTask(
            uuid.uuid4().hex,
            project_id,
            pane_id,
            task_id,
            resource_class,
            self._clock(),
            on_admitted,
        )
        with self._lock:
            self._waiting.setdefault(project_id, deque()).append(item)
        self._emit(
            "resource_task_queued",
            queue_id=item.queue_id,
            project_id=project_id,
            pane_id=pane_id,
            task_id=task_id,
            resource_class=resource_class.value,
        )
        return item.queue_id

    def cancel_waiting(
        self,
        *,
        project_id: str,
        pane_id: str | None = None,
        task_id: str | None = None,
    ) -> int:
        removed = 0
        with self._lock:
            queue = self._waiting.get(project_id)
            if queue is None:
                return 0
            kept: deque[QueuedTask] = deque()
            for item in queue:
                if (pane_id is None or item.pane_id == pane_id) and (
                    task_id is None or item.task_id == task_id
                ):
                    removed += 1
                else:
                    kept.append(item)
            if kept:
                self._waiting[project_id] = kept
            else:
                self._waiting.pop(project_id, None)
        if removed:
            self._emit(
                "resource_tasks_cancelled",
                project_id=project_id,
                pane_id=pane_id,
                task_id=task_id,
                count=removed,
            )
        return removed

    def dispatch_waiting(self, max_dispatch: int | None = None) -> list[ResourceToken]:
        """Admit waiting work round-robin by project, FIFO within a project."""
        admitted: list[tuple[QueuedTask, ResourceToken]] = []
        with self._lock:
            projects = list(self._waiting)
            if projects:
                start = self._project_cursor % len(projects)
                projects = projects[start:] + projects[:start]
            progress = True
            while projects and progress and (max_dispatch is None or len(admitted) < max_dispatch):
                progress = False
                for project_id in list(projects):
                    if max_dispatch is not None and len(admitted) >= max_dispatch:
                        break
                    queue = self._waiting.get(project_id)
                    if not queue:
                        continue
                    item = queue[0]
                    decision = self.request_slot(
                        project_id=item.project_id,
                        pane_id=item.pane_id,
                        task_id=item.task_id,
                        resource_class=item.resource_class,
                    )
                    if decision.allowed and decision.token is not None:
                        queue.popleft()
                        if not queue:
                            self._waiting.pop(project_id, None)
                        admitted.append((item, decision.token))
                        progress = True
                self._project_cursor += 1
        for item, token in admitted:
            if item.on_admitted is not None:
                try:
                    item.on_admitted(token)
                except Exception:
                    self.release_slot(token)
        return [token for _, token in admitted]

    def snapshot(self) -> dict:
        with self._lock:
            heavy, by_project, by_class = self._counts()
            return {
                "cpu_percent": self._cpu_percent,
                "available_memory_percent": self._available_ram_percent,
                "available_memory_bytes": self._available_memory_bytes,
                "total_memory_bytes": self._total_memory_bytes,
                "process_count": self._process_count,
                "overloaded": self._overloaded,
                "active_heavy_tasks": heavy,
                "active_tasks_by_project": by_project,
                "active_tasks_by_class": {key.value: value for key, value in by_class.items()},
                "queued_resource_tasks": sum(len(queue) for queue in self._waiting.values()),
                "waiting_tasks": [
                    {
                        "project_id": item.project_id,
                        "pane_id": item.pane_id,
                        "task_id": item.task_id,
                        "resource_class": item.resource_class.value,
                        "queued_at": item.queued_at,
                    }
                    for queue in self._waiting.values()
                    for item in queue
                ],
                "resource_limits": {
                    field: getattr(self.limits, field) for field in self.limits.__dataclass_fields__
                },
            }


def classify_resource(role_name: str, task: str = "") -> ResourceClass:
    role = role_name.split("#", 1)[0].lower()
    text = task.lower()
    if role in {"qa", "critic", "designer"} or any(
        marker in text for marker in ("playwright", "chromium", "browser qa", "lighthouse")
    ):
        return ResourceClass.BROWSER
    if any(marker in text for marker in ("npm install", "npm ci", "pip install", "uv sync")):
        return ResourceClass.PACKAGE_INSTALL
    if any(marker in text for marker in ("next build", "npm run build", "webpack", "vite build")):
        return ResourceClass.BUILD
    if any(marker in text for marker in ("pytest", "npm test", "jest", "test suite")):
        return ResourceClass.TEST
    if role in {"lead", "shell"}:
        return ResourceClass.LIGHT
    return ResourceClass.NORMAL
