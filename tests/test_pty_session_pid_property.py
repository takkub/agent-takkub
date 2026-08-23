"""#364 lever 6 — `PtySession.pid`: the public accessor `ram_report`'s
callers need for the previously-private `_pid`."""

from __future__ import annotations

from types import SimpleNamespace

from agent_takkub.pty_session import PtySession


def test_pid_property_exposes_the_root_process_id() -> None:
    fake = SimpleNamespace(_pid=4242)
    assert PtySession.pid.fget(fake) == 4242


def test_pid_property_is_none_before_spawn() -> None:
    fake = SimpleNamespace(_pid=None)
    assert PtySession.pid.fget(fake) is None


def test_pid_property_is_none_when_never_set() -> None:
    fake = SimpleNamespace()
    assert PtySession.pid.fget(fake) is None
