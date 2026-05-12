"""Spawn claude, wait 3s for trust prompt to render, write Enter, see what happens."""
import os
import subprocess
import sys
import time

import winpty
from agent_takkub.config import find_claude_executable


def main():
    exe = find_claude_executable()
    cmd = subprocess.list2cmdline([exe, "--dangerously-skip-permissions"])
    print(f"[test] spawning", flush=True)

    proc = winpty.PtyProcess.spawn(
        cmd,
        dimensions=(36, 110),
        cwd=os.getcwd(),
        backend=winpty.Backend.WinPTY,
    )
    started = time.time()
    print(f"[test] spawned, alive={proc.isalive()}", flush=True)

    # Read for 3s only
    end = time.time() + 3
    pre = 0
    while time.time() < end:
        try:
            d = proc.read(4096)
        except EOFError:
            if not proc.isalive():
                print(f"[test] dead at {time.time()-started:.2f}s", flush=True)
                return
            time.sleep(0.04)
            continue
        if d:
            pre += len(d) if isinstance(d, (bytes, bytearray)) else len(d.encode())
        else:
            time.sleep(0.03)
    print(f"[test] {time.time()-started:.2f}s: pre-write {pre} bytes, alive={proc.isalive()}", flush=True)

    # Write Enter
    try:
        proc.write(b"\r")
        print(f"[test] {time.time()-started:.2f}s: wrote Enter ok", flush=True)
    except Exception as e:
        print(f"[test] {time.time()-started:.2f}s: write failed: {e}", flush=True)
        return

    # Read for 5 more seconds
    end2 = time.time() + 5
    post = b""
    while time.time() < end2:
        try:
            d = proc.read(4096)
        except EOFError:
            if not proc.isalive():
                print(f"[test] dead post-write at {time.time()-started:.2f}s after {len(post)} bytes", flush=True)
                break
            time.sleep(0.04)
            continue
        if d:
            b = d.encode("utf-8", "replace") if isinstance(d, str) else d
            post += b
        else:
            time.sleep(0.03)

    print(f"[test] post-write bytes: {len(post)}, alive={proc.isalive()}", flush=True)
    print(f"[test] last 200 chars of post-write:")
    print(post[-200:].decode("utf-8", "replace"))
    proc.terminate(force=True)


if __name__ == "__main__":
    main()
