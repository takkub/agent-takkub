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


def sanitize_win32_mb_shims() -> list[str]:
    """On Windows, extensionless POSIX shell scripts named 'mb' (created by npm
    or bash installers under %APPDATA%\\npm\\mb or ~/.local/bin/mb) break Win32
    SearchPathW / ShellExecute resolution (issue #156). Because the file has
    no extension, Win32 finds 'mb' as a literal match before checking PATHEXT
    (.cmd/.exe), and then ShellExecute pops the Windows 'Select an app to open'
    dialog because extensionless files have no registered verb.

    Renaming extensionless 'mb' to 'mb.sh' when 'mb.cmd' exists resolves 'mb'
    cleanly to 'mb.cmd' for all Win32 apps (agy.exe, codex.exe, cmd.exe).
    """
    if sys.platform != "win32":
        return []

    import os
    import shutil
    from pathlib import Path

    cleaned: list[str] = []
    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "mb")
    candidates.append(Path.home() / ".local" / "bin" / "mb")

    for cand in candidates:
        if cand.is_file() and not cand.name.endswith(
            (".cmd", ".exe", ".bat", ".ps1", ".sh", ".sh_bak")
        ):
            cmd_sibling = cand.with_name("mb.cmd")
            if cmd_sibling.is_file() or shutil.which("mb.cmd"):
                target = cand.with_name("mb.sh")
                try:
                    if target.exists():
                        target.unlink()
                    cand.rename(target)
                    cleaned.append(str(cand))
                except OSError:
                    pass
    return cleaned
