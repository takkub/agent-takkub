"""#986: `takkub provider probe codex` printed `used=0%` and then "still
limited" with no reason — the 0% is codex's 5h primary window only; its
weekly (secondary) window was still exhausted, which correctly keeps the
quota-hit. The verdict must name the window that kept it."""

from __future__ import annotations

from types import SimpleNamespace

from agent_takkub.cli import _probe_keep_reason


def _usage(*windows, status="active"):
    return SimpleNamespace(status=status, utilization=0.0, windows=list(windows))


def test_full_weekly_window_is_named() -> None:
    usage = _usage(
        {"name": "primary", "utilization": 0.0}, {"name": "secondary", "utilization": 100.0}
    )
    assert _probe_keep_reason(usage) == "window secondary ใช้ไป 100%"


def test_window_without_a_figure_is_named() -> None:
    usage = _usage(
        {"name": "primary", "utilization": 0.0}, {"name": "secondary", "utilization": None}
    )
    assert "secondary ไม่มีตัวเลข" in _probe_keep_reason(usage)


def test_inactive_probe_says_unconfirmed() -> None:
    assert "status=error" in _probe_keep_reason(_usage(status="error"))
