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
    # #292: chip prints RAM *used* (100 - available), same direction as the
    # CPU figure beside it — 41% available means 59% used.
    assert "RAM 59%" in holder._chip_performance.text()
    assert "H 2/4" in holder._chip_performance.text()
    assert "Q 5" in holder._chip_performance.text()
    tooltip = holder._chip_performance.toolTip()
    # The tooltip keeps the available side, explicitly labelled.
    assert "Available RAM: 41%" in tooltip
    assert "16.0/32.0 GiB" in tooltip
    assert "Writer depth: 4" in tooltip
    assert "Duplicate notices prevented: 5" in tooltip


def test_health_chip_ram_climbs_as_memory_fills() -> None:
    """Regression guard for #292: the chip number must rise when memory runs
    out, never fall. The old chip printed `available_memory_percent`, so a box
    down to its last 8% of RAM displayed "RAM 8%" — a reassuring-looking figure
    at the exact moment the governor was about to declare OVERLOAD."""
    holder = _Holder()
    holder.orch.performance_status = lambda project=None: {
        "cpu_percent": 12,
        "available_memory_percent": 8,
        "overloaded": True,
        "resource_limits": {"max_heavy_global": 4},
    }
    holder._refresh_performance_health_chip()
    assert "RAM 92%" in holder._chip_performance.text()
    assert "SYS OVERLOAD" in holder._chip_performance.text()


class _WaitingResumeOrch:
    """Reproduces issue #274's exact field dialog: RAM recovered above its
    20% pause line to 21.5% but not yet up to the 25% resume line, while CPU
    (29.1%) was never near either of its own thresholds."""

    def performance_status(self, project=None):
        return {
            "cpu_percent": 29.1,
            "available_memory_percent": 21.5,
            "available_memory_bytes": int(8.5 * 1024**3),
            "total_memory_bytes": int(39.6 * 1024**3),
            "process_count": 300,
            "overloaded": True,
            "overload_reason": "waiting_resume",
            "active_heavy_tasks": 2,
            "active_tasks_by_class": {"browser": 1, "build": 0, "test": 0},
            "queued_resource_tasks": 2,
            "spawn_queue_depth": 0,
            "fanout_queue_depth": 0,
            "resource_limits": {
                "max_heavy_global": 4,
                "max_browser_global": 2,
                "max_build_global": 2,
                "max_test_global": 2,
                "cpu_pause_percent": 85,
                "cpu_resume_percent": 65,
                "min_available_ram_percent": 20,
                "resume_ram_percent": 25,
            },
            "writer_queues": {},
            "duplicate_notices_prevented": 0,
            "main_thread_stall_count": 0,
            "waiting_tasks": [
                {"reason": "waiting_resume"},
            ],
        }


class _WaitingResumeHolder(StatusHeaderMixin):
    def __init__(self):
        self.tabs = _Tabs()
        self.orch = _WaitingResumeOrch()
        self._chip_performance = QPushButton()
        self._performance_status_cache = {}


def test_health_dialog_does_not_blame_cpu_when_ram_is_the_actual_latch(monkeypatch) -> None:
    import agent_takkub.status_header as sh_mod

    captured: dict[str, str] = {}
    monkeypatch.setattr(
        sh_mod.QMessageBox,
        "information",
        lambda *a, **kw: captured.setdefault("text", a[2]),
    )

    holder = _WaitingResumeHolder()
    holder._show_performance_health_dialog()

    text = captured["text"]
    assert "cpu_high" not in text
    assert "CPU above pause threshold" not in text
    assert "waiting to clear resume thresholds" in text
    assert "Thresholds: CPU pause ≥85% / resume ≤65% · RAM pause <20% / resume ≥25%" in text
    assert "Needed to resume: RAM ≥25% (now 21.5%)" in text
