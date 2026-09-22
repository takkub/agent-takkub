"""Provision the `cloudflared` binary for Quick-tunnel mode (#710).

Quick tunnel (`TunnelConfig.type == "quick"`) is the no-domain path — the
cockpit runs `cloudflared tunnel --url http://localhost:<port>` and gets a
random `*.trycloudflare.com` address — but it still needs the binary, and on
a fresh machine there is none on PATH. Before this module, picking Quick
tunnel on such a machine failed at Enable time with "The tunnel couldn't
start" and left the user to find and install cloudflared by hand, which is
exactly the hunting the mode exists to avoid.

Resolution order (`resolve_cloudflared`):
  1. an explicit `cloudflared_bin` from the config,
  2. `cloudflared` on PATH,
  3. a copy this module downloaded earlier into `DATA_HOME/bin/`,
  4. (only when the caller asks) download Cloudflare's official release
     asset for this OS/arch into `DATA_HOME/bin/` and use that.

Downloads come from the canonical GitHub release URL only — never a mirror
or a package index — and are verified by running `cloudflared --version`
before the path is handed back. A partial download is written to a `.part`
file and renamed only on success, so a killed download never leaves a
broken binary behind. Pure stdlib + `subprocess`; no Qt — the dialog runs
this on a worker thread and polls.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .._win_console import SUBPROCESS_NO_WINDOW
from ..config import DATA_HOME

_RELEASE_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
_VERIFY_TIMEOUT_S = 15
_DOWNLOAD_TIMEOUT_S = 60


class CloudflaredInstallError(RuntimeError):
    pass


def install_dir() -> Path:
    return DATA_HOME / "bin"


def _binary_name() -> str:
    return "cloudflared.exe" if sys.platform == "win32" else "cloudflared"


def installed_path() -> Path:
    return install_dir() / _binary_name()


def release_asset_for(system: str | None = None, machine: str | None = None) -> str:
    """The official release asset name for this OS/arch — pure, so the
    mapping is unit-testable without a network."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arm = machine in ("arm64", "aarch64")
    if system == "windows":
        return "cloudflared-windows-arm64.exe" if arm else "cloudflared-windows-amd64.exe"
    if system == "darwin":
        return "cloudflared-darwin-arm64.tgz" if arm else "cloudflared-darwin-amd64.tgz"
    if system == "linux":
        return "cloudflared-linux-arm64" if arm else "cloudflared-linux-amd64"
    raise CloudflaredInstallError(f"no cloudflared release for {system}/{machine}")


def _verify(binary: Path) -> bool:
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_VERIFY_TIMEOUT_S,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "cloudflared" in (proc.stdout + proc.stderr).lower()


def resolve_cloudflared(
    explicit: str = "",
    *,
    download: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> str | None:
    """The cloudflared executable to run, or None when none is available
    and *download* is False. With *download* True a missing binary is
    fetched (see module docstring); `CloudflaredInstallError` on failure.

    An explicit path wins as given (same precedence `Tunnel._cloudflared_bin`
    always had) — the user Browsed to it, so a wrong path should fail
    loudly at launch rather than be silently replaced by another copy."""
    if explicit:
        return explicit
    on_path = shutil.which("cloudflared")
    if on_path:
        return on_path
    local = installed_path()
    if local.is_file():
        return str(local)
    if not download:
        return None
    return str(download_cloudflared(progress=progress))


def download_cloudflared(*, progress: Callable[[int, int], None] | None = None) -> Path:
    """Fetch the official release asset into `install_dir()` and return the
    verified binary path. *progress(done_bytes, total_bytes)* is called as
    chunks land (total may be 0 when the server sends no length)."""
    asset = release_asset_for()
    url = _RELEASE_BASE + asset
    dest_dir = install_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = installed_path()
    part = dest_dir / (asset + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "agent-takkub"})
        with (
            urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_S) as resp,
            open(part, "wb") as fh,
        ):
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise CloudflaredInstallError(f"download failed: {exc}") from exc

    try:
        if asset.endswith(".tgz"):
            with tarfile.open(part, "r:gz") as tar:
                member = next((m for m in tar.getmembers() if m.name.endswith("cloudflared")), None)
                if member is None:
                    raise CloudflaredInstallError("archive has no cloudflared binary")
                with tar.extractfile(member) as src, open(target, "wb") as dst:  # type: ignore[union-attr]
                    shutil.copyfileobj(src, dst)
            part.unlink(missing_ok=True)
        else:
            os.replace(part, target)
        if sys.platform != "win32":
            target.chmod(0o700)
    except (OSError, tarfile.TarError) as exc:
        part.unlink(missing_ok=True)
        raise CloudflaredInstallError(f"install failed: {exc}") from exc

    if not _verify(target):
        target.unlink(missing_ok=True)
        raise CloudflaredInstallError("downloaded cloudflared did not run (`--version` failed)")
    return target
