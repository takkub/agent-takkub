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

import gc
import time
import tracemalloc
from collections.abc import Mapping, Sequence

import psutil

from . import __version__ as _TAKKUB_VERSION

_QTWEBENGINE_MARKER = "qtwebengineprocess"

# #364 lever 5: the object types this process's own gc census specifically
# calls out, because they're the ones the issue itself named as suspects
# (pyte screen history / transcript buffers / display cache / closed-pane Qt
# widgets) — see `orchestrator.Orchestrator.ram_profile`'s docstring and
# docs/audit/2026-08-23-364-lever5-main-process-profile.md for what was
# actually found for each. `AgentPane`/`TerminalWidget`/`PtySession` are the
# per-teammate-pane objects; `HeadlessPane` is #365's subagent-mode
# equivalent (no Qt widget, still worth counting).
_WATCHED_TYPE_NAMES = ("AgentPane", "TerminalWidget", "PtySession", "HeadlessPane")


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


def _snapshot_ppid_map(psutil_module: object) -> dict[int, int] | None:
    """One whole-machine ``{pid: ppid}`` snapshot, or None when the injected
    psutil has no such primitive (the test fake) — callers then fall back to
    per-process ``ppid()``.

    Why this exists (2026-08-26 Lead-pane input-lag investigation): psutil's
    ``Process.ppid()`` on Windows is implemented as ``ppid_map()[pid]`` —
    i.e. EVERY call re-enumerates the ENTIRE process table
    (``NtQuerySystemInformation``, ~10 ms for the 380 processes this box
    runs) inside a C extension call that never releases the GIL. Called once
    per cockpit descendant (28 measured) that is ~0.3 s of *uninterruptible*
    GIL hold per 15 s RAM-chip tick standalone, and 0.7–2 s inside the live
    cockpit — ``py-spy --gil`` put this one line at 51% of all GIL-holding
    samples, and ``py-spy dump`` during a logged ``main_thread_stall`` showed
    it as the only GIL holder while the Qt main thread sat blocked in
    ``EnumWindows``/``Path.stat`` waiting to get the GIL back. Running the
    walk on a QThreadPool worker (the existing design) does not help: a
    worker thread that holds the GIL in C code stalls the main thread just
    as hard as if the walk ran there. One ``ppid_map()`` call is a single
    ~10 ms hold instead of 28 of them.

    ``ppid_map`` is a private-but-stable psutil primitive present on every
    platform module (Windows/Linux/macOS/BSD — ``Process.children()`` itself
    is built on it), so this is no more platform-specific than the
    ``children(recursive=True)`` call above."""
    platform_mod = getattr(psutil_module, "_psplatform", None)
    fn = getattr(platform_mod, "ppid_map", None)
    if not callable(fn):
        return None
    try:
        raw = fn()
    except Exception:
        return None
    out: dict[int, int] = {}
    try:
        for pid, ppid in raw.items():
            out[int(pid)] = int(ppid)
    except Exception:
        return None
    return out


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
    ppid_map = _snapshot_ppid_map(psutil_module)
    for proc in descendants:
        try:
            pid = int(proc.pid)
        except Exception:
            continue
        by_pid[pid] = proc
        ppid = ppid_map.get(pid) if ppid_map is not None else None
        if ppid is None:
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
            # #364 lever 1: whether this pane's Chromium renderer is
            # currently discarded (caller-supplied — this module never
            # touches TerminalWidget/Qt itself, see the module docstring).
            "discarded": bool(spec.get("discarded", False)),
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


def collect_main_process_profile(*, top_n: int = 15) -> dict:
    """On-demand, temporary main-process profile (#364 lever 5).

    Unlike `collect_ram_report` above, this can only ever describe the
    process it runs IN (tracemalloc/gc have no remote mode), so there is no
    `main_pid` parameter — the caller is always the live cockpit's own IPC
    handler, on the Qt main thread, same trust/cost tier as `ram_status`'s
    `ram-status` (an explicit opt-in diagnostic, never a recurring timer
    tick).

    Two different techniques, because they answer two different questions
    and neither alone is enough:

    * **tracemalloc top allocators** answers "what's actively allocating
      Python-heap memory in this window." It is necessarily a WEAK signal
      here: tracemalloc only sees allocations that happen after `.start()`,
      so calling it on an already-warm process (as this always is — the
      cockpit has been running for a while by the time someone asks for a
      profile) massively undercounts already-resident memory. It also only
      tracks the Python object heap (`pymalloc`) — it cannot see PyQt6/Qt/
      Chromium's own C++ allocations, which is most of a cockpit's RSS. Spike
      measurement confirmed both of these directly: a controlled subprocess
      that started tracemalloc before any import still only ever traced a
      few MB even after booting 5 real WebEngine-backed panes whose RSS grew
      by 65 MB (see docs/audit/2026-08-23-364-lever5-main-process-profile.md)
      — the traced total is a lower bound, not a full accounting. Still
      useful for spotting *runaway* Python-heap growth (something producing
      thousands of new objects at one line), just not for "what's the
      300-480 MB made of."
    * **gc object census** (`gc.get_objects()` grouped by `type(obj).__name__`)
      answers "what Python objects are alive right now," independent of when
      they were allocated — this is the retroactive-safe half, and the one
      that can actually prove or disprove a leak (`watched_pane_object_count`
      vs. the caller's own live pane count — a mismatch means something is
      keeping a closed pane's objects referenced).

    Never leaves tracemalloc running: if this call is the one that started
    it, it stops it again before returning. If tracemalloc was already
    running for some other reason (e.g. `PYTHONTRACEMALLOC=1` in the
    environment), this respects that and leaves it running afterward — it
    only ever turns off what it turned on.
    """
    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:
        tracemalloc.start(1)
    gc.collect()
    snapshot = tracemalloc.take_snapshot()
    stats = snapshot.statistics("lineno")
    top_allocators = [
        {
            "file": stat.traceback[0].filename,
            "line": stat.traceback[0].lineno,
            "size_bytes": int(stat.size),
            "count": int(stat.count),
        }
        for stat in stats[:top_n]
    ]
    traced_current_bytes, traced_peak_bytes = tracemalloc.get_traced_memory()
    if not already_tracing:
        tracemalloc.stop()

    objs = gc.get_objects()
    type_counts: dict[str, int] = {}
    for obj in objs:
        name = type(obj).__name__
        type_counts[name] = type_counts.get(name, 0) + 1
    top_types = sorted(type_counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

    return {
        "generated_at": time.time(),
        "takkub_version": _TAKKUB_VERSION,
        "tracemalloc_was_already_running": already_tracing,
        "tracemalloc_traced_current_bytes": int(traced_current_bytes),
        "tracemalloc_traced_peak_bytes": int(traced_peak_bytes),
        "top_allocators": top_allocators,
        "gc_object_count": len(objs),
        "top_object_types": [{"type": t, "count": c} for t, c in top_types],
        "watched_pane_object_counts": {
            name: type_counts.get(name, 0) for name in _WATCHED_TYPE_NAMES
        },
    }
