from agent_takkub.doctor import Status, check_performance_live


def test_performance_live_reports_governor_and_queue_metrics() -> None:
    findings = check_performance_live(
        {
            "ok": True,
            "cpu_percent": 42,
            "available_memory_percent": 61,
            "active_heavy_tasks": 2,
            "queued_resource_tasks": 1,
            "overloaded": False,
            "writer_queues": {
                "backend": {"depth": 3, "stale_dropped": 2},
                "qa": {"depth": 1, "stale_dropped": 1},
            },
        }
    )
    assert findings[0].status is Status.OK
    assert "writer-max=3" in findings[0].detail
    assert "stale-dropped=3" in findings[0].detail


def test_performance_live_warns_when_resource_protection_is_active() -> None:
    finding = check_performance_live({"ok": True, "overloaded": True, "writer_queues": {}})[0]
    assert finding.status is Status.WARN
