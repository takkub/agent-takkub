"""#535 regression spike — a stalled _reattach() must self-recover, not strand
the pane blank forever.

`TerminalWidget._reattach()` asks Chromium to flip `QWebEnginePage`'s
lifecycle state from Discarded back to Active and then just waits for
`bridge.ready()` to arrive. `setLifecycleState()` returns nothing and that
reverse (Discarded -> Active) transition is a single fire-and-forget request
into Chromium — if it is ever silently refused or dropped, nothing else asks
again: `_page_ready` stays False forever, every future PTY byte queues into
`_pending_writes` and is never painted, even though the underlying agent
process keeps running normally (this is what issue #535 reports: a pane the
orchestrator/status/transcript all show as healthy and actively producing
output, but whose viewport renders permanently blank).

This spike forces exactly that failure by stubbing
`QWebEnginePage.setLifecycleState` to a no-op right before the pane is hidden
(so the Discarded->Active transition _reattach() requests can never
"really" succeed) and then measures whether the pane's own watchdog
(`_reattach_timer` / `_on_reattach_timeout`, forcing a real `_view.load()`)
recovers `_page_ready` within a bounded wait.

Same hard requirements as tools/spike_pane_discard_ram.py (see that file's
docstring for the full rationale): QtWebEngineWidgets imported before any
QApplication, and QApplication(sys.argv) with a real argv — both are needed
to avoid a native Chromium hard-abort under the offscreen QPA platform.

Run directly:

    .venv/Scripts/python.exe tools/spike_pane_reattach_stall.py --json-out out.json

tests/test_pane_reattach_stall_spike.py shells this out as a subprocess (own
process, own crash domain) so a native abort fails one test with a clear
message instead of aborting the whole pytest run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-background-timer-throttling --disable-renderer-backgrounding "
    "--disable-backgrounding-occluded-windows --disable-gpu --disable-gpu-compositing",
)

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

from PyQt6.QtWebEngineCore import QWebEnginePage  # noqa: E402
from PyQt6.QtWidgets import QApplication, QTabWidget  # noqa: E402

# Import BEFORE QApplication is constructed — see module docstring.
from agent_takkub.terminal_widget import TerminalWidget  # noqa: E402


def _pump(app: QApplication, predicate, timeout_s: float, step_s: float = 0.02) -> float:
    t0 = time.monotonic()
    while not predicate() and time.monotonic() - t0 < timeout_s:
        app.processEvents()
        time.sleep(step_s)
    app.processEvents()
    return time.monotonic() - t0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discard-debounce-ms", type=int, default=150)
    parser.add_argument("--reattach-timeout-ms", type=int, default=400)
    parser.add_argument("--boot-timeout-s", type=float, default=15.0)
    parser.add_argument("--recovery-timeout-s", type=float, default=6.0)
    parser.add_argument("--json-out", type=str, default=None)
    args = parser.parse_args(argv)

    os.environ["TAKKUB_PANE_DISCARD_DEBOUNCE_MS"] = str(args.discard_debounce_ms)
    os.environ["TAKKUB_PANE_REATTACH_TIMEOUT_MS"] = str(args.reattach_timeout_ms)

    app = QApplication.instance() or QApplication(sys.argv)
    tabs = QTabWidget()
    visible = TerminalWidget()
    victim = TerminalWidget()
    tabs.addTab(visible, "lead")
    tabs.addTab(victim, "qa")
    tabs.resize(800, 400)
    tabs.show()

    boot_elapsed = _pump(
        app, lambda: visible._page_ready and victim._page_ready, args.boot_timeout_s
    )
    if not (visible._page_ready and victim._page_ready):
        print(json.dumps({"error": "not all panes booted", "elapsed_s": boot_elapsed}))
        return 1

    # Simulate Chromium silently refusing/dropping the lifecycle transition —
    # indistinguishable, from Python's side, from "it worked" (setLifecycleState
    # has no return value and the call is wrapped in a bare try/except).
    real_page = victim._view.page()
    real_page.setLifecycleState = lambda state: None  # no-op every request

    tabs.setCurrentWidget(visible)
    victim.set_keepalive(False)  # arms the discard debounce
    _pump(app, lambda: victim.is_discarded, 3.0)
    believed_discarded = victim.is_discarded
    really_still_active = real_page.lifecycleState() == QWebEnginePage.LifecycleState.Active

    marker = "SHOULD-APPEAR-AFTER-RECOVERY"
    victim.write_bytes(f"{marker}\r\n")

    tabs.setCurrentWidget(victim)
    victim.set_keepalive(True)  # -> _reattach() -> setLifecycleState(Active) [no-op, stubbed]
    recovered_elapsed = _pump(app, lambda: victim._page_ready, args.recovery_timeout_s)

    result = {
        "believed_discarded_after_debounce": believed_discarded,
        "real_page_still_active_when_discard_requested": really_still_active,
        "page_ready_recovered": victim._page_ready,
        "recovered_after_s": round(recovered_elapsed, 2),
        "pending_writes_backlog": len(victim._pending_writes),
    }
    out = json.dumps(result, indent=2)
    print(out)
    if args.json_out:
        Path(args.json_out).write_text(out, encoding="utf-8")

    for p in (visible, victim):
        p.destroy_terminal()
    return 0 if result["page_ready_recovered"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
