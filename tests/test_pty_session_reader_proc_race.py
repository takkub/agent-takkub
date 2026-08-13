"""Repro for #179: AttributeError 'NoneType' object has no attribute 'isalive'
at pty_session.py:610.

Root cause: `_teardown_resources`'s background `_teardown()` joins the reader
thread with a *bounded* `.wait(2000)` and then unconditionally sets
`thread_obj._proc = None` (pty_session.py ~941), regardless of whether the
join actually succeeded. If `_ReaderThread.run()` is still mid-iteration past
that timeout — it just caught `EOFError` from `proc.read()` and is about to
check `self._proc.isalive()` — the concurrent null wins the race and the
`self._proc.isalive()` re-read raises AttributeError on `None`.

This test reproduces the race deterministically (no timing dependency): the
fake proc's `read()` nulls `reader._proc` as a side effect, standing in for
the teardown thread's write landing between `proc.read()` raising and the
`isalive()` check that follows it.
"""

from __future__ import annotations

from agent_takkub.pty_session import _ReaderThread


class _NullingProc:
    """Raises EOFError from read(), and — simulating a concurrent teardown
    thread — nulls the reader's `_proc` attribute before returning."""

    def __init__(self, reader_box: dict) -> None:
        self._reader_box = reader_box
        self.isalive_calls = 0

    def read(self, _size: int) -> bytes:
        self._reader_box["reader"]._proc = None  # concurrent teardown, simulated
        raise EOFError("Pty is closed")

    def isalive(self) -> bool:
        self.isalive_calls += 1
        return False


def test_reader_thread_survives_proc_nulled_between_read_and_isalive() -> None:
    box: dict = {}
    proc = _NullingProc(box)
    reader = _ReaderThread(proc, on_data=None)
    box["reader"] = reader

    # Must return cleanly (loop breaks on dead process) — not raise AttributeError.
    reader.run()

    assert proc.isalive_calls >= 1, "isalive() must be checked on the read proc, not self._proc"
