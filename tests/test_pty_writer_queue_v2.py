from __future__ import annotations

import time

from agent_takkub.pty_session import (
    PtyWriteMessage,
    WritePriority,
    _ReaderThread,
    _WriterThread,
)


class _Proc:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)


def test_writer_queue_is_bounded_and_reserves_control_capacity() -> None:
    writer = _WriterThread(_Proc(), maxsize=4, control_reserve=1)
    assert writer.write("t1", priority=WritePriority.TASK)
    assert writer.write("t2", priority=WritePriority.TASK)
    assert writer.write("t3", priority=WritePriority.TASK)
    assert not writer.write("t4", priority=WritePriority.TASK)
    assert writer.queue_depth == 3
    assert writer.write("enter", priority=WritePriority.CONTROL)
    assert writer.queue_depth == 4


def test_control_and_user_preempt_background_work() -> None:
    writer = _WriterThread(_Proc(), maxsize=4, control_reserve=1)
    assert writer.write("background", priority=WritePriority.BACKGROUND)
    assert writer.write("task-1", priority=WritePriority.TASK)
    assert writer.write("task-2", priority=WritePriority.TASK)
    assert writer.write("user", priority=WritePriority.USER)
    assert writer.write("ctrl-c", priority=WritePriority.CONTROL)
    with writer._condition:
        assert writer._pop_next().data == "ctrl-c"
        assert writer._pop_next().data == "user"


def test_stale_generation_and_ttl_are_checked_at_native_write() -> None:
    proc = _Proc()
    generation = [2]
    writer = _WriterThread(
        proc,
        maxsize=8,
        control_reserve=1,
        generation_getter=lambda: generation[0],
    )
    writer.write(
        PtyWriteMessage(
            "old-generation",
            session_generation=1,
            priority=WritePriority.TASK,
        )
    )
    writer.write(
        PtyWriteMessage(
            "expired",
            expires_at=time.time() - 1,
            session_generation=2,
            priority=WritePriority.TASK,
        )
    )
    writer.write(
        PtyWriteMessage(
            "live",
            expires_at=time.time() + 10,
            session_generation=2,
            priority=WritePriority.TASK,
        )
    )
    writer.start()
    deadline = time.time() + 2
    while "live" not in proc.writes and time.time() < deadline:
        time.sleep(0.01)
    writer.request_stop()
    writer.wait(1000)
    assert proc.writes == ["live"]
    assert writer.stale_drop_count == 2


def test_cancelled_delivery_validator_is_checked_at_native_write() -> None:
    proc = _Proc()
    still_valid = [False]
    writer = _WriterThread(proc, maxsize=8, control_reserve=1)
    writer.write(
        PtyWriteMessage(
            "cancelled-delivery",
            priority=WritePriority.TASK,
            delivery_id="delivery-1",
            validator=lambda: still_valid[0],
        )
    )
    writer.start()
    deadline = time.time() + 2
    while writer.queue_depth and time.time() < deadline:
        time.sleep(0.01)
    writer.request_stop()
    writer.wait(1000)
    assert proc.writes == []
    assert writer.stale_drop_count == 1


class _ReadProc:
    def __init__(self) -> None:
        self._chunks = iter([b"a" * 600, b"b" * 600, b""])
        self._alive = True

    def read(self, _size: int):
        data = next(self._chunks)
        if not data:
            self._alive = False
        return data

    def isalive(self) -> bool:
        return self._alive


def test_reader_batches_parser_and_render_delivery() -> None:
    batches: list[bytes] = []
    reader = _ReaderThread(
        _ReadProc(),
        on_data=batches.append,
        batch_ms=10_000,
        batch_bytes=1024,
    )
    reader.start()
    assert reader.wait(1000)
    assert batches == [b"a" * 600 + b"b" * 600]
