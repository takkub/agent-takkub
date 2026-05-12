"""Headless test: spawn claude via pywinpty, dump first 10s of output."""
import os
import subprocess
import sys
import time

import winpty


def main() -> int:
    from agent_takkub.config import find_claude_executable

    exe = find_claude_executable()
    cmd = subprocess.list2cmdline([exe, "--dangerously-skip-permissions"])
    print(f"[test] spawning: {cmd}", flush=True)

    proc = winpty.PtyProcess.spawn(
        cmd,
        dimensions=(36, 110),
        cwd=os.getcwd(),
    )
    print(f"[test] spawned, alive={proc.isalive()}", flush=True)

    deadline = time.time() + 10
    total = 0
    while time.time() < deadline:
        try:
            data = proc.read(4096)
        except EOFError:
            print("[test] EOF", flush=True)
            break
        if data:
            if isinstance(data, str):
                b = data.encode("utf-8", "replace")
            else:
                b = data
            total += len(b)
            sys.stdout.buffer.write(b)
            sys.stdout.flush()
        else:
            time.sleep(0.05)

    print(f"\n[test] total={total} bytes, alive={proc.isalive()}", flush=True)
    proc.terminate(force=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
