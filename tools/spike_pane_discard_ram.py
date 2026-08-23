"""#364 lever 1 spike — measure real RAM returned by discarding hidden pane renderers.

Question this answers: does `QWebEnginePage.setLifecycleState(Discarded)` on a
HIDDEN pane's QWebEngineView actually return renderer RAM on this machine, how
much per pane, and how expensive is re-attaching when the user switches back?

STANDALONE SCRIPT — do NOT import this from pytest and do NOT construct these
widgets inside a pytest process. Two hard requirements fall out of that:

1. QtWebEngineWidgets must be imported before any QCoreApplication/QApplication
   instance exists (a plain Qt-only rule, not new here).
2. QApplication MUST be constructed with a real argv (``QApplication(sys.argv)``),
   not ``QApplication([])``. An empty argv leaves Chromium's ``base::CommandLine``
   with no argv[0] to parse, and the instant a real QWebEngineView tries to spin
   up its renderer process, Chromium hard-aborts the whole process natively
   (Windows exit code -1073740791 / 0xC0000409) — no Python exception, no
   traceback, nothing catchable. This is almost certainly why
   tests/test_terminal_widget.py and tests/test_editor_widget.py both document
   "constructing a real QWebEngineView hard-aborts pytest even under
   QT_QPA_PLATFORM=offscreen" — both files' ``qapp`` fixtures use
   ``QApplication([])``. Confirmed by direct repro while building this spike:
   swapping to ``QApplication(sys.argv)`` in an otherwise-identical script made
   the abort disappear (page booted in ~0.5-0.75s). Real fix for those two test
   files is a separate, more careful piece of work (pytest's own argv/plugins
   are not guaranteed as clean as this script's) — flagged here as a discovered
   side-finding, not fixed in this spike.

Run directly with the project's shared venv interpreter, e.g.:

    .venv/Scripts/python.exe tools/spike_pane_discard_ram.py --json-out out.json

tests/test_pane_discard_spike.py shells out to this script as a subprocess (own
process, own crash domain) so the measurement stays repeatable — including in
CI — without risking a hard native abort of the pytest process itself.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Mirrors app.py's production Chromium flags (renderer-process-limit=4 is the
# one that matters most for this spike: it makes Chromium REUSE renderer
# processes once more than 4 views exist, so "how much RAM does discarding one
# pane return" depends on whether that pane currently has a renderer process
# to itself or is sharing one with a still-visible pane). GPU is disabled
# outright here (not just software-rasterized) because there is no real
# display under the offscreen QPA platform.
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-background-timer-throttling --disable-renderer-backgrounding "
    "--disable-backgrounding-occluded-windows --renderer-process-limit=4 "
    "--disable-gpu --disable-gpu-compositing",
)

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import psutil  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage  # noqa: E402
from PyQt6.QtWidgets import QApplication, QTabWidget  # noqa: E402

# Import BEFORE QApplication is constructed — see module docstring point 1.
from agent_takkub.terminal_widget import TerminalWidget  # noqa: E402

_SAMPLE_TEXT = "".join(
    f"line {i:03d}: \x1b[32mgreen\x1b[0m \x1b[1mbold\x1b[0m ก่อนหลัง สวัสดี\r\n" for i in range(60)
)


def _pump(app: QApplication, predicate, timeout_s: float, step_s: float = 0.02) -> float:
    """Spin the Qt event loop until predicate() is true or timeout. Returns elapsed seconds."""
    t0 = time.monotonic()
    while not predicate() and time.monotonic() - t0 < timeout_s:
        app.processEvents()
        time.sleep(step_s)
    app.processEvents()
    return time.monotonic() - t0


def _get_buffer_text(app: QApplication, pane: TerminalWidget, timeout_s: float = 3.0) -> str:
    """Poll termGetBufferText() until it returns non-blank content or timeout.

    xterm.js applies term.write() asynchronously (its own internal write
    queue, drained on later frames) — a single immediate read-back races it.
    Retrying for up to `timeout_s` is what actually observing production
    behaviour requires; a one-shot read under-reports fidelity.
    """
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        got: dict[str, str] = {}

        def _cb(text: str, got: dict[str, str] = got) -> None:
            got["t"] = text

        pane.request_buffer_text(_cb)
        _pump(app, lambda got=got: "t" in got, 2.0)
        last = got.get("t") or ""
        if last.strip():
            return last
        time.sleep(0.1)
    return last


def _webengine_rss(pids_seen: set[int] | None = None) -> tuple[float, int, list[dict]]:
    """Total RSS (MB) + count + per-process detail of this process's
    QtWebEngineProcess children (the actual renderer processes)."""
    gc.collect()
    proc = psutil.Process()
    total = 0
    detail: list[dict] = []
    for child in proc.children(recursive=True):
        try:
            name = child.name()
            if "QtWebEngineProcess" not in name:
                continue
            rss = child.memory_info().rss
        except psutil.Error:
            continue
        total += rss
        detail.append({"pid": child.pid, "rss_mb": round(rss / (1024 * 1024), 1)})
        if pids_seen is not None:
            pids_seen.add(child.pid)
    return total / (1024 * 1024), len(detail), detail


@dataclass
class ReattachResult:
    pane_index: int
    lifecycle_requested: str
    lifecycle_effective: bool
    reload_ms: float
    repaint_ms: float
    total_ms: float
    scrollback_lost: bool
    new_content_matched: bool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panes", type=int, default=4, help="total panes (Lead + N teammates)")
    parser.add_argument(
        "--hidden",
        type=int,
        default=None,
        help="panes to discard (default: panes-1, all but tab 0)",
    )
    parser.add_argument("--lifecycle", choices=["discard", "frozen"], default="discard")
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=1500,
        help="wait after load / after discard before sampling RSS",
    )
    parser.add_argument("--boot-timeout-s", type=float, default=15.0)
    parser.add_argument("--reattach-timeout-s", type=float, default=6.0)
    parser.add_argument("--json-out", type=str, default=None)
    args = parser.parse_args(argv)

    hidden_n = args.hidden if args.hidden is not None else max(1, args.panes - 1)
    if hidden_n >= args.panes:
        parser.error("--hidden must be < --panes (tab 0 always stays visible)")

    app = QApplication.instance() or QApplication(sys.argv)

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
        return 1

    for p in panes:
        p.write_bytes(_SAMPLE_TEXT)
    for p in panes:
        _get_buffer_text(app, p, timeout_s=3.0)  # drain the async write queue before baselining

    hidden_panes = panes[1 : 1 + hidden_n]
    tabs.setCurrentWidget(panes[0])  # tab 0 is "the pane the user is looking at"
    for p in panes:
        visible = p not in hidden_panes
        p.set_keepalive(visible)  # exactly what ProjectTab._apply_pane_keepalive does today

    time.sleep(args.settle_ms / 1000.0)
    _pump(app, lambda: True, 0.3)
    before_mb, before_n, before_detail = _webengine_rss()

    lifecycle_state = (
        QWebEnginePage.LifecycleState.Discarded
        if args.lifecycle == "discard"
        else QWebEnginePage.LifecycleState.Frozen
    )
    for p in hidden_panes:
        assert not p.isVisible(), (
            "hidden pane must be a non-current QTabWidget tab for discard to take"
        )
        p._view.page().setLifecycleState(lifecycle_state)

    time.sleep(args.settle_ms / 1000.0)
    _pump(app, lambda: True, 0.3)
    after_mb, after_n, after_detail = _webengine_rss()

    lifecycle_effective = [p._view.page().lifecycleState() == lifecycle_state for p in hidden_panes]

    reattach_results: list[ReattachResult] = []
    for idx, p in enumerate(hidden_panes):
        page = p._view.page()
        if args.lifecycle == "frozen":
            # Frozen never unloads the page (that's exactly why it frees ~0
            # RAM — see module docstring) — there is no reload to time and no
            # fresh pageReady signal to wait for, so "reattach" is instant by
            # construction. Skip the reload/repaint measurement entirely
            # rather than report a misleading timeout.
            reattach_results.append(
                ReattachResult(
                    idx, args.lifecycle, lifecycle_effective[idx], 0.0, 0.0, 0.0, False, True
                )
            )
            continue
        # Every pane got _SAMPLE_TEXT written + drained before the baseline RSS
        # sample above, so "had real content pre-discard" is already known —
        # querying a *discarded* page's buffer isn't meaningful (no JS context
        # to answer runJavaScript at all while LifecycleState.Discarded).
        p._page_ready = False
        tabs.setCurrentWidget(p)  # user switches back to this tab
        page.setLifecycleState(QWebEnginePage.LifecycleState.Active)
        reload_elapsed = _pump(app, lambda p=p: p._page_ready, args.reattach_timeout_s)
        reload_ms = reload_elapsed * 1000.0
        if not p._page_ready:
            reattach_results.append(
                ReattachResult(
                    idx, args.lifecycle, lifecycle_effective[idx], reload_ms, -1, -1, True, False
                )
            )
            continue

        post_reload_text = _get_buffer_text(app, p, timeout_s=0.5)
        scrollback_lost = lifecycle_effective[idx] and not post_reload_text.strip()

        t1 = time.monotonic()
        marker = f"POSTREATTACH-{idx}"
        p.write_bytes(f"{marker} \x1b[1mbold\x1b[0m\r\n")
        text_after = _get_buffer_text(app, p, timeout_s=3.0)
        repaint_ms = (time.monotonic() - t1) * 1000.0

        reattach_results.append(
            ReattachResult(
                pane_index=idx,
                lifecycle_requested=args.lifecycle,
                lifecycle_effective=lifecycle_effective[idx],
                reload_ms=round(reload_ms, 1),
                repaint_ms=round(repaint_ms, 1),
                total_ms=round(reload_ms + repaint_ms, 1),
                scrollback_lost=scrollback_lost,
                new_content_matched=marker in text_after,
            )
        )
        # Leave the tab where it started so the next iteration's baseline is comparable.
        tabs.setCurrentWidget(panes[0])
        p.set_keepalive(False)

    result = {
        "panes_total": args.panes,
        "panes_hidden": hidden_n,
        "lifecycle_requested": args.lifecycle,
        "lifecycle_effective_all": all(lifecycle_effective),
        "lifecycle_effective_per_pane": lifecycle_effective,
        "webengine_rss_mb_before": round(before_mb, 1),
        "webengine_process_count_before": before_n,
        "webengine_rss_detail_before": before_detail,
        "webengine_rss_mb_after": round(after_mb, 1),
        "webengine_process_count_after": after_n,
        "webengine_rss_detail_after": after_detail,
        "rss_freed_mb_total": round(before_mb - after_mb, 1),
        "rss_freed_mb_per_hidden_pane": (
            round((before_mb - after_mb) / hidden_n, 1) if hidden_n else 0.0
        ),
        "reattach": [asdict(r) for r in reattach_results],
    }
    out = json.dumps(result, indent=2, ensure_ascii=False)
    print(out)
    if args.json_out:
        Path(args.json_out).write_text(out, encoding="utf-8")

    for p in panes:
        p.destroy_terminal()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
