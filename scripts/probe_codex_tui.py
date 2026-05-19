"""Spawn `codex` in a PTY, wait for the TUI to render, then dump the
pyte screen so we can identify stable ready-prompt markers (mirrors
of claude's 'bypass permissions on' / 'shift+tab to cycle').

Run from repo root:
    python scripts/probe_codex_tui.py

The script kills codex after the dump so it doesn't sit attached.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import threading
import time

import pyte
import winpty

sys.stdout = io.TextIOWrapper(
    sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True, write_through=True
)
sys.stderr = io.TextIOWrapper(
    sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True, write_through=True
)


def main() -> int:
    codex = shutil.which("codex")
    if codex is None:
        print("codex not on PATH", file=sys.stderr)
        return 1
    print(f">>> found codex at {codex}")

    cols, rows = 110, 36
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)

    env = os.environ.copy()
    print(">>> spawning codex via ConPTY")
    try:
        proc = winpty.PtyProcess.spawn(
            codex,
            dimensions=(rows, cols),
            cwd=os.getcwd(),
            env=env,
            backend=winpty.Backend.ConPTY,
        )
    except Exception as e:
        print(f"spawn failed: {e!r}", file=sys.stderr)
        return 2
    print(">>> spawned, entering read loop")

    # Watchdog: hard-kill codex after 12 s so blocking read() can't pin
    # us forever. proc.read() on Windows can block past the deadline
    # check if codex sits silently after its splash.
    def _watchdog() -> None:
        time.sleep(12.0)
        try:
            proc.terminate(force=True)
            print(">>> watchdog: forced codex terminate", flush=True)
        except Exception:
            pass

    threading.Thread(target=_watchdog, daemon=True).start()

    # Codex 0.130 first-run shows an "Update available!" modal blocking
    # the splash with options 1/2/3. After 1.5 s we send "3\n" (Skip
    # until next version) so the probe captures the *real* ready prompt
    # underneath. If the user has already cleared the modal on a prior
    # run, the "3" is just ignored.
    def _dismiss_modals() -> None:
        # Codex first-launch shows up to two modals back to back:
        # (a) Trust directory? — default "1. Yes, continue", press Enter
        # (b) Update available — gone after we upgraded to 0.131.0
        # Press Enter twice with a gap, then a third to be safe.
        for delay in (1.5, 3.0, 4.5):
            time.sleep(delay - (delay - 1.5))
            try:
                proc.write("\r")
                print(f">>> sent Enter at t+{delay}s", flush=True)
            except Exception:
                pass
            time.sleep(1.5)

    threading.Thread(target=_dismiss_modals, daemon=True).start()

    deadline = time.time() + 12.0
    bytes_total = 0
    raw_buf = bytearray()
    while time.time() < deadline:
        try:
            data = proc.read(4096)
        except EOFError:
            if not proc.isalive():
                break
            time.sleep(0.04)
            continue
        except Exception:
            time.sleep(0.04)
            continue
        if not data:
            time.sleep(0.05)
            continue
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        bytes_total += len(data)
        raw_buf.extend(data)
        try:
            stream.feed(data)
        except Exception:
            pass

    print(">>> exited read loop, terminating codex")
    try:
        proc.terminate(force=True)
    except Exception as e:
        print(f">>> terminate raised: {e!r}")
    print(">>> terminated")

    print(f"=== bytes read: {bytes_total} ===")
    print("=== pyte screen (rendered lines) ===")
    rendered_any = False
    for i, line in enumerate(screen.display):
        stripped = line.rstrip()
        if stripped:
            rendered_any = True
            print(f"  {i:02d}| {stripped!r}")
    if not rendered_any:
        print("  (pyte screen entirely whitespace)")
    print("=== raw byte sample (first 600 bytes, repr) ===")
    print(repr(bytes(raw_buf[:600])))
    print("=== end ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
