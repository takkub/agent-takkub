"""Deterministic Performance & Reliability v2 stress/fault harness.

This harness is deliberately self-contained and never connects to a running
cockpit.  It uses production admission, delivery, writer, dedupe, reader, and
spawn-FIFO primitives with controlled fakes, then writes a reproducible JSON
record plus a short Markdown summary under TAKKUB_ARTIFACTS_DIR.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import psutil  # noqa: E402
from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer  # noqa: E402

from agent_takkub.performance_settings import preset  # noqa: E402
from agent_takkub.pty_session import (  # noqa: E402
    PtyWriteMessage,
    WritePriority,
    _ReaderThread,
    _WriterThread,
)
from agent_takkub.resource_governor import (  # noqa: E402
    GovernorLimits,
    ResourceClass,
    ResourceGovernor,
)
from agent_takkub.spawn_engine import PaneRegistry, SpawnEngineMixin  # noqa: E402
from agent_takkub.task_delivery import (  # noqa: E402
    DeliveryManager,
    NoticeDeduper,
    make_notice_id,
)

HEARTBEAT_SLA_MS = 250.0


def _result(scenario: str, passed: bool, **metrics: object) -> dict:
    return {"scenario": scenario, "passed": bool(passed), "metrics": metrics}


def scenario_a(_seed: int) -> dict:
    limits = GovernorLimits(max_heavy_global=4, max_heavy_per_project=2)
    governor = ResourceGovernor(limits, sampler=lambda: (10.0, 80.0, 100))
    active = []
    admitted_ids: list[str] = []
    max_heavy = 0
    max_per_project = 0

    def admitted(token) -> None:
        active.append(token)
        admitted_ids.append(token.task_id)

    for project_idx in range(10):
        project = f"project-{project_idx}"
        for pane_idx in range(3):
            task_id = f"{project}/pane-{pane_idx}"
            decision = governor.request_slot(
                project_id=project,
                pane_id=f"pane-{pane_idx}",
                task_id=task_id,
                resource_class=ResourceClass.HEAVY,
            )
            if decision.allowed:
                admitted(decision.token)
            else:
                governor.enqueue(
                    project_id=project,
                    pane_id=f"pane-{pane_idx}",
                    task_id=task_id,
                    resource_class=ResourceClass.HEAVY,
                    on_admitted=admitted,
                )
            snap = governor.snapshot()
            max_heavy = max(max_heavy, snap["active_heavy_tasks"])
            max_per_project = max(
                max_per_project, *(snap["active_tasks_by_project"].values() or [0])
            )

    while active:
        token = active.pop(0)
        governor.release_slot(token)
        governor.dispatch_waiting()
        snap = governor.snapshot()
        max_heavy = max(max_heavy, snap["active_heavy_tasks"])
        max_per_project = max(max_per_project, *(snap["active_tasks_by_project"].values() or [0]))

    snap = governor.snapshot()
    passed = (
        len(set(admitted_ids)) == 30
        and max_heavy <= limits.max_heavy_global
        and max_per_project <= limits.max_heavy_per_project
        and snap["active_heavy_tasks"] == 0
        and snap["queued_resource_tasks"] == 0
    )
    return _result(
        "STR-A",
        passed,
        admitted=len(set(admitted_ids)),
        max_heavy=max_heavy,
        max_per_project=max_per_project,
        final_active=snap["active_heavy_tasks"],
        final_queued=snap["queued_resource_tasks"],
    )


class _FloodProc:
    def __init__(self, chunks: int, chunk_size: int) -> None:
        self._left = chunks
        self._chunk = b"x" * chunk_size

    def read(self, _size: int) -> bytes:
        if self._left <= 0:
            return b""
        self._left -= 1
        return self._chunk

    def isalive(self) -> bool:
        return self._left > 0


def scenario_b(_seed: int) -> dict:
    app = QCoreApplication.instance() or QCoreApplication([])
    batches: list[int] = []
    readers = [
        _ReaderThread(
            _FloodProc(250, 4096),
            on_data=lambda batch: batches.append(len(batch)),
            batch_ms=25,
            batch_bytes=32 * 1024,
        )
        for _ in range(6)
    ]
    for reader in readers:
        reader.start()
    ticks: list[float] = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    loop = QEventLoop()
    timer.start()
    QTimer.singleShot(500, loop.quit)
    loop.exec()
    timer.stop()
    for reader in readers:
        reader.wait(2_000)
    gaps_ms = [(b - a) * 1000 for a, b in pairwise(ticks)]
    p95 = statistics.quantiles(gaps_ms, n=20)[18] if len(gaps_ms) >= 20 else max(gaps_ms, default=0)
    max_gap = max(gaps_ms, default=0)
    perf = preset("balanced", logical_cpus=16, total_memory_gb=32)
    passed = (
        bool(ticks)
        and max_gap <= HEARTBEAT_SLA_MS
        and perf.hidden_render_ms > 16
        and bool(batches)
        and max(batches) <= 32 * 1024
    )
    del app
    return _result(
        "STR-B",
        passed,
        heartbeat_samples=len(ticks),
        heartbeat_p95_ms=round(p95, 3),
        heartbeat_max_ms=round(max_gap, 3),
        heartbeat_sla_ms=HEARTBEAT_SLA_MS,
        reader_batches=len(batches),
        max_batch_bytes=max(batches, default=0),
        hidden_render_ms=perf.hidden_render_ms,
    )


def _hysteresis_scenario(name: str, samples: list[tuple[float, float, int]]) -> dict:
    values = iter(samples)
    governor = ResourceGovernor(
        GovernorLimits(
            max_heavy_global=1,
            max_heavy_per_project=1,
            cpu_pause_percent=85,
            cpu_resume_percent=65,
            min_available_ram_percent=20,
            resume_ram_percent=25,
        ),
        sampler=lambda: next(values),
    )
    states = []
    for _ in samples:
        states.append(bool(governor.sample()["overloaded"]))
    denied = governor.request_slot(
        project_id="p",
        pane_id="build",
        task_id="heavy",
        resource_class=ResourceClass.HEAVY,
    )
    # The final sample is recovery, so admission must succeed after hysteresis.
    passed = states == [True, True, False] and denied.allowed
    if denied.token:
        governor.release_slot(denied.token)
    return _result(name, passed, overload_states=states, recovered_admission=denied.allowed)


def scenario_c(_seed: int) -> dict:
    return _hysteresis_scenario("STR-C", [(95, 60, 100), (75, 60, 100), (60, 60, 100)])


def scenario_d(_seed: int) -> dict:
    return _hysteresis_scenario("STR-D", [(20, 10, 100), (20, 22, 100), (20, 30, 100)])


def scenario_e(_seed: int, stall_seconds: float) -> dict:
    manager = DeliveryManager(default_ttl_sec=30)
    delivery = manager.create(
        task_id="stall-submit",
        project_id="p",
        pane_id="backend",
        session_generation=1,
        payload="FULL PAYLOAD",
    )
    native_payload_writes: list[str] = []
    if manager.begin_write(delivery.delivery_id, 1):
        native_payload_writes.append(delivery.payload)
    manager.mark_written(delivery.delivery_id)
    manager.begin_submit(delivery.delivery_id, 1)
    time.sleep(max(0.0, stall_seconds))
    for _ in range(3):
        manager.retry_enter(delivery.delivery_id, 1)
    passed = native_payload_writes == ["FULL PAYLOAD"] and delivery.enter_retries == 3
    return _result(
        "STR-E",
        passed,
        payload_write_count=len(native_payload_writes),
        enter_retries=delivery.enter_retries,
        injected_stall_seconds=stall_seconds,
    )


class _BlockedWriterProc:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.entered.set()
        self.release.wait(timeout=3)
        self.writes.append(data)


def scenario_f(_seed: int) -> dict:
    proc = _BlockedWriterProc()
    generation = [2]
    writer = _WriterThread(
        proc, maxsize=8, control_reserve=2, generation_getter=lambda: generation[0]
    )
    writer.start()
    writer.write("block", priority=WritePriority.CONTROL)
    proc.entered.wait(timeout=1)
    accepted = 0
    max_depth = 0
    for idx in range(100):
        accepted += int(writer.write(f"task-{idx}", priority=WritePriority.TASK))
        max_depth = max(max_depth, writer.queue_depth)
    writer.write(
        PtyWriteMessage("old-generation", priority=WritePriority.CONTROL, session_generation=1)
    )
    writer.write(PtyWriteMessage("cancelled", priority=WritePriority.CONTROL, cancelled=True))
    proc.release.set()
    deadline = time.time() + 3
    while writer.queue_depth and time.time() < deadline:
        time.sleep(0.01)
    writer.request_stop()
    writer.wait(2_000)
    passed = (
        max_depth <= 8
        and writer.queue_full_count > 0
        and "old-generation" not in proc.writes
        and "cancelled" not in proc.writes
        and writer.stale_drop_count >= 2
    )
    return _result(
        "STR-F",
        passed,
        accepted_task_writes=accepted,
        max_queue_depth=max_depth,
        configured_cap=8,
        queue_full_count=writer.queue_full_count,
        stale_drop_count=writer.stale_drop_count,
        native_writes=proc.writes,
    )


class _RecordingProc:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)


def scenario_g(_seed: int) -> dict:
    manager = DeliveryManager(default_ttl_sec=30)
    delivery = manager.create(
        task_id="task-a",
        project_id="p",
        pane_id="backend",
        session_generation=1,
        payload="TASK A",
    )
    manager.begin_write(delivery.delivery_id, 1)
    proc = _RecordingProc()
    current_generation = [2]
    writer = _WriterThread(proc, maxsize=8, generation_getter=lambda: current_generation[0])
    writer.write(
        PtyWriteMessage(
            delivery.payload,
            priority=WritePriority.TASK,
            session_generation=1,
            delivery_id=delivery.delivery_id,
            validator=lambda: manager.validate_for_write(
                delivery.delivery_id, current_generation[0]
            ),
        )
    )
    manager.cancel_for_session("p", "backend", 1)
    writer.start()
    deadline = time.time() + 1
    while writer.queue_depth and time.time() < deadline:
        time.sleep(0.01)
    writer.request_stop()
    writer.wait(1_000)
    return _result(
        "STR-G",
        proc.writes == [],
        replacement_session_task_a_write_count=len(proc.writes),
        stale_drop_count=writer.stale_drop_count,
    )


def scenario_h(_seed: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="takkub-dedupe-") as td:
        dedupe_path = Path(td) / "notice.json"
        notice_id = make_notice_id("p", "qa", "task", 7)
        first = NoticeDeduper(dedupe_path)
        deliveries = sum(int(first.mark_once(notice_id)) for _ in range(10))
        restored = NoticeDeduper(dedupe_path)
        deliveries += int(restored.mark_once(notice_id))
        return _result(
            "STR-H",
            deliveries == 1,
            attempted=11,
            delivered=deliveries,
            duplicates_prevented=first.duplicate_count + restored.duplicate_count,
            survived_reload=restored.seen(notice_id),
        )


class _Pane:
    session = None


class _SpawnHarness(SpawnEngineMixin):
    def __init__(self) -> None:
        self._registry = PaneRegistry()
        self.max_parallel = 0
        self.parallel = 0
        self.started: list[str] = []
        self.finished: list[str] = []

    def _resolve_project(self, project):
        return project or "default"

    def _project_panes(self, project):
        return self._registry.panes_by_project.setdefault(project, {})

    def _preserve_pending_spawn_initial_task(self, *_args) -> None:
        return None

    def spawn(self, role, cwd=None, project=None, **_kwargs):
        if self._spawn_in_progress:
            raise AssertionError("spawn collision")
        self._spawn_in_progress = True
        self.parallel += 1
        self.max_parallel = max(self.max_parallel, self.parallel)
        key = f"{project}/{role}"
        self.started.append(key)

        def finish() -> None:
            self.finished.append(key)
            self.parallel -= 1
            self._spawn_in_progress = False
            self._drain_spawn_queue()

        QTimer.singleShot(5, finish)
        return True, "spawned"


def scenario_i(seed: int) -> dict:
    randomizer = random.Random(seed)
    app = QCoreApplication.instance() or QCoreApplication([])
    harness = _SpawnHarness()
    items = []
    for idx in range(10):
        project = f"project-{idx}"
        harness._project_panes(project)["backend"] = _Pane()
        items.append(("backend", None, project, False, 0, None))
    randomizer.shuffle(items)
    harness._spawn_queue = deque(items)
    harness._drain_spawn_queue()
    loop = QEventLoop()
    deadline = time.monotonic() + 3

    def poll() -> None:
        if len(harness.finished) == len(items) or time.monotonic() >= deadline:
            loop.quit()

    poll_timer = QTimer()
    poll_timer.setInterval(5)
    poll_timer.timeout.connect(poll)
    poll_timer.start()
    QTimer.singleShot(3_100, loop.quit)
    loop.exec()
    poll_timer.stop()
    del app
    passed = (
        harness.max_parallel == 1
        and len(harness.started) == 10
        and len(harness.finished) == 10
        and not harness._spawn_queue
    )
    return _result(
        "STR-I",
        passed,
        projects=10,
        max_parallel_spawns=harness.max_parallel,
        started=len(harness.started),
        finished=len(harness.finished),
        queue_remaining=len(harness._spawn_queue),
    )


def _git_manifest() -> dict:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10, check=False
        )
        return result.stdout.strip()

    dirty = run("status", "--porcelain")
    return {
        "sha": run("rev-parse", "HEAD"),
        "dirty": bool(dirty),
        "dirty_files": dirty.splitlines(),
    }


def run_all(seed: int, stall_seconds: float) -> dict:
    started = datetime.now(tz=UTC)
    scenarios = [
        scenario_a(seed),
        scenario_b(seed),
        scenario_c(seed),
        scenario_d(seed),
        scenario_e(seed, stall_seconds),
        scenario_f(seed),
        scenario_g(seed),
        scenario_h(seed),
        scenario_i(seed),
    ]
    vm = psutil.virtual_memory()
    return {
        "schema_version": 1,
        "run_id": started.strftime("%Y%m%dT%H%M%S.%fZ"),
        "started_at": started.isoformat(),
        "ended_at": datetime.now(tz=UTC).isoformat(),
        "seed": seed,
        "pass": all(row["passed"] for row in scenarios),
        "heartbeat_sla_ms": HEARTBEAT_SLA_MS,
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "logical_cpus": psutil.cpu_count(logical=True),
            "physical_cpus": psutil.cpu_count(logical=False),
            "total_memory_bytes": vm.total,
            "available_memory_bytes": vm.available,
        },
        "git": _git_manifest(),
        "configuration": {
            "stall_seconds": stall_seconds,
            "isolated": True,
            "connects_to_running_cockpit": False,
        },
        "scenarios": scenarios,
    }


def write_artifacts(report: dict, output_root: Path) -> tuple[Path, Path]:
    run_dir = output_root / "performance-reliability" / report["run_id"]
    run_dir.mkdir(parents=True, exist_ok=False)
    json_path = run_dir / "stress-results.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    rows = [
        "# Performance & Reliability stress run",
        "",
        f"- Run: `{report['run_id']}`",
        f"- Seed: `{report['seed']}`",
        f"- Result: **{'PASS' if report['pass'] else 'FAIL'}**",
        f"- UI heartbeat SLA: `{report['heartbeat_sla_ms']} ms`",
        "",
        "| Scenario | Result | Metrics |",
        "|---|---|---|",
    ]
    for result in report["scenarios"]:
        metrics = json.dumps(result["metrics"], ensure_ascii=False, sort_keys=True)
        rows.append(
            f"| {result['scenario']} | {'PASS' if result['passed'] else 'FAIL'} | `{metrics}` |"
        )
    md_path = run_dir / "stress-summary.md"
    md_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20260814)
    parser.add_argument("--stall-seconds", type=float, default=1.0)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output_root = args.output_dir or Path(
        os.environ.get("TAKKUB_ARTIFACTS_DIR", ROOT / "runtime" / "exports")
    )
    report = run_all(args.seed, args.stall_seconds)
    json_path, md_path = write_artifacts(report, output_root)
    print(json.dumps({"pass": report["pass"], "json": str(json_path), "summary": str(md_path)}))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
