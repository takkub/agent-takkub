"""Headless: spawn claude, wait, write Enter (b'\\r'), see if trust prompt advances."""
import os
import subprocess
import sys
import time

import winpty
from agent_takkub.config import find_claude_executable


def main():
    exe = find_claude_executable()
    cmd = subprocess.list2cmdline([exe, "--dangerously-skip-permissions"])
    print(f"[test] spawning: {cmd}", flush=True)

    proc = winpty.PtyProcess.spawn(
        cmd,
        dimensions=(36, 110),
        cwd=os.getcwd(),
        backend=winpty.Backend.WinPTY,
    )
    print(f"[test] spawned backend=WinPTY, reading 8s of output...", flush=True)

    end = time.time() + 8
    total_pre = 0
    started = time.time()
    last_alive_change = None
    last_alive = True
    while time.time() < end:
        if proc.isalive() != last_alive:
            last_alive = not last_alive
            print(f"[test] @{time.time()-started:.2f}s alive flipped to {last_alive}", flush=True)
        try:
            d = proc.read(4096)
        except EOFError:
            if not proc.isalive():
                print(f"[test] EOF (process dead) at {time.time()-started:.2f}s before write", flush=True)
                break
            time.sleep(0.04)
            continue
        if d:
            total_pre += len(d) if isinstance(d, (bytes, bytearray)) else len(d.encode())
        else:
            time.sleep(0.05)
    print(f"[test] pre-write: {total_pre} bytes, elapsed {time.time()-started:.2f}s, alive={proc.isalive()}", flush=True)
    if not proc.isalive():
        print("[test] not writing — process already dead", flush=True)
        return

    # write Enter to accept trust prompt (option 1 is preselected)
    print("[test] writing b'\\r'", flush=True)
    proc.write(b"\r")
    print(f"[test] write returned (no exception). alive: {proc.isalive()}", flush=True)

    # capture next 6s of output (should be claude proceeding past trust)
    end2 = time.time() + 6
    print("[test] reading 6s after write:", flush=True)
    print("---")
    sys.stdout.flush()
    total_post = 0
    while time.time() < end2:
        try:
            d = proc.read(4096)
        except EOFError:
            if not proc.isalive():
                print("\n[test] EOF (process dead) after write", flush=True)
                break
            time.sleep(0.04)
            continue
        if d:
            b = d.encode("utf-8", "replace") if isinstance(d, str) else d
            total_post += len(b)
            sys.stdout.buffer.write(b)
            sys.stdout.flush()
        else:
            time.sleep(0.05)
    print(f"\n---\n[test] post-write bytes: {total_post}, alive: {proc.isalive()}", flush=True)
    proc.terminate(force=True)


if __name__ == "__main__":
    main()
