"""#365 phase 10 — WebEngine lifecycle soak for the Workspace/IDE-lite
surface (13_PERFORMANCE_AND_QT_RULES.md rule 4: "no painted QWebEngineView
reparent across projects").

Cycles the app-wide Monaco `EditorHost` open/close across several projects,
cycles the (headless) `PreviewController` state machine the same way, and
discards/reattaches a real `TerminalWidget` pane — all N times — then
compares RSS + live-object counts before/after. A crash anywhere in this
process (the real risk this soak exists to catch — a reparented/reused
QWebEngineView across projects hard-aborts the whole process, same as
`tools/spike_pane_discard_ram.py`) shows up as a non-zero exit code, not a
Python exception.

STANDALONE SCRIPT — same two hard requirements as
tools/spike_pane_discard_ram.py (read that module's docstring for the full
explanation): import QtWebEngineWidgets/TerminalWidget before any
QCoreApplication exists, and construct QApplication with a real argv
(`QApplication(sys.argv)`, never `QApplication([])`) or a real
QWebEngineView hard-aborts the process. tests/test_workspace_webengine_soak.py
shells this out as a subprocess for the same crash-isolation reason
test_pane_discard_spike.py does.

Preview scope note: `PreviewController` is a headless QObject — there is no
real `preview_widget.py` yet (frontend, not built as of #365 phase 5; see
preview_controller.py's own module docstring). So the "open/close preview"
cycle here exercises its state machine only, never a second QWebEngineView.
Editor DOES use a real Monaco QWebEngineView via `EditorHost`/`_EditorWebView`
— that is the one surface under actual WebEngine lifecycle test.

Run directly with the project's shared venv interpreter:

    .venv/Scripts/python.exe tools/soak_workspace_webengine.py --cycles 20 --json-out out.json
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Same production-mirroring Chromium flags spike_pane_discard_ram.py uses —
# see that module's docstring for why each one matters under offscreen/no-display.
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-background-timer-throttling --disable-renderer-backgrounding "
    "--disable-backgrounding-occluded-windows --renderer-process-limit=4 "
    "--disable-gpu --disable-gpu-compositing",
)

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import psutil  # noqa: E402
from PyQt6.QtWidgets import QApplication, QTabWidget, QWidget  # noqa: E402

# Import BEFORE QApplication is constructed — see module docstring.
from agent_takkub import editor_widget as ew  # noqa: E402
from agent_takkub.editor_widget import EditorHost  # noqa: E402
from agent_takkub.preview_controller import PreviewController  # noqa: E402
from agent_takkub.terminal_widget import TerminalWidget  # noqa: E402


def _pump(app: QApplication, predicate, timeout_s: float, step_s: float = 0.02) -> float:
    t0 = time.monotonic()
    while not predicate() and time.monotonic() - t0 < timeout_s:
        app.processEvents()
        time.sleep(step_s)
    app.processEvents()
    return time.monotonic() - t0


def _rss_mb() -> float:
    gc.collect()
    return psutil.Process().memory_info().rss / (1024 * 1024)


def _webengine_rss_mb() -> tuple[float, int]:
    gc.collect()
    proc = psutil.Process()
    total = 0
    count = 0
    for child in proc.children(recursive=True):
        try:
            if "QtWebEngineProcess" not in child.name():
                continue
            total += child.memory_info().rss
            count += 1
        except psutil.Error:
            continue
    return total / (1024 * 1024), count


def _gc_count(type_name: str) -> int:
    return sum(1 for o in gc.get_objects() if type(o).__name__ == type_name)


def _make_projects(n: int) -> dict[str, dict]:
    tmp_root = Path(tempfile.mkdtemp(prefix="takkub_soak_ws_"))
    projects: dict[str, dict] = {}
    for i in range(n):
        root = tmp_root / f"proj{i}"
        root.mkdir()
        f = root / "sample.py"
        f.write_text(f"# sample project {i}\nprint({i})\n", encoding="utf-8")
        projects[f"proj{i}"] = {"root": root, "file": f}
    return projects


def _editor_soak(
    app: QApplication, projects: dict, cycles: int, settle_ms: int, boot_timeout_s: float
) -> dict:
    def _fake_project_roots(name: str) -> dict[str, Path]:
        return {"main": projects[name]["root"]}

    ew.project_roots = _fake_project_roots  # type: ignore[assignment]

    container = QWidget()
    host = EditorHost(container)  # default view_factory — a REAL QWebEngineView
    open_errors: list[str] = []
    host.fileOpenFailed.connect(lambda p, e: open_errors.append(e))

    before_rss = _rss_mb()
    before_webengine_mb, before_webengine_n = _webengine_rss_mb()
    before_gc = _gc_count("_EditorWebView")

    # Rule 4 ("no painted QWebEngineView reparent across projects") is
    # enforced by EditorHost's own design: `_ensure_view()` only ever
    # constructs a new `_EditorWebView` when `self._view is None`, and
    # every cycle below drives `close_all()` (full teardown, never a
    # hide/reparent) before the next `open_file()`. So the property this
    # soak actually proves is "that teardown+recreate cycle survives N
    # repetitions without the process hard-aborting" (a reparent bug would
    # show up as a native Chromium crash — non-zero exit — not a Python
    # assertion); per-cycle `has_view()`/`open_count()` transitions are
    # checked directly rather than tracking view object identity, since
    # CPython can recycle a freed object's `id()` for its very next
    # allocation — an id-equality check across a destroy+recreate cycle
    # would be testing the allocator, not this code.
    stuck_open: list[int] = []
    stuck_closed: list[int] = []
    names = list(projects)
    for cycle in range(cycles):
        proj_name = names[cycle % len(names)]
        host.open_file(proj_name, str(projects[proj_name]["file"]))
        _pump(app, lambda: host.open_count() > 0 or open_errors, boot_timeout_s)
        if host.open_count() == 0 and not open_errors:
            stuck_open.append(cycle)
        time.sleep(settle_ms / 1000.0)
        _pump(app, lambda: True, 0.1)
        host.close_all()
        _pump(app, lambda: not host.has_view(), 3.0)
        if host.has_view():
            stuck_closed.append(cycle)

    # Chromium renderer processes exit asynchronously after `deleteLater` —
    # give the last cycle's teardown a real chance to complete before
    # sampling, or "after" over-reports (stale not-yet-exited processes),
    # same settle-before-sample convention spike_pane_discard_ram.py uses.
    time.sleep(settle_ms / 1000.0)
    _pump(app, lambda: True, 0.3)
    after_rss = _rss_mb()
    after_webengine_mb, after_webengine_n = _webengine_rss_mb()
    after_gc = _gc_count("_EditorWebView")

    return {
        "cycles": cycles,
        "open_errors": open_errors,
        "stuck_open_cycles": stuck_open,
        "stuck_closed_cycles": stuck_closed,
        "rss_mb_before": round(before_rss, 1),
        "rss_mb_after": round(after_rss, 1),
        "rss_delta_mb": round(after_rss - before_rss, 1),
        "webengine_rss_mb_before": round(before_webengine_mb, 1),
        "webengine_rss_mb_after": round(after_webengine_mb, 1),
        "webengine_process_count_before": before_webengine_n,
        "webengine_process_count_after": after_webengine_n,
        "gc_editorwebview_before": before_gc,
        "gc_editorwebview_after": after_gc,
    }


def _preview_soak(projects: dict, cycles: int) -> dict:
    controller = PreviewController()
    errors: list[str] = []
    names = list(projects)
    for cycle in range(cycles):
        proj_name = names[cycle % len(names)]
        try:
            controller.open_url(proj_name, "http://127.0.0.1:5173")
            controller.close(proj_name)
        except Exception as exc:  # pragma: no cover — soak instrument, report don't raise
            errors.append(f"{type(exc).__name__}: {exc}")
    return {"cycles": cycles, "errors": errors}


def _discard_reattach_soak(
    app: QApplication, cycles: int, debounce_ms: int, timeout_s: float
) -> dict:
    os.environ["TAKKUB_PANE_DISCARD_DEBOUNCE_MS"] = str(debounce_ms)
    tabs = QTabWidget()
    visible_anchor = TerminalWidget()
    pane = TerminalWidget()
    tabs.addTab(visible_anchor, "anchor")
    tabs.addTab(pane, "soak")
    tabs.resize(800, 400)
    tabs.show()
    tabs.setCurrentWidget(visible_anchor)  # pane must be the non-current tab to discard

    boot_elapsed = _pump(app, lambda: pane._page_ready and visible_anchor._page_ready, timeout_s)
    results: list[dict] = []
    if not pane._page_ready:
        visible_anchor.destroy_terminal()
        pane.destroy_terminal()
        return {
            "cycles": 0,
            "boot_elapsed_s": round(boot_elapsed, 2),
            "results": [],
            "all_ok": False,
            "boot_failed": True,
        }

    for cycle in range(cycles):
        pane.set_keepalive(False)
        _pump(app, lambda: pane.is_discarded, timeout_s)
        was_discarded = pane.is_discarded
        pane.write_bytes(f"MIDDISCARD-{cycle}\r\n")  # must queue, not drop, while discarded
        pane.set_keepalive(True)
        reattached_elapsed = _pump(app, lambda: pane._page_ready, timeout_s)
        results.append(
            {
                "cycle": cycle,
                "discarded": bool(was_discarded),
                "reattached": bool(pane._page_ready),
                "reattach_s": round(reattached_elapsed, 2),
            }
        )

    visible_anchor.destroy_terminal()
    pane.destroy_terminal()
    return {
        "cycles": cycles,
        "boot_elapsed_s": round(boot_elapsed, 2),
        "results": results,
        "all_ok": all(r["discarded"] and r["reattached"] for r in results),
        "boot_failed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--projects", type=int, default=2)
    parser.add_argument("--settle-ms", type=int, default=150)
    parser.add_argument("--boot-timeout-s", type=float, default=15.0)
    parser.add_argument("--discard-debounce-ms", type=int, default=200)
    parser.add_argument("--json-out", type=str, default=None)
    args = parser.parse_args(argv)

    if args.projects < 1:
        parser.error("--projects must be >= 1")

    app = QApplication.instance() or QApplication(sys.argv)
    projects = _make_projects(args.projects)

    editor = _editor_soak(app, projects, args.cycles, args.settle_ms, args.boot_timeout_s)
    preview = _preview_soak(projects, args.cycles)
    discard_reattach = _discard_reattach_soak(
        app, args.cycles, args.discard_debounce_ms, args.boot_timeout_s
    )

    ok = (
        not editor["open_errors"]
        and not editor["stuck_open_cycles"]
        and not editor["stuck_closed_cycles"]
        and not preview["errors"]
        and not discard_reattach["boot_failed"]
        and discard_reattach["all_ok"]
    )

    result = {"ok": ok, "editor": editor, "preview": preview, "discard_reattach": discard_reattach}
    out = json.dumps(result, indent=2, ensure_ascii=False)
    print(out)
    if args.json_out:
        Path(args.json_out).write_text(out, encoding="utf-8")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
