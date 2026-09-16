"""#640: `boot_phase` stamps every boot milestone with ms since process start.

A prod boot after an update took ~50 s with a 30 s hole no event could
attribute, and the stall watchdog only starts after the main window exists,
so it was blind to that hole. These events are what makes the next such boot
explain itself — pin that they fire, carry `t_ms`, and stay silent outside a
real boot (CLI, tests)."""

from __future__ import annotations

import time

from agent_takkub import orchestrator_text as ot


def test_silent_outside_a_boot(monkeypatch):
    seen: list = []
    monkeypatch.setattr(ot, "_BOOT_T0", None)
    monkeypatch.setattr(ot, "_log_event", lambda event, **kw: seen.append((event, kw)))
    ot.boot_phase("anything")
    assert seen == []


def test_stamps_elapsed_ms_after_mark(monkeypatch):
    seen: list = []
    monkeypatch.setattr(ot, "_log_event", lambda event, **kw: seen.append((event, kw)))
    monkeypatch.setattr(ot, "_BOOT_T0", time.monotonic() - 1.5)
    ot.boot_phase("gate_end", wizard=True)
    assert len(seen) == 1
    event, kw = seen[0]
    assert event == "boot_phase"
    assert kw["phase"] == "gate_end"
    assert kw["wizard"] is True
    assert 1400 <= kw["t_ms"] <= 3000


def test_mark_boot_start_sets_origin(monkeypatch):
    monkeypatch.setattr(ot, "_BOOT_T0", None)
    ot.mark_boot_start()
    try:
        assert ot._BOOT_T0 is not None
        assert time.monotonic() - ot._BOOT_T0 < 5
    finally:
        ot._BOOT_T0 = None


def test_events_log_async_writer_keeps_order_and_flushes(tmp_path, monkeypatch):
    """#640: events.log lines are built on the caller's thread and written by
    one background writer — order preserved, nothing lost after a flush."""
    import json as _json
    import threading as _threading

    monkeypatch.delenv("TAKKUB_EVENTS_LOG_SYNC", raising=False)
    log = tmp_path / "events.log"
    monkeypatch.setattr(ot, "EVENTS_LOG", log)
    monkeypatch.setattr(
        ot, "_orch_attr", lambda name, default: log if name == "EVENTS_LOG" else default
    )
    main_ident = _threading.get_ident()
    writer_idents: list[int] = []
    real = ot._append_event_lines

    def _spy(path, max_bytes, lines):
        writer_idents.append(_threading.get_ident())
        real(path, max_bytes, lines)

    monkeypatch.setattr(ot, "_append_event_lines", _spy)
    for i in range(200):
        ot._log_event("probe", n=i)
    assert ot.flush_events_log(5.0)
    rows = [_json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [r["n"] for r in rows] == list(range(200))
    assert writer_idents and all(w != main_ident for w in writer_idents)
    assert len(writer_idents) < 200, "bursts must be batched"
