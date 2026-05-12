"""Minimal: spawn claude, write Enter immediately (no read interference)."""
import os
import subprocess
import time
import threading

import winpty
from agent_takkub.config import find_claude_executable


def main():
    exe = find_claude_executable()
    cmd = subprocess.list2cmdline([exe, "--dangerously-skip-permissions"])

    proc = winpty.PtyProcess.spawn(
        cmd,
        dimensions=(36, 110),
        cwd=os.getcwd(),
        backend=winpty.Backend.WinPTY,
    )
    started = time.time()
    print(f"[test] @{time.time()-started:.2f}s spawned, alive={proc.isalive()}", flush=True)

    # spawn a reader thread that drains output continuously
    output_bytes = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                d = proc.read(4096)
            except EOFError:
                if not proc.isalive():
                    print(f"[reader] @{time.time()-started:.2f}s EOF, dead", flush=True)
                    return
                time.sleep(0.04)
                continue
            if d:
                b = d.encode("utf-8", "replace") if isinstance(d, str) else d
                output_bytes.append(b)
            else:
                time.sleep(0.03)
        print(f"[reader] @{time.time()-started:.2f}s stop", flush=True)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    # wait 2s then write Enter
    time.sleep(2)
    print(f"[test] @{time.time()-started:.2f}s alive={proc.isalive()}, writing Enter...", flush=True)
    if proc.isalive():
        proc.write("\r")  # str, not bytes
        print(f"[test] @{time.time()-started:.2f}s write returned", flush=True)
    else:
        print(f"[test] @{time.time()-started:.2f}s already dead before write!", flush=True)
        return

    # let claude proceed for 4 more seconds
    time.sleep(4)
    print(f"[test] @{time.time()-started:.2f}s alive={proc.isalive()}, total_bytes={sum(len(b) for b in output_bytes)}", flush=True)
    print("[test] last 300 chars of output:")
    all_out = b"".join(output_bytes)
    print(all_out[-300:].decode("utf-8", "replace"))

    stop.set()
    proc.terminate(force=True)


if __name__ == "__main__":
    main()
