"""#365 phase 10 — repeatable measurement wrapper for
`tools/soak_workspace_webengine.py`.

Same crash-isolation reason as tests/test_pane_discard_spike.py: the script
constructs real QWebEngineView instances, so it runs as a **subprocess**
(own process, own crash domain) — a reparent/lifecycle bug that hard-aborts
the process fails this test with a clear message instead of aborting the
whole pytest run.

Kept light (few cycles / few projects) so it's cheap enough for
`takkub qa-gate --targeted tests/test_workspace_webengine_soak.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "tools" / "soak_workspace_webengine.py"


def _run_soak(*extra_args: str, timeout: float = 90.0) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--cycles",
            "3",
            "--projects",
            "2",
            "--settle-ms",
            "100",
            "--discard-debounce-ms",
            "150",
            *extra_args,
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert proc.returncode == 0, (
        f"soak script exited {proc.returncode} (a real QWebEngineView reparent/lifecycle "
        f"hard-abort shows up here as a large negative code, e.g. -1073740791, rather than a "
        f"Python traceback)\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    payload = proc.stdout[proc.stdout.find("{") :]
    return json.loads(payload)


# Same opt-in gate as test_pane_discard_spike.py / test_editor_widget.py's
# real-QWebEngineView smoke — unverified on CI's macos/ubuntu offscreen
# runners, run directly with the env var set (and/or a real display) to
# actually exercise this.
@pytest.mark.skipif(
    os.environ.get("AGENT_TAKKUB_QT_WEBENGINE_SMOKE") != "1",
    reason="opt-in — real QWebEngineView segfaults under offscreen/no-display CI",
)
@pytest.mark.timeout(120)
class TestWorkspaceWebEngineSoak:
    def test_overall_result_is_ok(self) -> None:
        result = _run_soak()
        assert result["ok"] is True

    def test_editor_open_close_leaves_no_stuck_cycles(self) -> None:
        result = _run_soak()
        editor = result["editor"]
        assert editor["open_errors"] == []
        assert editor["stuck_open_cycles"] == []
        assert editor["stuck_closed_cycles"] == []

    def test_preview_state_machine_cycles_without_error(self) -> None:
        result = _run_soak()
        assert result["preview"]["errors"] == []

    def test_pane_discard_reattach_survives_repeated_cycles(self) -> None:
        result = _run_soak()
        dr = result["discard_reattach"]
        assert dr["boot_failed"] is False
        assert dr["all_ok"] is True
        assert len(dr["results"]) == 3
        assert all(r["discarded"] and r["reattached"] for r in dr["results"])
