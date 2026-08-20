"""Repro for #316: RuntimeError @ pty_session.py:1456 in thread 'pty-teardown'.

`_teardown_resources`'s inner `_teardown()` calls `_writer.quit()` /
`_writer.wait()` (and the same for `_reader`) on a background thread. Both
are QThread children of the PtySession, so app shutdown can delete their
C++ side out from under this thread between the parent-chain teardown and
this call — `quit()`/`wait()` on an already-deleted wrapper then raises
``RuntimeError: wrapped C/C++ object of type _WriterThread has been
deleted``, which was previously unhandled and crashed the teardown thread.

This exercises `_teardown_resources` directly (mocks only, no real Qt event
loop) with fake writer/reader stand-ins that raise that RuntimeError from
quit()/wait(), the same pattern test_pane_transcript.py uses for PtySession.
"""

from __future__ import annotations

from unittest.mock import MagicMock


class _DeletedQObject:
    """Stands in for a QThread whose C++ side Qt has already destroyed."""

    def quit(self) -> None:
        raise RuntimeError("wrapped C/C++ object of type _WriterThread has been deleted")

    def wait(self, _ms: int) -> bool:
        raise RuntimeError("wrapped C/C++ object of type _WriterThread has been deleted")

    def request_stop(self) -> None:
        pass


def _fake_session():
    from agent_takkub.pty_session import PtySession

    session = PtySession.__new__(PtySession)
    session._writer = _DeletedQObject()
    session._reader = _DeletedQObject()
    session._pid = None
    session._proc = None
    session._job_object = None
    session._transcript = None
    session._alive = True
    return session


def test_teardown_survives_writer_quit_on_deleted_qthread() -> None:
    session = _fake_session()

    # Must return cleanly (RuntimeError from the deleted C++ object is
    # swallowed) — not propagate and kill the pty-teardown thread.
    session._teardown_resources(kill_process=True, wait=True)


def test_teardown_survives_reader_quit_on_deleted_qthread() -> None:
    session = _fake_session()
    writer = session._writer = MagicMock()  # healthy writer; only the reader is "deleted"

    session._teardown_resources(kill_process=True, wait=True)

    writer.quit.assert_called_once()
    writer.wait.assert_called_once_with(2000)


def test_teardown_still_joins_healthy_threads() -> None:
    session = _fake_session()
    writer = session._writer = MagicMock()
    reader = session._reader = MagicMock()

    session._teardown_resources(kill_process=True, wait=True)

    writer.quit.assert_called_once()
    writer.wait.assert_called_once_with(2000)
    reader.quit.assert_called_once()
    reader.wait.assert_called_once_with(2000)
