"""#364 lever 1 spike — repeatable measurement wrapper.

The measurement itself (`tools/spike_pane_discard_ram.py`) constructs real
QWebEngineView instances, which is exactly the thing tests/test_terminal_widget.py
and tests/test_editor_widget.py document as a pytest-process hard-abort risk.
So this test never imports that module or constructs a TerminalWidget itself —
it shells the script out as a **subprocess** (own process, own crash domain)
and only asserts on its exit code + JSON output. A crash in there fails this
one test with a clear message instead of aborting the whole pytest run.

Kept intentionally light (2 panes / 1 hidden, short settle) so it's cheap
enough for `takkub qa-gate --targeted tests/test_pane_discard_spike.py`
without needing a full Chromium warm-up budget; the numbers used for the
actual go/no-go call in the #364 writeup were gathered with heavier
`--panes 4/6` runs of the script directly, not from this test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "tools" / "spike_pane_discard_ram.py"


def _run_spike(*extra_args: str, timeout: float = 60.0) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--panes",
            "2",
            "--hidden",
            "1",
            "--settle-ms",
            "500",
            *extra_args,
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert proc.returncode == 0, (
        f"spike script exited {proc.returncode} (a real QWebEngineView hard-abort shows up here "
        f"as a large negative code, e.g. -1073740791, rather than a Python traceback)\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    # Chromium logs (GPU fallback warnings etc.) land on stdout ahead of the
    # JSON blob under the offscreen platform — the JSON itself nests dicts
    # (the "reattach" list), so anchor on the FIRST '{' (top-level open),
    # not the last.
    payload = proc.stdout[proc.stdout.find("{") :]
    return json.loads(payload)


@pytest.mark.timeout(90)
class TestPaneDiscardSpike:
    def test_discard_frees_hidden_renderer_ram(self) -> None:
        result = _run_spike("--lifecycle", "discard")
        assert result["lifecycle_effective_all"] is True
        assert result["panes_hidden"] == 1
        # Sanity bound, not the go/no-go threshold itself (that's a judgement
        # call over the heavier --panes 4 numbers in the #364 report) — just
        # proves discard is doing *something* real on this machine, not 0.
        assert result["rss_freed_mb_per_hidden_pane"] > 10.0

    def test_reattach_restores_content_and_preserves_scrollback(self) -> None:
        """Lever 1 implementation (post-spike): TerminalWidget snapshots the
        buffer as plain text before discarding and replays it in
        `_on_page_ready` right after reattach, so scrollback is no longer
        lost — this was the spike's one open limitation, now closed."""
        result = _run_spike("--lifecycle", "discard")
        assert len(result["reattach"]) == 1
        reattach = result["reattach"][0]
        assert reattach["new_content_matched"] is True
        assert reattach["total_ms"] < 5000
        assert reattach["scrollback_lost"] is False

    def test_write_while_discarded_queues_not_drops(self) -> None:
        """#364 lever 1 requirement: a PTY write arriving while the pane is
        discarded (TerminalWidget._page_ready gate) must survive to reattach
        via `_pending_writes`, not vanish into a torn-down page."""
        result = _run_spike("--lifecycle", "discard")
        reattach = result["reattach"][0]
        assert reattach["mid_discard_write_preserved"] is True

    def test_frozen_lifecycle_does_not_free_meaningful_ram(self) -> None:
        result = _run_spike("--lifecycle", "frozen")
        assert result["lifecycle_effective_all"] is True
        assert abs(result["rss_freed_mb_per_hidden_pane"]) < 10.0
