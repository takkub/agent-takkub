from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil
import pytest

from agent_takkub.job_object_manager import JobObjectManager

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object test")


def _alive(pid: int, created_at: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and abs(process.create_time() - created_at) < 0.01
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _scoped_cleanup(rows: dict[int, float]) -> None:
    for pid, created_at in reversed(list(rows.items())):
        if not _alive(pid, created_at):
            continue
        try:
            psutil.Process(pid).kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


@pytest.mark.parametrize("close_reason", ["pane_close", "application_shutdown"])
def test_job_close_reaps_root_child_and_grandchild(tmp_path: Path, close_reason: str) -> None:
    helper = Path(__file__).parent / "fixtures" / "process_tree_helper.py"
    go_file = tmp_path / "go"
    pid_file = tmp_path / "pids.json"
    root = subprocess.Popen(
        [sys.executable, str(helper), "root", str(go_file), str(pid_file)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    manager = JobObjectManager()
    tracked: dict[int, float] = {root.pid: psutil.Process(root.pid).create_time()}
    started = datetime.now(tz=UTC)
    try:
        assert manager.assign(root.pid), "root process could not be assigned to kill-on-close job"
        go_file.write_text("go", encoding="utf-8")
        deadline = time.time() + 10
        while not pid_file.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert pid_file.exists(), "helper process tree did not become ready"
        pids = json.loads(pid_file.read_text(encoding="utf-8"))
        for pid in pids.values():
            tracked[int(pid)] = psutil.Process(int(pid)).create_time()
        before = {name: _alive(int(pid), tracked[int(pid)]) for name, pid in pids.items()}
        assert before == {"root": True, "child": True, "grandchild": True}

        assert manager.close()
        deadline = time.time() + 10
        while (
            any(_alive(pid, created) for pid, created in tracked.items()) and time.time() < deadline
        ):
            time.sleep(0.05)
        after = {name: _alive(int(pid), tracked[int(pid)]) for name, pid in pids.items()}
        assert after == {"root": False, "child": False, "grandchild": False}

        artifact_root = Path(os.environ.get("TAKKUB_ARTIFACTS_DIR", tmp_path))
        artifact_root.mkdir(parents=True, exist_ok=True)
        artifact = (
            artifact_root / f"process-tree-{close_reason}-{started.strftime('%H%M%S%f')}.json"
        )
        artifact.write_text(
            json.dumps(
                {
                    "close_reason": close_reason,
                    "started_at": started.isoformat(),
                    "ended_at": datetime.now(tz=UTC).isoformat(),
                    "pids": pids,
                    "before": before,
                    "after": after,
                    "pass": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        manager.close()
        _scoped_cleanup(tracked)
        try:
            root.wait(timeout=2)
        except subprocess.TimeoutExpired:
            root.kill()
