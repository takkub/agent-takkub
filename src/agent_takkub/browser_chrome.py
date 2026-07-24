"""Cockpit-owned Chrome lifecycle for mini-browser on Windows (issue #123).

``mb`` always connects to CDP on ``127.0.0.1:9222``.  Its bundled
``mb-start-chrome`` wrapper is a shell script; on Windows the ``.cmd`` shim
falls through to WSL and never starts Chrome.  The cockpit therefore starts a
native ``chrome.exe`` itself for eligible, non-sharded browser panes.

Only a process started by this object is stopped by :meth:`close`.  If an
operator already has a compatible CDP endpoint on port 9222, it is reused and
left alone at shutdown.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping

from . import config
from .pane_guard import BROWSER_ROLES

MB_CDP_HOST = "127.0.0.1"
MB_CDP_PORT = 9222
_START_TIMEOUT_SEC = 8.0


def find_chrome_executable(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: pathlib.Path | None = None,
) -> str | None:
    """Return a native Chrome/Chromium executable without launching it."""
    platform = platform or sys.platform
    env = env if env is not None else os.environ
    home = home or pathlib.Path.home()

    override = env.get("CHROME_BIN", "").strip()
    if override and pathlib.Path(override).is_file():
        return override

    if platform == "win32":
        candidates = (
            pathlib.Path(env.get("PROGRAMFILES", r"C:\Program Files"))
            / "Google/Chrome/Application/chrome.exe",
            pathlib.Path(env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
            / "Google/Chrome/Application/chrome.exe",
            pathlib.Path(env.get("LOCALAPPDATA", str(home / "AppData/Local")))
            / "Google/Chrome/Application/chrome.exe",
        )
        which_names = ("chrome.exe", "chrome")
    elif platform == "darwin":
        candidates = (
            pathlib.Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            home / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        which_names = ("google-chrome", "chromium")
    else:
        candidates = tuple(
            pathlib.Path(path)
            for path in (
                "/usr/bin/google-chrome",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
            )
        )
        which_names = ("google-chrome", "chromium", "chromium-browser")

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    for name in which_names:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return None


def should_manage_native_chrome(
    base_role: str,
    shard_idx: int | None,
    *,
    platform: str | None = None,
) -> bool:
    """Whether this pane may use cockpit-owned mb Chrome.

    ``mb`` has no configurable client port, so shards must keep using their
    isolated Playwright MCP profiles instead of sharing CDP 9222 (#92).
    """
    return (
        (platform or sys.platform) == "win32" and base_role in BROWSER_ROLES and shard_idx is None
    )


def apply_chrome_bin(env: dict[str, str], base_role: str) -> None:
    """Expose Chrome discovery to every provider-backed browser role."""
    if base_role not in BROWSER_ROLES or env.get("CHROME_BIN"):
        return
    chrome = find_chrome_executable()
    if chrome:
        env["CHROME_BIN"] = chrome


class NativeChromeManager:
    """Own at most one Windows Chrome process for mb's fixed CDP endpoint."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._owns_process = False

    @staticmethod
    def _cdp_ready() -> bool:
        try:
            with urllib.request.urlopen(
                f"http://{MB_CDP_HOST}:{MB_CDP_PORT}/json/version",
                timeout=0.2,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return isinstance(payload, dict) and bool(payload.get("webSocketDebuggerUrl"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    def ensure_started(self) -> tuple[bool, str]:
        """Launch Windows Chrome when CDP 9222 is absent, otherwise reuse it."""
        if sys.platform != "win32":
            return True, "native Chrome launch is Windows-only"
        if self._cdp_ready():
            return True, "reusing Chrome CDP 9222"

        if self._process is not None and self._process.poll() is not None:
            self._process = None
            self._owns_process = False

        chrome = find_chrome_executable()
        if not chrome:
            return False, "Chrome executable not found"

        profile_dir = config.RUNTIME_DIR / "browser-profiles" / "mb-native-chrome"
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"could not create Chrome profile: {exc}"

        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
        argv = [
            chrome,
            f"--remote-debugging-port={MB_CDP_PORT}",
            "--remote-debugging-address=127.0.0.1",
            "--headless=new",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ]
        try:
            self._process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            self._owns_process = True
        except OSError as exc:
            self._process = None
            self._owns_process = False
            return False, f"could not launch Chrome: {exc}"

        deadline = time.monotonic() + _START_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if self._cdp_ready():
                return True, "launched native Chrome CDP 9222"
            if self._process.poll() is not None:
                break
            time.sleep(0.05)

        self.close()
        return False, "Chrome did not expose CDP 9222"

    def close(self) -> None:
        """Stop only the Chrome process tree launched by this cockpit."""
        process = self._process
        owned = self._owns_process
        self._process = None
        self._owns_process = False
        if not owned or process is None or process.poll() is not None:
            return

        if sys.platform == "win32":
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                if result.returncode == 0:
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
