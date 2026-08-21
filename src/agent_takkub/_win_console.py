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


def _window_pid(hwnd: int) -> int | None:
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value) or None


def _is_descendant_of(pid: int, root_pid: int, max_depth: int = 16) -> bool:
    """Walk *pid*'s parents looking for *root_pid*.

    Walking UP is deliberate: the alternative — enumerating every descendant
    of the cockpit — is a full process-table scan (this box routinely has 400+
    processes), and this runs on a timer. A parent chain is a handful of hops.
    `max_depth` guards against a cycle in a lying/racy parent table.
    """
    import psutil

    try:
        proc = psutil.Process(pid)
        for _ in range(max_depth):
            proc = proc.parent()
            if proc is None:
                return False
            if proc.pid == root_pid:
                return True
    except Exception:
        # Process exited mid-walk, or access denied — not ours as far as we
        # can prove, and proving it is the whole point of this check.
        return False
    return False


def hide_own_console_windows(root_pid: int, already_seen: set[int]) -> set[int]:
    """Hide console windows belonging to *root_pid*'s process tree.

    Why the ownership check, when spawn-time hiding never needed one: that
    pass only had to cover the few hundred milliseconds around a PTY spawn, so
    "any console window that is new since a snapshot taken a moment ago" was
    safe. Console windows that appear LATER cannot be swept the same way —
    codex opens `pwsh.exe` for every shell tool call it makes, minutes into a
    session (#286 already records pwsh as codex scaffolding), and a blind
    periodic sweep would just as happily hide a terminal the USER opened.

    *already_seen* is a caller-owned set of HWNDs this has already ruled on;
    it is mutated in place and returned. It keeps each sweep to an
    `EnumWindows` plus a parent walk for genuinely new windows only.
    """
    if sys.platform != "win32":
        return already_seen
    fresh = snapshot_console_hwnds() - already_seen
    if not fresh:
        return already_seen
    mine: set[int] = set()
    for hwnd in fresh:
        already_seen.add(hwnd)
        pid = _window_pid(hwnd)
        if pid is not None and _is_descendant_of(pid, root_pid):
            mine.add(hwnd)
    if mine:
        hide_hwnds(mine)
    return already_seen


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
