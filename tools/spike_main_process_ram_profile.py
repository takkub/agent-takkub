"""#364 lever 5 spike — profile the main process (pythonw, 300-480 MB).

Measures three things in one fresh subprocess, before proposing any cap:

1. **tracemalloc top-N Python-heap allocators.** `tracemalloc.start()` runs as
   the very first line of this script, before any project import — so the
   snapshot has true historical attribution for everything this process ever
   allocated on the Python heap, not just "what allocated in the last few
   seconds" (which is all an on-demand snapshot against an already-warm live
   cockpit could ever show — see `ram_report.collect_main_process_profile`'s
   docstring for that tradeoff, and why the live IPC path is a *different*,
   necessarily weaker measurement than this script).
2. **gc object census** (count by type) at three points: baseline (app booted,
   no panes), with N panes up + written to, and after destroying all N panes
   + forcing `gc.collect()` — answers "does closing a pane actually give the
   Python-side wrapper objects back", independent of whatever the Chromium
   renderer side does (already measured separately by lever 1's spike,
   `tools/spike_pane_discard_ram.py`).
3. **psutil RSS of the main process itself** (not the QtWebEngineProcess
   renderer children — those are separate PIDs, out of scope here) at each of
   the three points above.

STANDALONE SCRIPT — same two hard requirements as
`tools/spike_pane_discard_ram.py` (read that module docstring for the full
QApplication(sys.argv)-not-QApplication([]) / import-order reasoning): do NOT
import this from pytest, do NOT construct these widgets inside a pytest
process. `tests/test_main_process_ram_profile_spike.py` shells this out as a
subprocess for that reason.

Run directly with the project's shared venv interpreter, e.g.:

    .venv/Scripts/python.exe tools/spike_main_process_ram_profile.py --json-out out.json
"""

from __future__ import annotations

# tracemalloc must start before any project import so its historical
# attribution actually covers this process's own allocations from the start,
# not just whatever happens after some arbitrary later point in main().
import tracemalloc

tracemalloc.start(1)

import argparse  # noqa: E402
import gc  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import weakref  # noqa: E402
from pathlib import Path  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Mirrors app.py's production Chromium flags — see spike_pane_discard_ram.py's
# module docstring for why (no real display under offscreen QPA).
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-background-timer-throttling --disable-renderer-backgrounding "
    "--disable-backgrounding-occluded-windows --renderer-process-limit=4 "
    "--disable-gpu --disable-gpu-compositing",
)

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import psutil  # noqa: E402
from PyQt6.QtWidgets import QApplication, QTabWidget  # noqa: E402

# Import BEFORE QApplication is constructed — see spike_pane_discard_ram.py's
# module docstring point 1 (QtWebEngineWidgets-before-QApplication rule).
from agent_takkub.terminal_widget import TerminalWidget  # noqa: E402

_SAMPLE_TEXT = "".join(
    f"line {i:03d}: \x1b[32mgreen\x1b[0m \x1b[1mbold\x1b[0m ก่อนหลัง สวัสดี\r\n" for i in range(60)
)

# Classes this spike specifically watches for "still alive after close" —
# the issue's own callout list (pyte screen history, transcript buffers,
# display cache, closed-pane Qt widgets), narrowed by earlier reading of
# pty_session.py / terminal_widget.py in this same task: pyte screen is a
# plain bounded pyte.Screen (not HistoryScreen, confirmed in the lever 1
# audit), _display_lines_cache is a bounded rows-length tuple, and
# _transcript is a disk file handle, not an in-memory buffer — none of those
# three grow with usage, so the only remaining plausible per-pane growth is
# the Qt/WebEngine wrapper objects themselves not being released on close.
_WATCH_TYPES = ("TerminalWidget", "QWebEngineView", "QWebEnginePage", "QWebEngineProfile")


def _pump(app: QApplication, predicate, timeout_s: float, step_s: float = 0.02) -> float:
    t0 = time.monotonic()
    while not predicate() and time.monotonic() - t0 < timeout_s:
        app.processEvents()
        time.sleep(step_s)
    app.processEvents()
    return time.monotonic() - t0


def _rss_mb() -> float:
    return psutil.Process().memory_info().rss / (1024 * 1024)


def _object_census(top_n: int = 15) -> dict:
    gc.collect()
    objs = gc.get_objects()
    counts: dict[str, int] = {}
    for obj in objs:
        name = type(obj).__name__
        counts[name] = counts.get(name, 0) + 1
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return {
        "total_objects": len(objs),
        "top_types": [{"type": t, "count": c} for t, c in top],
        "watched": {t: counts.get(t, 0) for t in _WATCH_TYPES},
    }


def _top_allocators(top_n: int = 15) -> list[dict]:
    snapshot = tracemalloc.take_snapshot()
    stats = snapshot.statistics("lineno")
    out = []
    for stat in stats[:top_n]:
        frame = stat.traceback[0]
        out.append(
            {
                "file": frame.filename,
                "line": frame.lineno,
                "size_kb": round(stat.size / 1024, 1),
                "count": stat.count,
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panes", type=int, default=5)
    parser.add_argument("--settle-ms", type=int, default=800)
    parser.add_argument("--boot-timeout-s", type=float, default=15.0)
    parser.add_argument("--json-out", type=str, default=None)
    args = parser.parse_args(argv)

    app = QApplication.instance() or QApplication(sys.argv)
    _pump(app, lambda: True, 0.2)
    gc.collect()

    baseline_rss = _rss_mb()
    baseline_census = _object_census()

    tabs = QTabWidget()
    panes = [TerminalWidget() for _ in range(args.panes)]
    for i, p in enumerate(panes):
        tabs.addTab(p, f"pane{i}")
    tabs.resize(900, 500)
    tabs.show()

    boot_elapsed = _pump(app, lambda: all(p._page_ready for p in panes), args.boot_timeout_s)
    booted = [p._page_ready for p in panes]
    if not all(booted):
        print(
            json.dumps(
                {"error": "not all panes booted", "booted": booted, "elapsed_s": boot_elapsed}
            )
        )
        sys.stdout.flush()
        # os._exit, not return: see the matching comment at the bottom of
        # main() for why this script never lets Python/Qt run their normal
        # shutdown sequence.
        os._exit(1)

    for p in panes:
        p.write_bytes(_SAMPLE_TEXT)
    time.sleep(args.settle_ms / 1000.0)
    _pump(app, lambda: True, 0.3)

    up_rss = _rss_mb()
    up_census = _object_census()

    weak_refs = [weakref.ref(p) for p in panes]
    for p in panes:
        p.destroy_terminal()
    del p  # the `for` loop variable outlives the loop and would otherwise
    # pin panes[-1] alive in this function's locals, producing a false
    # "still alive after close" positive that has nothing to do with
    # TerminalWidget itself (discovered empirically while building this
    # spike — see docs/audit/2026-08-23-364-lever5-main-process-profile.md).
    tabs.deleteLater()
    # `panes.clear()`, not `del panes`: the list itself must stop holding
    # strong references to every TerminalWidget for the weakref check below
    # to mean anything (confirmed empirically — leaving the list populated
    # made all N panes report "still alive," which was just the list itself,
    # not a real leak). `del panes` would do the same thing but ruff/pyflakes'
    # F821 mis-flags a `del` on a name a lambda/genexpr closes over anywhere
    # earlier in this same function as "undefined" — see the F821 note in
    # docs/audit/2026-08-23-364-lever5-main-process-profile.md. `tabs` isn't
    # closed over by any lambda here, so `del tabs` doesn't hit that quirk —
    # confirmed both were needed empirically: `tabs` (the QTabWidget) still
    # holds its children referenced until it's actually gone, not just
    # deleteLater()-scheduled.
    panes.clear()
    del tabs
    _pump(app, lambda: True, 0.5)
    gc.collect()
    time.sleep(args.settle_ms / 1000.0)
    _pump(app, lambda: True, 0.3)
    gc.collect()

    after_rss = _rss_mb()
    after_census = _object_census()
    still_alive = sum(1 for w in weak_refs if w() is not None)

    top_allocators = _top_allocators()
    current_traced, peak_traced = tracemalloc.get_traced_memory()

    result = {
        "panes": args.panes,
        "baseline_rss_mb": round(baseline_rss, 1),
        "up_rss_mb": round(up_rss, 1),
        "after_close_rss_mb": round(after_rss, 1),
        "rss_growth_mb_total": round(up_rss - baseline_rss, 1),
        "rss_growth_mb_per_pane": round((up_rss - baseline_rss) / args.panes, 2)
        if args.panes
        else 0.0,
        "rss_not_returned_after_close_mb": round(after_rss - baseline_rss, 1),
        "terminalwidget_python_wrappers_still_alive_after_close": still_alive,
        "baseline_object_census": baseline_census,
        "up_object_census": up_census,
        "after_close_object_census": after_census,
        "tracemalloc_top_allocators_kb": top_allocators,
        "tracemalloc_current_traced_mb": round(current_traced / (1024 * 1024), 2),
        "tracemalloc_peak_traced_mb": round(peak_traced / (1024 * 1024), 2),
    }
    out = json.dumps(result, indent=2, ensure_ascii=False)
    print(out)
    if args.json_out:
        Path(args.json_out).write_text(out, encoding="utf-8")
    sys.stdout.flush()
    # All measurement is done and written by this point. Letting main()
    # `return` normally would fall through to Python's interpreter shutdown,
    # which destroys the QApplication and its already-torn-down WebEngine
    # views' C++ side — empirically that path native-crashes (Windows access
    # violation, exit 0xC0000005) on this machine/Qt version, even though
    # nothing is wrong with the measurement itself (confirmed: the JSON above
    # is complete and correct every time, only the *exit code* is wrong).
    # os._exit skips that shutdown sequence entirely, so the process reports
    # the real outcome instead of a crash code from teardown code this
    # one-shot diagnostic script doesn't need to run correctly.
    os._exit(0)


if __name__ == "__main__":
    main()
