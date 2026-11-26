"""Start the cockpit when the user signs in to the computer.

The OS registration is the only state — there is no settings file that could
disagree with it. Per-user, no admin rights:

* Windows: a value under ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``.
  Task Manager's "Startup apps" switch is honoured (it writes a separate
  ``StartupApproved`` flag instead of removing the value).
* macOS: a LaunchAgent plist in ``~/Library/LaunchAgents`` with ``RunAtLoad``.
  The file is only written/removed — never ``launchctl load``-ed, which would
  start a second cockpit right now.
* anything else: unsupported (``is_supported()`` is False; the UI disables
  the switch).

A dev checkout and an installed build register under different names, so a
machine that has both never has one overwrite the other's entry. No Qt
dependency, so the settings UI and tests share it.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

from . import config

_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def _is_dev() -> bool:
    return config.DATA_HOME == config.REPO_ROOT


def entry_name() -> str:
    """Registry value name (Windows)."""
    return "TakkubCockpit-dev" if _is_dev() else "TakkubCockpit"


def agent_label() -> str:
    """LaunchAgent label (macOS)."""
    return "com.takkub.cockpit.dev" if _is_dev() else "com.takkub.cockpit"


def is_supported(platform: str | None = None) -> bool:
    return (platform or sys.platform) in ("win32", "darwin")


def launch_args(platform: str | None = None) -> list[str]:
    """The command that starts this cockpit: the interpreter it is running on
    now, so the same venv (and therefore the same DATA_HOME) comes back.
    Windows prefers the sibling ``pythonw.exe`` so no console window opens."""
    exe = Path(sys.executable)
    if (platform or sys.platform) == "win32":
        windowless = exe.with_name("pythonw.exe")
        if windowless.is_file():
            exe = windowless
    return [str(exe), "-m", "agent_takkub"]


def launch_command(platform: str | None = None) -> str:
    """`launch_args` as one command line (what the Run key stores)."""
    return subprocess.list2cmdline(launch_args(platform))


# ── Windows ──────────────────────────────────────────────────────────────


def _win_get(name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return str(value)


def _win_user_disabled(name: str) -> bool:
    """True when the user switched the entry off in Task Manager / Settings →
    Startup apps: the Run value stays, a StartupApproved flag with an odd
    first byte marks it disabled."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_APPROVED_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return False
    return isinstance(value, bytes) and len(value) > 0 and bool(value[0] & 1)


def _win_set(name: str, command: str) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
    # Turning it on here must win over an earlier "disabled" in Task Manager.
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _WIN_APPROVED_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, name)
    except OSError:
        pass


def _win_delete(name: str) -> None:
    import winreg

    for key_path in (_WIN_RUN_KEY, _WIN_APPROVED_KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except OSError:
            pass


# ── macOS ────────────────────────────────────────────────────────────────


def mac_plist_path(agents_dir: Path | None = None) -> Path:
    base = agents_dir or (Path.home() / "Library" / "LaunchAgents")
    return base / f"{agent_label()}.plist"


def _mac_get(agents_dir: Path | None = None) -> list[str] | None:
    try:
        with mac_plist_path(agents_dir).open("rb") as fh:
            payload = plistlib.load(fh)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    if not isinstance(payload, dict) or not payload.get("RunAtLoad"):
        return None
    args = payload.get("ProgramArguments")
    return [str(a) for a in args] if isinstance(args, list) and args else None


def _mac_set(args: list[str], agents_dir: Path | None = None) -> None:
    target = mac_plist_path(agents_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": agent_label(),
        "ProgramArguments": list(args),
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }
    tmp = target.with_name(target.name + ".tmp")
    with tmp.open("wb") as fh:
        plistlib.dump(payload, fh)
    tmp.replace(target)


def _mac_delete(agents_dir: Path | None = None) -> None:
    try:
        mac_plist_path(agents_dir).unlink()
    except FileNotFoundError:
        pass


# ── public API ───────────────────────────────────────────────────────────


def registered_command() -> str | None:
    """The command currently registered to run at sign-in, or None."""
    if sys.platform == "win32":
        return _win_get(entry_name())
    if sys.platform == "darwin":
        args = _mac_get()
        return subprocess.list2cmdline(args) if args else None
    return None


def is_enabled() -> bool:
    if sys.platform == "win32":
        name = entry_name()
        return _win_get(name) is not None and not _win_user_disabled(name)
    if sys.platform == "darwin":
        return _mac_get() is not None
    return False


def set_enabled(enabled: bool) -> bool:
    """Register / unregister this cockpit for sign-in. Returns True when the
    OS state now matches `enabled`; False on an unsupported platform or when
    the OS refused the write. Never raises."""
    if not is_supported():
        return False
    try:
        if sys.platform == "win32":
            if enabled:
                _win_set(entry_name(), launch_command())
            else:
                _win_delete(entry_name())
        elif enabled:
            _mac_set(launch_args())
        else:
            _mac_delete()
    except OSError:
        return False
    return is_enabled() == bool(enabled)


def refresh_if_stale() -> bool:
    """Re-point an enabled entry at the interpreter running now (the venv
    moved, or an entry written by an older install). Called once at boot;
    does nothing when the switch is off. Returns True when it rewrote."""
    try:
        if not is_enabled() or registered_command() == launch_command():
            return False
    except OSError:
        return False
    return set_enabled(True)
