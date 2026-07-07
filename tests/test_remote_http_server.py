"""Tests for `agent_takkub.remote.http_server` (§6.2, X-check 3.1/4.1/B3):

* HTTP runs off the Qt main thread — a handler thread never has to reach
  Orchestrator directly, only through the signal bridge.
* secret-path / bearer-token gating always answers with a bare 404.
* the SSE ticket + broadcast path (§6.3/B3: bounded buffer, drop-oldest).
* static-file traversal is rejected.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.remote import api, http_server
from agent_takkub.remote.config import RemoteConfig


class _FakeOrch:
    _lead_token = "lead-tok"


def _pump_until(app: QCoreApplication, predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(api, "pulse", lambda orch, project: {"working": 1, "total": 2})
    monkeypatch.setattr(
        api, "projects", lambda project: {"projects": [], "active": None, "open_tabs": []}
    )
    monkeypatch.setattr(api, "lead_say", lambda orch, text, project: {"ok": True})

    config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control")
    srv = http_server.start_server(config, _FakeOrch())
    yield srv
    srv.stop()


def _url(srv, path: str) -> str:
    return f"http://127.0.0.1:{srv.port}{path}"


def _get_status(url: str, headers: dict | None = None) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers or {}), timeout=5
        ) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestSecretPathAndAuth:
    def test_wrong_secret_path_is_404(self, server):
        status, _ = _get_status(_url(server, "/wrong/api/pulse"))
        assert status == 404

    def test_root_without_secret_path_is_404(self, server):
        status, _ = _get_status(_url(server, "/"))
        assert status == 404

    def test_correct_secret_path_but_no_bearer_is_404(self, server):
        status, _ = _get_status(_url(server, "/sek/api/pulse"))
        assert status == 404

    def test_correct_secret_path_wrong_bearer_is_404(self, server):
        status, _ = _get_status(_url(server, "/sek/api/pulse"), {"Authorization": "Bearer wrong"})
        assert status == 404

    def test_handler_thread_is_not_the_qt_main_thread(self, monkeypatch, server):
        from agent_takkub.remote.auth import AuthGate

        seen: dict[str, threading.Thread] = {}
        orig_touch = AuthGate.touch

        def _touch(self):
            seen["thread"] = threading.current_thread()
            return orig_touch(self)

        monkeypatch.setattr(AuthGate, "touch", _touch)
        _get_status(_url(server, "/sek/api/pulse"))  # no bearer -> 404, but touch() ran first
        assert seen["thread"] is not threading.main_thread()


class TestMarshaledRoutes:
    def test_pulse_with_bearer_returns_bridge_result(self, server):
        app = QCoreApplication.instance()
        outcome: dict = {}

        def _do() -> None:
            outcome["status"], body = _get_status(
                _url(server, "/sek/api/pulse"), {"Authorization": "Bearer tok"}
            )
            outcome["body"] = json.loads(body)

        t = threading.Thread(target=_do)
        t.start()
        assert _pump_until(app, lambda: not t.is_alive())
        t.join(timeout=1)
        assert outcome["status"] == 200
        assert outcome["body"] == {"working": 1, "total": 2}

    def test_projects_with_bearer_returns_bridge_result(self, server):
        app = QCoreApplication.instance()
        outcome: dict = {}

        def _do() -> None:
            outcome["status"], body = _get_status(
                _url(server, "/sek/api/projects"), {"Authorization": "Bearer tok"}
            )
            outcome["body"] = json.loads(body)

        t = threading.Thread(target=_do)
        t.start()
        assert _pump_until(app, lambda: not t.is_alive())
        t.join(timeout=1)
        assert outcome["status"] == 200
        assert outcome["body"] == {"projects": [], "active": None, "open_tabs": []}

    def test_lead_say_in_control_mode_returns_ok(self, server):
        app = QCoreApplication.instance()
        outcome: dict = {}

        def _do() -> None:
            req = urllib.request.Request(
                _url(server, "/sek/api/lead/say"),
                data=json.dumps({"text": "hello"}).encode("utf-8"),
                headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    outcome["status"] = resp.status
                    outcome["body"] = json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                outcome["status"] = exc.code
                outcome["body"] = json.loads(exc.read())

        t = threading.Thread(target=_do)
        t.start()
        assert _pump_until(app, lambda: not t.is_alive())
        t.join(timeout=1)
        assert outcome["status"] == 200
        assert outcome["body"] == {"ok": True}

    def test_lead_say_in_view_mode_is_forbidden_without_marshaling(self, monkeypatch):
        monkeypatch.setattr(api, "lead_say", lambda orch, text, project: {"ok": True})
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="view")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            req = urllib.request.Request(
                _url(srv, "/sek/api/lead/say"),
                data=json.dumps({"text": "hi"}).encode("utf-8"),
                headers={"Authorization": "Bearer tok"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                pytest.fail("expected HTTPError")
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
        finally:
            srv.stop()


class TestSSEBroadcaster:
    def test_drops_oldest_when_full(self):
        broadcaster = http_server.SSEBroadcaster()
        q = broadcaster.register()
        for i in range(http_server._SSE_QUEUE_MAXSIZE + 50):
            broadcaster.push("lead", str(i))
        assert q.qsize() <= http_server._SSE_QUEUE_MAXSIZE
        items = []
        while not q.empty():
            items.append(q.get_nowait()[1])
        assert items[-1] == str(http_server._SSE_QUEUE_MAXSIZE + 49)

    def test_rejects_beyond_max_clients(self):
        broadcaster = http_server.SSEBroadcaster()
        clients = [broadcaster.register() for _ in range(http_server._MAX_SSE_CLIENTS)]
        assert all(c is not None for c in clients)
        assert broadcaster.register() is None


class TestSSEEndToEnd:
    def test_ticket_flow_delivers_a_pushed_event(self, server):
        req = urllib.request.Request(
            _url(server, "/sek/api/sse-ticket"),
            data=b"",
            headers={"Authorization": "Bearer tok"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            ticket = json.loads(resp.read())["ticket"]

        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            conn.request("GET", f"/sek/api/lead?ticket={ticket}")
            resp = conn.getresponse()
            assert resp.status == 200

            server.broadcaster.push("lead", "hello mobile")

            expected = b"event: lead\ndata: hello mobile\n\n"
            chunk = resp.read(len(expected))
            assert chunk == expected
        finally:
            conn.close()

    def test_ticket_is_single_use(self, server):
        req = urllib.request.Request(
            _url(server, "/sek/api/sse-ticket"),
            data=b"",
            headers={"Authorization": "Bearer tok"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            ticket = json.loads(resp.read())["ticket"]

        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            conn.request("GET", f"/sek/api/lead?ticket={ticket}")
            first = conn.getresponse()
            first.read(0)
        finally:
            conn.close()
        assert first.status == 200

        conn2 = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            conn2.request("GET", f"/sek/api/lead?ticket={ticket}")
            second = conn2.getresponse()
            assert second.status == 404
        finally:
            conn2.close()


class TestStaticFileTraversal:
    def test_path_traversal_outside_static_root_rejected(self, server):
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
            sock.sendall(b"GET /sek/../config.py HTTP/1.0\r\n\r\n")
            resp = sock.recv(4096)
        assert resp.startswith(b"HTTP/1.0 404")

    def test_missing_static_file_is_404(self, server):
        status, _ = _get_status(_url(server, "/sek/does-not-exist.js"))
        assert status == 404
