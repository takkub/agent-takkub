"""#364 lever 5 spike — repeatable measurement wrapper.

Same reasoning as tests/test_pane_discard_spike.py: the measurement itself
(`tools/spike_main_process_ram_profile.py`) constructs real QWebEngineView
instances, which is documented as a pytest-process hard-abort risk — so this
shells the script out as a subprocess and only asserts on its exit code +
JSON output, never imports it directly.

Kept light (2 panes, short settle) for `takkub qa-gate --targeted
tests/test_main_process_ram_profile_spike.py`; the 5-pane numbers used for
the #364 writeup were gathered by running the script directly."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "tools" / "spike_main_process_ram_profile.py"


def _run_spike(*extra_args: str, timeout: float = 60.0) -> dict:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--panes", "2", "--settle-ms", "300", *extra_args],
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
    payload = proc.stdout[proc.stdout.find("{") :]
    return json.loads(payload)


# Same opt-in gate as test_pane_discard_spike.py — the script this shells out
# to constructs real QWebEngineView instances, documented as a hard-abort
# risk (segfault) under offscreen/no-display CI.
@pytest.mark.skipif(
    os.environ.get("AGENT_TAKKUB_QT_WEBENGINE_SMOKE") != "1",
    reason="opt-in — real QWebEngineView segfaults under offscreen/no-display CI",
)
@pytest.mark.timeout(90)
class TestMainProcessRamProfileSpike:
    def test_terminalwidget_wrappers_are_fully_released_after_close(self) -> None:
        """The leak check this task exists to answer: create N panes, destroy
        them, force gc — do the Python-side wrapper objects actually go away?
        (2026-08-23 5-pane run: yes, 0 remained — see
        docs/audit/2026-08-23-364-lever5-main-process-profile.md. This test
        pins that at a cheaper 2-pane count so it stays part of the regular
        targeted-test surface, not just a one-off manual run.)"""
        result = _run_spike()
        assert result["terminalwidget_python_wrappers_still_alive_after_close"] == 0
        assert result["after_close_object_census"]["watched"]["TerminalWidget"] == 0
        assert result["after_close_object_census"]["watched"]["QWebEngineView"] == 0

    def test_reports_per_pane_rss_growth_and_top_allocators(self) -> None:
        result = _run_spike()
        assert result["panes"] == 2
        assert result["rss_growth_mb_per_pane"] > 0
        assert len(result["tracemalloc_top_allocators_kb"]) > 0
        assert result["tracemalloc_current_traced_mb"] >= 0
