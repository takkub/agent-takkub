from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtWidgets import QPushButton

from agent_takkub.status_header import StatusHeaderMixin


class _Tabs:
    def currentWidget(self):
        return SimpleNamespace(project_name="project-a")


class _Orch:
    def performance_status(self, project=None):
        assert project == "project-a"
        return {
            "cpu_percent": 73,
            "available_memory_percent": 41,
            "available_memory_bytes": 16 * 1024**3,
            "total_memory_bytes": 32 * 1024**3,
            "process_count": 120,
            "overloaded": False,
            "active_heavy_tasks": 2,
            "active_tasks_by_class": {"browser": 1, "build": 1},
            "queued_resource_tasks": 3,
            "spawn_queue_depth": 2,
            "resource_limits": {"max_heavy_global": 4, "max_browser_global": 2},
            "writer_queues": {
                "backend": {
                    "depth": 4,
                    "stale_dropped": 1,
                    "queue_full": 2,
                }
            },
            "duplicate_notices_prevented": 5,
            "main_thread_stall_count": 0,
        }


class _Holder(StatusHeaderMixin):
    def __init__(self):
        self.tabs = _Tabs()
        self.orch = _Orch()
        self._chip_performance = QPushButton()
        self._performance_status_cache = {}


def test_health_chip_surfaces_load_limits_queues_and_reliability_metrics() -> None:
    holder = _Holder()
    holder._refresh_performance_health_chip()
    assert "CPU 73%" in holder._chip_performance.text()
    assert "RAM 41%" in holder._chip_performance.text()
    assert "H 2/4" in holder._chip_performance.text()
    assert "Q 5" in holder._chip_performance.text()
    tooltip = holder._chip_performance.toolTip()
    assert "16.0/32.0 GiB" in tooltip
    assert "Writer depth: 4" in tooltip
    assert "Duplicate notices prevented: 5" in tooltip
