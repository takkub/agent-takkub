"""Diagnostic: enumerate top-level windows and report class + title + pid."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def list_windows(only_visible: bool = True) -> list[tuple[int, str, str, int]]:
    out: list[tuple[int, str, str, int]] = []

    def _cb(hwnd: int, _lp: int) -> bool:
        if only_visible and not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, cls, 128)
        n = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, title_buf, n + 1)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append((int(hwnd), cls.value, title_buf.value, int(pid.value)))
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return out


if __name__ == "__main__":
    import sys

    only_console_ish = "--console" in sys.argv
    rows = list_windows(only_visible=True)
    for hwnd, cls, title, pid in rows:
        if only_console_ish:
            low = (cls + " " + title).lower()
            if not any(
                k in low
                for k in ("console", "cmd", "conhost", "winpty", "claude", "pseudo")
            ):
                continue
        print(f"hwnd={hwnd:>10}  pid={pid:>6}  class={cls:<32}  title={title!r}")
