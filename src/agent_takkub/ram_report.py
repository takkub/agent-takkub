"""Per-pane RAM breakdown (#364 lever 6 — visibility before anything else).

Pure leaf module: psutil only (already a hard dependency everywhere else in
this codebase — resource_governor.py, performance_settings.py — so this adds
no new dependency), no Qt, no orchestrator/cli import. psutil already handles
Windows and macOS uniformly, so there is no per-OS branch here.

Two callers share `collect_ram_report`:
  * `orchestrator.Orchestrator.ram_status()` — the synchronous `takkub doctor
    --ram` / `ram-status` IPC path (cli_server.py handles that request on the
    Qt main thread, same as `performance-status`; acceptable for an explicit
    opt-in diagnostic, unlike a recurring timer tick).
  * `status_header.py`'s background `_RamSnapshotWorker` (QRunnable) — the
    performance chip's periodic sampler, which must NOT run this on the Qt
    main thread (a full process-tree walk is too slow for a timer tick), so
    it calls this function from a QThreadPool worker instead.

Both callers gather the (role, project, provider, pid) pane specs themselves
(cheap, no psutil) before calling in — this module never touches pane/session
objects directly, which is what keeps it importable from either the Qt main
thread caller or a plain worker thread without any orchestrator/Qt import.

Never guesses which pane a QtWebEngineProcess belongs to: only a process
found by walking a *specific* pane pid's own descendant tree is attributed to
that pane. Every other QtWebEngineProcess found under the cockpit root is
reported as shared overhead instead (see `check_pane_mcp_handshake`-style
"don't invent facts you can't observe" precedent in doctor.py).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence

import psutil

from . import __version__ as _TAKKUB_VERSION

_QTWEBENGINE_MARKER = "qtwebengineprocess"


def _rss_bytes(proc: object) -> int:
    try:
        return int(proc.memory_info().rss)  # type: ignore[attr-defined]
    except Exception:
        return 0


def _proc_name(proc: object) -> str:
    try:
        return str(proc.name())  # type: ignore[attr-defined]
    except Exception:
        return ""


def collect_ram_report(
    pane_specs: Sequence[Mapping[str, object]],
    *,
    main_pid: int,
    governor_min_ram_percent: float | None = None,
    takkub_version: str = _TAKKUB_VERSION,
    psutil_module: object = psutil,
) -> dict:
    """Best-effort per-pane RAM breakdown, as a plain JSON-safe dict.

    `pane_specs`: one mapping per live pane — ``{"role", "project",
    "provider", "pid"}``. A pane whose `pid` is None or no longer alive gets
    a zeroed row with a `note` explaining why, rather than being dropped
    silently (a caller comparing pane counts against `takkub list` should
    never see fewer RAM rows than panes without an explanation).

    `psutil_module` is injectable so tests can exercise the tree-walk and
    shared-QtWebEngine logic against a fake process tree without spawning
    real processes.
    """
    now = time.time()

    try:
        main_proc = psutil_module.Process(main_pid)
    except Exception:
        main_proc = None

    descendants: list = []
    if main_proc is not None:
        try:
            descendants = main_proc.children(recursive=True)
        except Exception:
            descendants = []

    by_pid: dict[int, object] = {}
    children_by_ppid: dict[int, list[int]] = {}
    for proc in descendants:
        try:
            pid = int(proc.pid)
        except Exception:
            continue
        by_pid[pid] = proc
        try:
            ppid = int(proc.ppid())
        except Exception:
            continue
        children_by_ppid.setdefault(ppid, []).append(pid)

    def _subtree_pids(root_pid: int) -> list[int]:
        out: list[int] = []
        stack = list(children_by_ppid.get(root_pid, ()))
        seen = set(stack)
        while stack:
            pid = stack.pop()
            out.append(pid)
            for child in children_by_ppid.get(pid, ()):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return out

    claimed: set[int] = set()
    panes: list[dict] = []
    for spec in pane_specs:
        row: dict = {
            "role": str(spec.get("role", "")),
            "project": str(spec.get("project", "")),
            "provider": spec.get("provider"),
            "pid": spec.get("pid"),
            "cli_rss_bytes": 0,
            "node_children_rss_bytes": 0,
            "node_children_count": 0,
            "qtwebengine_rss_bytes": 0,
            "total_bytes": 0,
            "note": "",
        }
        raw_pid = spec.get("pid")
        if raw_pid is None:
            row["note"] = "no pid recorded"
            panes.append(row)
            continue
        pid = int(raw_pid)
        cli_proc = by_pid.get(pid)
        if cli_proc is None:
            # Not (or no longer) a descendant of the cockpit root in this
            # snapshot — fall back to a direct lookup before giving up.
            try:
                cli_proc = psutil_module.Process(pid)
            except Exception:
                cli_proc = None
        if cli_proc is None:
            row["note"] = "process not found (exited?)"
            panes.append(row)
            continue
        claimed.add(pid)
        row["cli_rss_bytes"] = _rss_bytes(cli_proc)
        node_bytes = 0
        node_count = 0
        qtwe_bytes = 0
        for child_pid in _subtree_pids(pid):
            claimed.add(child_pid)
            child_proc = by_pid.get(child_pid)
            if child_proc is None:
                continue
            rss = _rss_bytes(child_proc)
            if _QTWEBENGINE_MARKER in _proc_name(child_proc).lower():
                qtwe_bytes += rss
            else:
                node_bytes += rss
                node_count += 1
        row["node_children_rss_bytes"] = node_bytes
        row["node_children_count"] = node_count
        row["qtwebengine_rss_bytes"] = qtwe_bytes
        row["total_bytes"] = row["cli_rss_bytes"] + node_bytes + qtwe_bytes
        panes.append(row)

    shared_bytes = 0
    shared_count = 0
    for pid, proc in by_pid.items():
        if pid in claimed:
            continue
        if _QTWEBENGINE_MARKER not in _proc_name(proc).lower():
            continue
        shared_bytes += _rss_bytes(proc)
        shared_count += 1

    main_rss = _rss_bytes(main_proc) if main_proc is not None else 0

    try:
        vm = psutil_module.virtual_memory()
        machine_total = int(vm.total)
        machine_available = int(vm.available)
    except Exception:
        machine_total = 0
        machine_available = 0
    machine_available_percent = (
        round(100.0 * machine_available / machine_total, 1) if machine_total else 0.0
    )

    panes_total = sum(int(row["total_bytes"]) for row in panes)

    return {
        "generated_at": now,
        "takkub_version": takkub_version,
        "main_process": {"pid": main_pid, "rss_bytes": main_rss},
        "shared_qtwebengine": {"rss_bytes": shared_bytes, "process_count": shared_count},
        "panes": panes,
        "total_panes_bytes": panes_total,
        "total_cockpit_bytes": panes_total + main_rss + shared_bytes,
        "machine_total_bytes": machine_total,
        "machine_available_bytes": machine_available,
        "machine_available_percent": machine_available_percent,
        "governor_min_available_ram_percent": governor_min_ram_percent,
    }
