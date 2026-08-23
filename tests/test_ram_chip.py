"""#364 lever 6 — performance chip's background RAM sampler.

Mirrors test_performance_health_chip.py's holder pattern. The worker itself
(`_RamSnapshotWorker`) does real psutil work off-thread when scheduled — these
tests only assert the Qt-thread side (tooltip rendering from whatever is
already cached), never wait on the async worker to land."""

from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtWidgets import QPushButton

from agent_takkub.status_header import StatusHeaderMixin


class _Tabs:
    def currentWidget(self):
        return SimpleNamespace(project_name="project-a")


class _Orch:
    def performance_status(self, project=None):
        return {
            "cpu_percent": 10,
            "available_memory_percent": 80,
            "overloaded": False,
            "resource_limits": {},
        }

    def pane_ram_specs(self):
        return [{"role": "backend", "project": "project-a", "provider": "claude", "pid": 1234}]


class _Holder(StatusHeaderMixin):
    def __init__(self) -> None:
        self.tabs = _Tabs()
        self.orch = _Orch()
        self._chip_performance = QPushButton()
        self._performance_status_cache: dict = {}
        self._ram_snapshot_cache: dict | None = None
        self._ram_snapshot_cache_at: float = 0.0
        self._ram_snapshot_worker_busy = False


def test_ram_tooltip_line_shows_sampling_before_any_worker_result() -> None:
    holder = _Holder()
    holder._refresh_performance_health_chip()
    assert "RAM top 3 (pane): (sampling…)" in holder._chip_performance.toolTip()


def test_ram_tooltip_line_shows_top3_from_worker_cache() -> None:
    holder = _Holder()
    holder._on_ram_snapshot_ready(
        {
            "panes": [
                {"role": "backend", "total_bytes": 700_000_000},
                {"role": "qa", "total_bytes": 650_000_000},
                {"role": "frontend", "total_bytes": 600_000_000},
                {"role": "devops", "total_bytes": 500_000_000},
            ]
        }
    )
    # Force the TTL guard past so the assertion below reads the cache set
    # above, unaffected by whether a real background worker also got kicked
    # off (it may — that's fine, its result lands asynchronously later).
    holder._ram_snapshot_cache_at = 1e18
    holder._refresh_performance_health_chip()
    tooltip = holder._chip_performance.toolTip()
    assert "RAM top 3 (pane): backend 668MB · qa 620MB · frontend 572MB" in tooltip
    assert "devops" not in tooltip.split("RAM top 3")[1].split("\n")[0]


def test_on_ram_snapshot_ready_ignores_a_failed_worker_result() -> None:
    holder = _Holder()
    holder._on_ram_snapshot_ready(None)
    assert holder._ram_snapshot_cache is None
    assert holder._ram_snapshot_worker_busy is False
