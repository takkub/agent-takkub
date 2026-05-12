"""List all top-level windows owned by a given pid."""
import ctypes
import ctypes.wintypes as wt
import sys

user32 = ctypes.WinDLL("user32", use_last_error=True)
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def main():
    target = int(sys.argv[1])
    rows = []

    def cb(hwnd: int, _lp: int) -> bool:
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == target:
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, cls, 128)
            n = user32.GetWindowTextLengthW(hwnd)
            tbuf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, tbuf, n + 1)
            vis = bool(user32.IsWindowVisible(hwnd))
            rows.append((hwnd, cls.value, tbuf.value, vis))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    for hwnd, cls, title, vis in rows:
        print(f"  hwnd={hwnd}  visible={vis}  class={cls!r}  title={title!r}")
    if not rows:
        print("  (no top-level windows for this pid)")


if __name__ == "__main__":
    main()
