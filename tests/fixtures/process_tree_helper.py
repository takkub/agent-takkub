"""Cooperative helper for the scoped Windows Job Object integration test."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _sleep() -> None:
    time.sleep(120)


def main() -> None:
    mode = sys.argv[1]
    if mode == "grandchild":
        _sleep()
        return
    if mode == "child":
        pid_file = Path(sys.argv[2])
        root_pid = int(sys.argv[3])
        grandchild = subprocess.Popen(
            [sys.executable, __file__, "grandchild"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        tmp = pid_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"root": root_pid, "child": os.getpid(), "grandchild": grandchild.pid}),
            encoding="utf-8",
        )
        os.replace(tmp, pid_file)
        _sleep()
        return
    if mode == "root":
        go_file = Path(sys.argv[2])
        pid_file = Path(sys.argv[3])
        deadline = time.time() + 30
        while not go_file.exists() and time.time() < deadline:
            time.sleep(0.01)
        if not go_file.exists():
            raise SystemExit(2)
        subprocess.Popen(
            [sys.executable, __file__, "child", str(pid_file), str(os.getpid())],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _sleep()
        return
    raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
