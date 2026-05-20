"""Windows-only helper: hide console windows that appear during PTY spawn.

pywinpty (ConPTY or WinPTY backend) often surfaces a `ConsoleWindowClass`
window owned by `conhost.exe` / `cmd.exe` when launching a console app from a
GUI process. Functionally harmless but visually disruptive.

Strategy: snapshot all ConsoleWindowClass HWNDs before spawn, then after
spawn diff against a fresh snapshot and `ShowWindow(hwnd, SW_HIDE)` any new
HWNDs.

Also exports `SUBPROCESS_NO_WINDOW` — a `creationflags` value that callers
pass to `subprocess.run/Popen` so console child processes (git, npm, codex,
gemini, npx) don't flash a conhost window when spawned from the PyQt GUI.
Zero on non-Windows so the same call site works cross-platform.
"""

from __future__ import annotations

import subprocess
import sys

SUBPROCESS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def snapshot_console_hwnds() -> set[int]:
    """Return set of HWNDs (int) for top-level ConsoleWindowClass windows."""
    if sys.platform != "win32":
        return set()

    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    out: set[int] = set()

    def _cb(hwnd: int, _lp: int) -> bool:
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value == "ConsoleWindowClass":
            out.add(int(hwnd))
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return out


def hide_hwnds(hwnds: set[int]) -> int:
    """Hide each HWND. Returns number actually hidden."""
    if sys.platform != "win32" or not hwnds:
        return 0

    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    SW_HIDE = 0
    hidden = 0
    for h in hwnds:
        if user32.IsWindowVisible(h):
            user32.ShowWindow(h, SW_HIDE)
            hidden += 1
    return hidden
