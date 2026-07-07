"""Wave C round 2 — real-HTTP e2e gaps that the existing unit suite doesn't
exercise end-to-end: lockout cooldown observed over the wire (not just
`AuthGate` in isolation), multi-line SSE payload integrity through a real
push, and ticket TTL enforced at the 30s boundary through `RemoteHttpServer`
itself (not `AuthGate` alone). See remote-control-plan/WaveC-round2-qa.md.
"""

from __future__ import annotations

import http.client
import json
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

    def _resolve_project(self, project):
        return "default"


def _pump_until(app: QCoreApplication, predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _run_pumped(fn):
    app = QCoreApplication.instance()
    result: dict = {}

    def _do() -> None:
        result["value"] = fn()

    t = threading.Thread(target=_do)
    t.start()
    assert _pump_until(app, lambda: not t.is_alive())
    t.join(timeout=1)
    return result["value"]


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(api, "pulse", lambda orch, project: {"working": 1, "total": 2})
    monkeypatch.setattr(
        api, "projects", lambda project, mode: {"projects": [], "mode": mode, "open_tabs": []}
    )
    monkeypatch.setattr(api, "lead_say", lambda orch, text, project: {"ok": True})
    config = RemoteConfig(
        bind_port=0, secret_path="sek", token="tok", mode="control", lockout_after_fails=3
    )
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


class TestLockoutOverRealHttp:
    def test_repeated_bad_bearer_locks_out_then_correct_token_also_404s(self, server):
        for _ in range(3):
            status, _ = _get_status(
                _url(server, "/sek/api/pulse"), {"Authorization": "Bearer wrong"}
            )
            assert status == 404
        # Locked out now: even the *correct* bearer must 404 during cooldown —
        # zero-surface design means lockout must look identical to bad auth.
        status, _ = _get_status(_url(server, "/sek/api/pulse"), {"Authorization": "Bearer tok"})
        assert status == 404
        assert server.auth.is_locked_out() is True

    def test_lockout_clears_after_backoff_and_correct_token_works_again(self, server, monkeypatch):
        for _ in range(3):
            _get_status(_url(server, "/sek/api/pulse"), {"Authorization": "Bearer wrong"})
        assert server.auth.is_locked_out() is True
        future = time.time() + 3600
        monkeypatch.setattr(time, "time", lambda: future)
        assert server.auth.is_locked_out() is False
        status, body = _run_pumped(
            lambda: _get_status(_url(server, "/sek/api/pulse"), {"Authorization": "Bearer tok"})
        )
        assert status == 200
        assert json.loads(body) == {"working": 1, "total": 2}


class TestTicketTtlOverRealHttp:
    def test_ticket_older_than_30s_is_rejected_by_the_server(self, server, monkeypatch):
        def _issue() -> str:
            req = urllib.request.Request(
                _url(server, "/sek/api/sse-ticket"),
                data=b"",
                headers={"Authorization": "Bearer tok"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())["ticket"]

        ticket = _run_pumped(_issue)

        future = time.time() + 31
        monkeypatch.setattr(time, "time", lambda: future)

        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            conn.request("GET", f"/sek/api/lead?ticket={ticket}")
            resp = conn.getresponse()
            assert resp.status == 404
        finally:
            conn.close()

    def test_ticket_within_30s_still_works(self, server, monkeypatch):
        def _issue() -> str:
            req = urllib.request.Request(
                _url(server, "/sek/api/sse-ticket"),
                data=b"",
                headers={"Authorization": "Bearer tok"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())["ticket"]

        ticket = _run_pumped(_issue)

        future = time.time() + 20
        monkeypatch.setattr(time, "time", lambda: future)

        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            conn.request("GET", f"/sek/api/lead?ticket={ticket}")
            resp = conn.getresponse()
            assert resp.status == 200
        finally:
            conn.close()


class TestMultiLineSseIntegrityOverRealHttp:
    def test_multiline_lead_text_arrives_as_one_intact_event(self, server):
        def _issue() -> str:
            req = urllib.request.Request(
                _url(server, "/sek/api/sse-ticket"),
                data=b"",
                headers={"Authorization": "Bearer tok"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())["ticket"]

        ticket = _run_pumped(_issue)

        multiline = "line one\nline two\nevent: fake\ndata: injected"
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            conn.request("GET", f"/sek/api/lead?ticket={ticket}")
            resp = conn.getresponse()
            assert resp.status == 200

            server.broadcaster.push("lead", multiline, "default")

            expected = f"event: lead\ndata: {json.dumps({'text': multiline})}\n\n".encode()
            chunk = resp.read(len(expected))
            assert chunk == expected
            # Exactly one SSE frame (one blank-line-terminated block) reached
            # the wire — the embedded "event:"/"data:" substrings inside the
            # JSON-encoded payload never became real SSE line framing (H-C).
            lines = chunk.decode().split("\n")
            assert lines[0] == "event: lead"
            assert lines[1].startswith("data: ")
            assert lines[2:] == ["", ""]
        finally:
            conn.close()
