"""#535 regression — a stalled TerminalWidget reattach must self-recover.

Shells out to tools/spike_pane_reattach_stall.py as a subprocess (own
process, own crash domain) for the same reason tests/test_pane_discard_spike.py
does: the script constructs real QWebEngineView instances, which is a
documented native-abort risk inside the pytest process itself. See that
file's docstring for the full rationale, and this repo's tools script's
docstring for exactly what failure mode is being reproduced.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "tools" / "spike_pane_reattach_stall.py"


def _run_spike(timeout: float = 60.0) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    # Chromium logs (GPU fallback warnings etc.) land on stdout ahead of the
    # JSON blob under the offscreen platform.
    payload = proc.stdout[proc.stdout.find("{") :]
    return proc.returncode, json.loads(payload)


# Same opt-in gate as test_pane_discard_spike.py / test_editor_widget.py's
# real-QWebEngineView tests — a real QWebEngineView can hard-abort under
# QT_QPA_PLATFORM=offscreen on a machine with no display/GPU (confirmed on
# macOS CI, run 32633203191). Run directly with the env var set (and/or on a
# machine with a real display) to actually exercise this.
@pytest.mark.skipif(
    os.environ.get("AGENT_TAKKUB_QT_WEBENGINE_SMOKE") != "1",
    reason="opt-in — real QWebEngineView segfaults under offscreen/no-display CI (run 32633203191, macOS)",
)
@pytest.mark.timeout(90)
def test_reattach_recovers_when_lifecycle_transition_is_silently_dropped() -> None:
    """`_reattach()`'s Discarded->Active request is fire-and-forget with no
    return value; if Chromium ever silently refuses/drops it, `_page_ready`
    must not be stranded False forever (#535: viewport goes permanently
    blank while the underlying agent process keeps running fine). The
    `_reattach_timer` watchdog must force a real `_view.load()` and recover.
    """
    returncode, result = _run_spike()
    assert result["believed_discarded_after_debounce"] is True
    assert result["real_page_still_active_when_discard_requested"] is True
    assert result["page_ready_recovered"] is True, (
        f"pane never recovered from a stalled reattach — this is #535: {result}"
    )
    assert result["pending_writes_backlog"] == 0, "queued PTY output must flush once recovered"
    assert returncode == 0
