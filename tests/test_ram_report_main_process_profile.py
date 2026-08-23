"""#364 lever 5 — `ram_report.collect_main_process_profile`.

Unlike `collect_ram_report` (which fakes psutil to test its tree-walk logic
against synthetic processes), this exercises the REAL function against the
actual pytest process — safe to do because tracemalloc/gc only ever look at
whatever process calls them; there is nothing here that spawns a subprocess,
touches a real cockpit, or leaves tracemalloc running afterward."""

from __future__ import annotations

import tracemalloc

from agent_takkub.ram_report import collect_main_process_profile


def test_stops_tracemalloc_it_started() -> None:
    assert not tracemalloc.is_tracing()

    result = collect_main_process_profile()

    assert not tracemalloc.is_tracing()
    assert result["tracemalloc_was_already_running"] is False


def test_leaves_tracemalloc_running_if_it_was_already_on() -> None:
    tracemalloc.start()
    try:
        result = collect_main_process_profile()
        assert result["tracemalloc_was_already_running"] is True
        assert tracemalloc.is_tracing()
    finally:
        tracemalloc.stop()


def test_shape_and_watched_object_counts() -> None:
    result = collect_main_process_profile(top_n=5)

    assert isinstance(result["gc_object_count"], int)
    assert result["gc_object_count"] > 0
    assert len(result["top_allocators"]) <= 5
    assert len(result["top_object_types"]) <= 5
    for entry in result["top_allocators"]:
        assert set(entry) == {"file", "line", "size_bytes", "count"}
    watched = result["watched_pane_object_counts"]
    assert set(watched) == {"AgentPane", "TerminalWidget", "PtySession", "HeadlessPane"}
    # None of these classes exist in a bare pytest process with no cockpit —
    # every watched count should be exactly 0, not merely "small".
    assert all(count == 0 for count in watched.values())
