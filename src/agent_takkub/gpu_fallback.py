"""Software-rendering fallback after a GPU-driver crash (#977).

The cockpit died with a native access violation in ``d3d11.dll`` (main thread
inside ``app.exec()``) about 12 minutes after an NVIDIA driver update. The
renderer runs on the GPU by default (see ``app.py``'s Chromium flag block),
and the only way off it was the env var ``TAKKUB_FORCE_SOFTWARE_GPU=1``,
which a user only finds by reading the code.

This module decides, before QApplication exists, whether this boot renders
in software:

* ``TAKKUB_FORCE_SOFTWARE_GPU=1`` forces software and ``=0`` forces hardware.
  Either one overrides everything below.
* Otherwise, an unexpired marker (``runtime/gpu-software-fallback.json``)
  selects software.
* Otherwise, the previous run's ``boot.log`` segment is checked. If it ended
  in a faulthandler native-crash dump, the Windows Application log (Event
  1000, matched by that run's pid) names the faulting module. A GPU or
  graphics module there writes the marker for ``FALLBACK_DAYS`` and this boot
  renders in software. A crash in any other module (e.g. ``python311.dll``)
  is reported but does not switch.

macOS gap: there is no event-log lookup, so a crash there is reported
(``native_crash_detected``) but never auto-switches. The env override works on
both OSes.

Stdlib only. ``app.py`` calls this at import time.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ENV_FORCE = "TAKKUB_FORCE_SOFTWARE_GPU"
FALLBACK_DAYS = 7
_MARKER_NAME = "gpu-software-fallback.json"

#: Faulting modules that mean "the graphics stack crashed", lower-case.
GPU_MODULES = frozenset(
    {
        "d3d11.dll",
        "d3d12.dll",
        "d3d9.dll",
        "dxgi.dll",
        "dcomp.dll",
        "d3d12core.dll",
        "libglesv2.dll",
        "libegl.dll",
        "opengl32.dll",
        "nvwgf2umx.dll",
        "nvwgf2um.dll",
        "nvldumdx.dll",
        "nvoglv64.dll",
        "nvd3dumx.dll",
        "igd10iumd64.dll",
        "igd12umd64.dll",
        "igdumdim64.dll",
        "igc64.dll",
        "amdxx64.dll",
        "atidxx64.dll",
        "amdxc64.dll",
        "atio6axx.dll",
    }
)

_BOOT_MARK_RE = re.compile(r"^--- boot (\d+) ---$", re.M)
_NATIVE_CRASH_RE = re.compile(
    r"^(Windows fatal exception: access violation"
    r"|Fatal Python error: (Segmentation fault|Bus error|Aborted))",
    re.M,
)


@dataclass
class GpuDecision:
    software: bool
    reason: str
    #: (event, details) pairs for events.log; ``app.main`` logs them once the
    #: orchestrator logger is importable.
    events: list[tuple[str, dict]] = field(default_factory=list)


def _env_override(env: dict[str, str]) -> bool | None:
    raw = env.get(ENV_FORCE, "").strip().lower()
    if not raw:
        return None
    return raw not in ("0", "false", "no", "off")


def previous_run_crash(boot_log_text: str) -> tuple[bool, int | None]:
    """``(crashed, pid)`` for the run BEFORE the current one. The current
    process has already appended its own ``--- boot <pid> ---`` line, so the
    previous run is the segment between the last two markers. When boot.log
    rotation cut the previous run's own marker away (real #977 log: the 32 KB
    tail kept began mid-run, after the watchdog dumps), the segment is
    everything before the current marker and the pid is unknown (None)."""
    marks = list(_BOOT_MARK_RE.finditer(boot_log_text))
    if not marks:
        return False, None
    cur = marks[-1]
    if len(marks) >= 2:
        prev = marks[-2]
        segment, pid = boot_log_text[prev.end() : cur.start()], int(prev.group(1))
    else:
        segment, pid = boot_log_text[: cur.start()], None
    if not _NATIVE_CRASH_RE.search(segment):
        return False, None
    return True, pid


_EVENT_RE = re.compile(r"<Event\b.*?</Event>", re.S)
_DATA_RE = re.compile(r"<Data Name='(\w+)'>([^<]*)</Data>")


def parse_app_error_events(xml_text: str) -> list[dict[str, str]]:
    """Event 1000 XML (``wevtutil /f:xml``) as ``[{AppName, ModuleName,
    ProcessId, ...}]``. XML field names are locale-independent; the rendered
    text message is not."""
    return [dict(_DATA_RE.findall(ev)) for ev in _EVENT_RE.findall(xml_text or "")]


def faulting_module_for_pid(pid: int | None, *, _run=subprocess.run) -> str | None:
    """Windows: the module named by the Application Error event for *pid*
    in the last 7 days, or None (not found, not Windows, wevtutil failed).
    ``pid=None`` (marker rotated away) takes the newest python crash of the
    last hour instead — a faulthandler "fatal exception" line alone is not
    proof (Windows prints it for access violations the process then handles),
    so a matching Event 1000 is still required."""
    if sys.platform != "win32":
        return None
    window_ms = 604800000 if pid is not None else 3600000
    query = f"*[System[(EventID=1000) and TimeCreated[timediff(@SystemTime) <= {window_ms}]]]"
    try:
        res = _run(
            ["wevtutil", "qe", "Application", f"/q:{query}", "/c:20", "/rd:true", "/f:xml"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for ev in parse_app_error_events(res.stdout):
        raw_pid = (ev.get("ProcessId") or "").strip()
        try:
            ev_pid = int(raw_pid, 16) if raw_pid.lower().startswith("0x") else int(raw_pid)
        except ValueError:
            continue
        if not (ev.get("AppName") or "").lower().startswith("python"):
            continue
        if pid is None and ev_pid == os.getpid():
            continue
        if pid is None or ev_pid == pid:
            return (ev.get("ModuleName") or "").strip() or None
    return None


def _read_marker(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def decide(
    runtime_dir: Path,
    boot_log_path: Path,
    env: dict[str, str] | None = None,
    now: float | None = None,
    *,
    _module_lookup=faulting_module_for_pid,
) -> GpuDecision:
    env = dict(os.environ) if env is None else env
    now = time.time() if now is None else now
    forced = _env_override(env)
    marker_path = runtime_dir / _MARKER_NAME
    marker = _read_marker(marker_path)
    events: list[tuple[str, dict]] = []

    try:
        text = boot_log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    crashed, crashed_pid = previous_run_crash(text)
    # a marker already written for this exact crash (pid known) is not news
    if crashed and (crashed_pid is None or crashed_pid != marker.get("crashed_pid")):
        module = _module_lookup(crashed_pid)
        gpu = bool(module) and module.lower() in GPU_MODULES
        events.append(
            (
                "native_crash_detected",
                {"crashed_pid": crashed_pid, "module": module, "gpu_module": gpu},
            )
        )
        if gpu:
            marker = {
                "crashed_pid": crashed_pid,
                "module": module,
                "since": now,
                "until": now + FALLBACK_DAYS * 86400,
            }
            try:
                runtime_dir.mkdir(parents=True, exist_ok=True)
                marker_path.write_text(json.dumps(marker), encoding="utf-8")
            except OSError:
                pass

    if forced is not None:
        reason = f"{ENV_FORCE}={'1' if forced else '0'}"
        software = forced
    elif marker and float(marker.get("until") or 0) > now:
        software = True
        reason = f"auto: previous crash in {marker.get('module')} (until marker expires)"
    else:
        software = False
        reason = "default hardware"
    if software and not forced:
        events.append(
            (
                "gpu_software_fallback",
                {
                    "module": marker.get("module"),
                    "until": int(float(marker.get("until") or 0)),
                    "marker": str(marker_path),
                    "override": f"{ENV_FORCE}=0",
                },
            )
        )
    return GpuDecision(software=software, reason=reason, events=events)


def apply_qt_software_rendering() -> None:
    """Take Qt's own scene graph off Direct3D 11 (Windows). Call before
    QApplication. Chromium's ``--disable-gpu`` only empties the GPU *process*,
    and Qt still composited every QWebEngineView through D3D11 in the main
    process, which is where #977 crashed. Measured: ``QT_QUICK_BACKEND=software``
    and ``setGraphicsApi(Software)`` are both ignored with WebEngine
    (``graphicsApi()`` stays ``Direct3D11Rhi``). Software OpenGL (the bundled
    ``opengl32sw.dll``) gives ``OpenGL`` and renders correctly."""
    if sys.platform != "win32":
        return
    from PyQt6.QtCore import Qt
    from PyQt6.QtQuick import QQuickWindow, QSGRendererInterface
    from PyQt6.QtWidgets import QApplication

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseSoftwareOpenGL)
    QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)
