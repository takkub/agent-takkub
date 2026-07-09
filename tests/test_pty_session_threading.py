"""Tests for moving pyte parsing off the Qt main thread.

stream.feed() now runs in the reader thread (via _feed_and_log) while the main
thread reads the screen (display_lines / is_at_*_prompt). A lock guards the
pyte screen. These tests verify the screen still updates correctly and that
concurrent feed+read can't crash. See docs/cockpit-freeze-rca-2026-05-29.md.
"""

from __future__ import annotations

import threading

from agent_takkub.pty_session import PtySession


def test_feed_and_log_updates_screen() -> None:
    s = PtySession(cols=80, rows=24)
    s._feed_and_log(b"hello world")
    assert "hello world" in "\n".join(s.display_lines())


def test_on_bytes_no_longer_feeds_pyte() -> None:
    # _on_bytes runs on the main thread and must only emit (render + notify);
    # the pyte feed is the reader thread's job (_feed_and_log).
    s = PtySession(cols=80, rows=24)
    s._on_bytes(b"should-not-be-parsed-into-screen")
    assert "should-not-be-parsed" not in "\n".join(s.display_lines())


def test_feed_and_log_never_raises_on_bad_chunk() -> None:
    s = PtySession(cols=80, rows=24)
    # Partial / garbage escape sequences must not propagate out.
    s._feed_and_log(b"\x1b[")  # truncated CSI
    s._feed_and_log(b"\xff\xfe random")
    # still usable
    s._feed_and_log(b"ok")
    assert "ok" in "\n".join(s.display_lines())


def test_concurrent_feed_and_read_no_crash() -> None:
    """A reader thread feeding pyte while the main thread reads screen state
    must never raise (the screen lock serialises access)."""
    s = PtySession(cols=80, rows=24)
    errors: list[Exception] = []

    def feeder() -> None:
        try:
            for i in range(3000):
                s._feed_and_log(b"line %d data here\r\n" % i)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def reader() -> None:
        try:
            for _ in range(3000):
                s.display_lines()
                s.is_at_ready_prompt()
                s.is_at_trust_prompt()
                s.cursor()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [
        threading.Thread(target=feeder),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent feed/read raised: {errors!r}"


def test_feed_and_log_caches_ready_state() -> None:
    """#106: _feed_and_log must classify ready state under the SAME lock
    acquisition it already uses to feed pyte, so is_at_ready_prompt_cached()
    (the lock-free accessor _sync_idle_flag polls) reflects the screen
    without a separate lock-guarded call on the main thread."""
    s = PtySession(cols=80, rows=24)
    assert s.is_at_ready_prompt_cached() is False  # nothing fed yet

    s._feed_and_log(b"bypass permissions")
    assert s.is_at_ready_prompt_cached() is True
    assert s.is_at_ready_prompt_cached() == s.is_at_ready_prompt()

    s._feed_and_log(b"\r\n(esc to interrupt) working...")
    assert s.is_at_ready_prompt_cached() is False
    assert s.is_at_ready_prompt_cached() == s.is_at_ready_prompt()


def test_feed_and_log_bad_chunk_leaves_cached_ready_unchanged() -> None:
    """A chunk that makes stream.feed() raise must not corrupt the cached
    verdict — it just stays at whatever the last good feed computed, same as
    display_lines() staying at its last good content (see
    test_feed_and_log_never_raises_on_bad_chunk)."""
    s = PtySession(cols=80, rows=24)
    s._feed_and_log(b"bypass permissions")
    assert s.is_at_ready_prompt_cached() is True

    def _boom(data: bytes) -> None:
        raise ValueError("simulated pyte choke")

    s.stream.feed = _boom  # type: ignore[method-assign]
    s._feed_and_log(b"anything")  # must not raise out of _feed_and_log
    assert s.is_at_ready_prompt_cached() is True


def test_resize_is_thread_safe_with_feed() -> None:
    s = PtySession(cols=80, rows=24)
    errors: list[Exception] = []

    def feeder() -> None:
        try:
            for _ in range(2000):
                s._feed_and_log(b"x" * 60 + b"\r\n")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def resizer() -> None:
        try:
            for i in range(500):
                s.resize(60 + (i % 40), 20 + (i % 15))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=feeder), threading.Thread(target=resizer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"resize/feed race raised: {errors!r}"
