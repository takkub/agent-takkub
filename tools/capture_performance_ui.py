"""Capture deterministic offscreen evidence for the reliability UI gate."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _bootstrap(scale: float, data_home: Path) -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = str(scale)
    os.environ["AGENT_TAKKUB_HOME"] = str(data_home)
    os.environ["TAKKUB_SKIP_MCP_WARM"] = "1"
    os.environ["TAKKUB_SKIP_GRAFT_BUILD"] = "1"
    os.environ["TAKKUB_SKIP_NATIVE_CHROME"] = "1"
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _bootstrap(args.scale, args.output_dir / "capture-home")

    from PyQt6.QtCore import QPoint, QRect, Qt
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import (
        QApplication,
        QLabel,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    from agent_takkub import cockpit_theme
    from agent_takkub.provider_usage import ProviderUsage
    from agent_takkub.settings_window import VIEW_PERFORMANCE, SettingsWindow
    from agent_takkub.status_header import StatusHeaderMixin
    from agent_takkub.usage_meter import (
        UsageMeter,
        _popup_position,
        _ProviderDetailPopup,
    )

    app = QApplication.instance() or QApplication([])
    fonts = cockpit_theme.ensure_fonts_loaded()
    capture_font = QFont(str(fonts["sans"]))
    app.setFont(capture_font)
    app.setStyleSheet(cockpit_theme.build_stylesheet(str(fonts["sans"]), str(fonts["mono"])))
    now = datetime.now(tz=UTC)
    usages = [
        ProviderUsage(
            provider="codex",
            status="active",
            plan="Plus",
            utilization=27,
            resets_at=now + timedelta(hours=2),
            fetched_at=now,
        ),
        ProviderUsage(
            provider="claude",
            status="active",
            plan="Max",
            utilization=61,
            resets_at=now + timedelta(hours=1),
            fetched_at=now,
            raw_data={
                "windows": {
                    "five_hour": {"utilization": 27, "eta": "2ชม. 3น."},
                    "seven_day": {"utilization": 61, "eta": "3วัน 4ชม."},
                }
            },
        ),
        ProviderUsage(provider="gemini", status="loading"),
        ProviderUsage(provider="opencode", status="unsupported"),
    ]

    def capture_meter(name: str, size: tuple[int, int], anchor: QPoint) -> None:
        canvas = QWidget()
        canvas.setObjectName("meterEvidence")
        canvas.setStyleSheet(
            f"QWidget#meterEvidence {{ background:{cockpit_theme.GROUND_WINDOW}; }}"
        )
        canvas.setFont(capture_font)
        canvas.resize(*size)
        title = QLabel(f"Token Meter · Lead: Codex · scale {args.scale:.2f}x", canvas)
        title.setStyleSheet(f"color:{cockpit_theme.TEXT_PRIMARY}; font-size:16px;")
        title.move(18, 18)
        meter = UsageMeter(canvas)
        meter.setFont(capture_font)
        meter.set_usages(usages, primary_provider="codex")
        meter.adjustSize()
        meter.move(anchor)
        popup = _ProviderDetailPopup(usages, meter)
        popup.setFont(capture_font)
        popup.setWindowFlags(Qt.WindowType.Widget)
        popup.setParent(canvas)
        popup.setFixedWidth(min(380, size[0] - 16))
        popup.adjustSize()
        popup_pos = _popup_position(
            QRect(meter.pos(), meter.size()),
            popup.size(),
            QRect(0, 0, size[0], size[1]),
        )
        popup.move(popup_pos)
        popup.show()
        meter.show()
        canvas.show()
        app.processEvents()
        assert canvas.grab().save(str(args.output_dir / f"token-meter-{name}.png"))
        canvas.close()

    capture_meter("right-edge", (760, 700), QPoint(610, 60))
    capture_meter("bottom-edge", (760, 700), QPoint(610, 660))
    capture_meter("narrow-window", (430, 700), QPoint(280, 60))

    settings = SettingsWindow(initial_view=VIEW_PERFORMANCE)
    settings.resize(1080, 760)
    settings.show()
    app.processEvents()
    assert settings.grab().save(str(args.output_dir / "performance-settings.png"))
    settings.close()

    class _Health(StatusHeaderMixin):
        pass

    health = _Health()
    health._chip_performance = QPushButton()
    health._performance_status_cache = {}
    health.tabs = type(
        "Tabs",
        (),
        {"currentWidget": lambda self: type("Tab", (), {"project_name": "demo"})()},
    )()
    snapshot = {
        "cpu_percent": 42,
        "available_memory_percent": 58,
        "available_memory_bytes": 18 * 1024**3,
        "total_memory_bytes": 32 * 1024**3,
        "process_count": 126,
        "overloaded": False,
        "active_heavy_tasks": 2,
        "active_tasks_by_class": {"browser": 1, "build": 1, "test": 0},
        "queued_resource_tasks": 1,
        "spawn_queue_depth": 0,
        "resource_limits": {"max_heavy_global": 4, "max_browser_global": 2},
        "writer_queues": {"backend": {"depth": 2, "stale_dropped": 0, "queue_full": 0}},
        "duplicate_notices_prevented": 3,
        "main_thread_stall_count": 0,
    }
    health.orch = type("Orch", (), {"performance_status": lambda self, project=None: snapshot})()
    health._refresh_performance_health_chip()
    panel = QWidget()
    panel.setStyleSheet(f"background:{cockpit_theme.GROUND_WINDOW};")
    layout = QVBoxLayout(panel)
    layout.addWidget(QLabel("Cockpit Health Status"))
    health._chip_performance.setParent(panel)
    layout.addWidget(health._chip_performance)
    detail = QLabel(health._chip_performance.toolTip(), panel)
    detail.setStyleSheet(f"color:{cockpit_theme.TEXT_PRIMARY}; font-size:13px;")
    layout.addWidget(detail)
    panel.resize(620, 420)
    panel.show()
    app.processEvents()
    assert panel.grab().save(str(args.output_dir / "performance-health.png"))
    panel.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
