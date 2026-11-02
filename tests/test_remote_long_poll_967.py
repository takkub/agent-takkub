"""#967: a Cloudflare quick tunnel holds an SSE body back until the stream
ends, so a phone behind one saw no live event and only caught up through the
30 s "no reply" history reload — users kept pressing Refresh. The stream now
opens with a `hello` frame, and `/api/lead/poll` delivers the same events as
plain request/response for a client that never sees it."""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from agent_takkub.remote import http_server
from agent_takkub.remote.config import RemoteConfig
from tests.test_remote_http_server import _FakeOrch, _run_pumped, _url


def test_wait_events_returns_only_newer_events_for_the_project():
    b = http_server.SSEBroadcaster()
    start = b.seq
    b.push("lead", "for a", "proj-a")
    b.push("lead", "for b", "proj-b")
    seq, events = b.wait_events("proj-a", start, 0.1)
    assert seq == start + 2
    assert [(e, json.loads(d)["text"]) for e, d in events] == [("lead", "for a")]
    assert b.wait_events("proj-a", seq, 0.05) == (seq, [])


def test_wait_events_wakes_as_soon_as_an_event_is_pushed():
    b = http_server.SSEBroadcaster()
    start = b.seq
    threading.Timer(0.2, lambda: b.push("idle", "", "p")).start()
    t0 = time.monotonic()
    _seq, events = b.wait_events("p", start, 5.0)
    assert [e for e, _ in events] == ["idle"]
    assert time.monotonic() - t0 < 2.0


def test_a_cursor_the_log_no_longer_covers_asks_for_a_history_reload(monkeypatch):
    monkeypatch.setattr(http_server, "_POLL_LOG_MAX", 3)
    b = http_server.SSEBroadcaster()
    for i in range(6):
        b.push("lead", str(i), "p")
    _seq, events = b.wait_events("p", 1, 0.05)
    assert [e for e, _ in events] == ["resync"]
    # a cursor from a previous server process (ahead of this one) too
    assert [e for e, _ in b.wait_events("p", 999, 0.05)[1]] == ["resync"]


def test_close_all_releases_a_held_poll():
    b = http_server.SSEBroadcaster()
    threading.Timer(0.2, b.close_all).start()
    t0 = time.monotonic()
    b.wait_events("p", b.seq, 10.0)
    assert time.monotonic() - t0 < 2.0


@pytest.fixture
def server():
    srv = http_server.start_server(
        RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control"), _FakeOrch()
    )
    yield srv
    srv.stop()


def _poll(server, since):
    def _do():
        req = urllib.request.Request(
            _url(server, "/sek/api/lead/poll"),
            data=json.dumps({"project": None, "since": since}).encode("utf-8"),
            headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            assert resp.headers["Cache-Control"].startswith("private, no-store")
            return json.loads(resp.read())

    return _run_pumped(_do)


def test_poll_endpoint_gives_a_cursor_then_delivers_pushed_events(server):
    first = _poll(server, -1)
    assert first["events"] == []
    threading.Timer(0.5, lambda: server.broadcaster.push("lead", "live", "default")).start()
    got = _poll(server, first["seq"])
    assert [(e["event"], json.loads(e["data"])["text"]) for e in got["events"]] == [
        ("lead", "live")
    ]
    assert got["seq"] == first["seq"] + 1


def test_poll_requires_auth(server):
    req = urllib.request.Request(
        _url(server, "/sek/api/lead/poll"),
        data=b'{"since": 0}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)
    assert err.value.code in (401, 403, 404)
