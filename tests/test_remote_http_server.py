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


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(api, "pulse", lambda orch, project: {"working": 1, "total": 2})
    monkeypatch.setattr(
        api,
        "projects",
        lambda project, mode: {"projects": [], "mode": mode, "open_tabs": []},
    )
    monkeypatch.setattr(api, "lead_say", lambda orch, text, project: {"ok": True})
    monkeypatch.setattr(api, "open_project", lambda orch, project: {"ok": True, "project": project})
    monkeypatch.setattr(
        api, "close_project", lambda orch, project: {"ok": True, "project": project}
    )

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


def _run_pumped(fn):
    """Any route that reaches the `_Bridge` (pulse/projects/sse-ticket) needs
    the Qt main thread pumped to deliver the queued signal — run `fn` on a
    background thread while the test thread pumps `QCoreApplication`."""
    app = QCoreApplication.instance()
    result: dict = {}

    def _do() -> None:
        result["value"] = fn()

    t = threading.Thread(target=_do)
    t.start()
    assert _pump_until(app, lambda: not t.is_alive())
    t.join(timeout=1)
    return result["value"]


def _issue_ticket(server, project: str | None = None) -> str:
    def _do() -> str:
        req = urllib.request.Request(
            _url(server, "/sek/api/sse-ticket"),
            data=json.dumps({"project": project}).encode("utf-8") if project else b"",
            headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())["ticket"]

    return _run_pumped(_do)


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
        # Successful bearer auth (not just a secret-path match, see M-6 below)
        # still must never run on the Qt main thread.
        _run_pumped(
            lambda: _get_status(_url(server, "/sek/api/pulse"), {"Authorization": "Bearer tok"})
        )
        assert seen["thread"] is not threading.main_thread()

    def test_wrong_bearer_does_not_touch_idle_clock(self, monkeypatch, server):
        """M-6: a request that only knows the secret path (wrong/no bearer)
        must never reset the idle-expire clock — only a *successful* auth
        counts as activity."""
        from agent_takkub.remote.auth import AuthGate

        touched = []
        monkeypatch.setattr(AuthGate, "touch", lambda self: touched.append(True))
        _get_status(_url(server, "/sek/api/pulse"))  # no bearer -> 404
        _get_status(_url(server, "/sek/api/pulse"), {"Authorization": "Bearer wrong"})
        assert touched == []

    def test_correct_bearer_touches_idle_clock(self, monkeypatch, server):
        from agent_takkub.remote.auth import AuthGate

        touched = []
        monkeypatch.setattr(AuthGate, "touch", lambda self: touched.append(True))
        _run_pumped(
            lambda: _get_status(_url(server, "/sek/api/pulse"), {"Authorization": "Bearer tok"})
        )
        assert touched == [True]


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
        assert outcome["body"] == {"projects": [], "mode": "control", "open_tabs": []}

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

    def test_open_in_control_mode_returns_ok(self, server):
        app = QCoreApplication.instance()
        outcome: dict = {}

        def _do() -> None:
            req = urllib.request.Request(
                _url(server, "/sek/api/open"),
                data=json.dumps({"project": "proj-a"}).encode("utf-8"),
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
        assert outcome["body"] == {"ok": True, "project": "proj-a"}

    def test_open_in_view_mode_is_forbidden_without_marshaling(self, monkeypatch):
        monkeypatch.setattr(
            api, "open_project", lambda orch, project: {"ok": True, "project": project}
        )
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="view")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            req = urllib.request.Request(
                _url(srv, "/sek/api/open"),
                data=json.dumps({"project": "proj-a"}).encode("utf-8"),
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

    def test_open_rejects_unknown_project_via_bridge_error(self, monkeypatch):
        """The route must surface `api.open_project`'s `RemoteApiError`
        status/msg verbatim, not flatten it to a generic 500 — an unlisted
        project name is a client error (400), not a server fault."""

        def _fake_open(orch, project):
            raise api.RemoteApiError(400, "unknown project")

        monkeypatch.setattr(api, "open_project", _fake_open)
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control")
        srv = http_server.start_server(config, _FakeOrch())

        def _do() -> tuple[int, dict]:
            req = urllib.request.Request(
                _url(srv, "/sek/api/open"),
                data=json.dumps({"project": "ghost"}).encode("utf-8"),
                headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        try:
            status, body = _run_pumped(_do)
        finally:
            srv.stop()
        assert status == 400
        assert body == {"ok": False, "msg": "unknown project"}

    def test_close_in_control_mode_returns_ok(self, server):
        app = QCoreApplication.instance()
        outcome: dict = {}

        def _do() -> None:
            req = urllib.request.Request(
                _url(server, "/sek/api/close"),
                data=json.dumps({"project": "proj-a"}).encode("utf-8"),
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
        assert outcome["body"] == {"ok": True, "project": "proj-a"}

    def test_close_in_view_mode_is_forbidden_without_marshaling(self, monkeypatch):
        monkeypatch.setattr(
            api, "close_project", lambda orch, project: {"ok": True, "project": project}
        )
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="view")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            req = urllib.request.Request(
                _url(srv, "/sek/api/close"),
                data=json.dumps({"project": "proj-a"}).encode("utf-8"),
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

    def test_close_rejects_unknown_project_via_bridge_error(self, monkeypatch):
        """The route must surface `api.close_project`'s `RemoteApiError`
        status/msg verbatim, not flatten it to a generic 500."""

        def _fake_close(orch, project):
            raise api.RemoteApiError(400, "unknown project")

        monkeypatch.setattr(api, "close_project", _fake_close)
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control")
        srv = http_server.start_server(config, _FakeOrch())

        def _do() -> tuple[int, dict]:
            req = urllib.request.Request(
                _url(srv, "/sek/api/close"),
                data=json.dumps({"project": "ghost"}).encode("utf-8"),
                headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        try:
            status, body = _run_pumped(_do)
        finally:
            srv.stop()
        assert status == 400
        assert body == {"ok": False, "msg": "unknown project"}


class TestProjectScoping:
    """Project picker: a client can scope `/api/lead` (SSE), `/api/pulse`,
    and `/api/lead/say` to any project it can see in `/api/projects` —
    never to one it can't. Missing/unknown/forged names always fall back
    to the orchestrator's active project, never error and never leak."""

    def test_bridge_resolves_requested_project_when_open(self, monkeypatch):
        monkeypatch.setattr(http_server._config, "get_open_tabs", lambda: ["proj-a", "proj-b"])
        bridge = http_server._Bridge(_FakeOrch())
        assert bridge._resolve_scoped_project("proj-b") == "proj-b"

    def test_bridge_falls_back_to_active_when_project_not_open(self, monkeypatch):
        monkeypatch.setattr(http_server._config, "get_open_tabs", lambda: ["proj-a"])
        bridge = http_server._Bridge(_FakeOrch())
        assert bridge._resolve_scoped_project("proj-forged") == "default"

    def test_bridge_falls_back_to_active_when_project_missing(self, monkeypatch):
        monkeypatch.setattr(http_server._config, "get_open_tabs", lambda: ["proj-a"])
        bridge = http_server._Bridge(_FakeOrch())
        assert bridge._resolve_scoped_project(None) == "default"

    def test_sse_ticket_scopes_to_the_requested_open_project(self, monkeypatch):
        monkeypatch.setattr(http_server._config, "get_open_tabs", lambda: ["proj-a", "proj-b"])
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            ticket = _issue_ticket(srv, project="proj-b")
            conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            try:
                conn.request("GET", f"/sek/api/lead?ticket={ticket}")
                resp = conn.getresponse()
                assert resp.status == 200

                srv.broadcaster.push("lead", "hi proj-b", "proj-b")
                expected = f"event: lead\ndata: {json.dumps({'text': 'hi proj-b'})}\n\n".encode()
                assert resp.read(len(expected)) == expected
            finally:
                conn.close()
        finally:
            srv.stop()

    def test_sse_ticket_ignores_a_project_name_that_is_not_open(self, monkeypatch):
        monkeypatch.setattr(http_server._config, "get_open_tabs", lambda: ["proj-a"])
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            ticket = _issue_ticket(srv, project="proj-forged")
            conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            try:
                conn.request("GET", f"/sek/api/lead?ticket={ticket}")
                resp = conn.getresponse()
                assert resp.status == 200

                # never scoped to the forged name — falls back to
                # `_FakeOrch._resolve_project(None)` == "default".
                srv.broadcaster.push("lead", "should not arrive", "proj-forged")
                srv.broadcaster.push("lead", "hi default", "default")
                expected = f"event: lead\ndata: {json.dumps({'text': 'hi default'})}\n\n".encode()
                assert resp.read(len(expected)) == expected
            finally:
                conn.close()
        finally:
            srv.stop()

    def test_pulse_forwards_validated_project_to_api(self, monkeypatch):
        monkeypatch.setattr(http_server._config, "get_open_tabs", lambda: ["proj-a", "proj-b"])
        seen: dict = {}

        def _fake_pulse(orch, project):
            seen["project"] = project
            return {"working": 0, "total": 0}

        monkeypatch.setattr(api, "pulse", _fake_pulse)
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            _run_pumped(
                lambda: _get_status(
                    _url(srv, "/sek/api/pulse?project=proj-b"), {"Authorization": "Bearer tok"}
                )
            )
        finally:
            srv.stop()
        assert seen["project"] == "proj-b"

    def test_pulse_falls_back_when_requested_project_is_not_open(self, monkeypatch):
        monkeypatch.setattr(http_server._config, "get_open_tabs", lambda: ["proj-a"])
        seen: dict = {}

        def _fake_pulse(orch, project):
            seen["project"] = project
            return {"working": 0, "total": 0}

        monkeypatch.setattr(api, "pulse", _fake_pulse)
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            _run_pumped(
                lambda: _get_status(
                    _url(srv, "/sek/api/pulse?project=proj-forged"),
                    {"Authorization": "Bearer tok"},
                )
            )
        finally:
            srv.stop()
        assert seen["project"] == "default"

    def test_lead_say_forwards_validated_project_to_api(self, monkeypatch):
        monkeypatch.setattr(http_server._config, "get_open_tabs", lambda: ["proj-a", "proj-b"])
        seen: dict = {}

        def _fake_lead_say(orch, text, project):
            seen["project"] = project
            return {"ok": True}

        monkeypatch.setattr(api, "lead_say", _fake_lead_say)
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control")
        srv = http_server.start_server(config, _FakeOrch())

        def _do() -> int:
            req = urllib.request.Request(
                _url(srv, "/sek/api/lead/say"),
                data=json.dumps({"text": "hi", "project": "proj-b"}).encode("utf-8"),
                headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status

        try:
            status = _run_pumped(_do)
        finally:
            srv.stop()
        assert status == 200
        assert seen["project"] == "proj-b"


class TestLeadHistoryRoute:
    """Gemini CRITICAL/HIGH: `/api/lead/history` — read-only, works in view
    mode, scoped to the requested (validated) project like pulse/projects."""

    def test_returns_bridge_result_and_forwards_limit(self, monkeypatch):
        seen: dict = {}

        def _fake_history(orch, project_ns, limit):
            seen["project_ns"] = project_ns
            seen["limit"] = limit
            return {"project": project_ns, "messages": [{"text": "hi"}]}

        monkeypatch.setattr(api, "lead_history", _fake_history)
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            status, body = _run_pumped(
                lambda: _get_status(
                    _url(srv, "/sek/api/lead/history?limit=50"),
                    {"Authorization": "Bearer tok"},
                )
            )
        finally:
            srv.stop()
        assert status == 200
        assert json.loads(body) == {"project": "default", "messages": [{"text": "hi"}]}
        assert seen["project_ns"] == "default"
        assert seen["limit"] == "50"

    def test_forwards_validated_project_to_api(self, monkeypatch):
        monkeypatch.setattr(http_server._config, "get_open_tabs", lambda: ["proj-a", "proj-b"])
        seen: dict = {}

        def _fake_history(orch, project_ns, limit):
            seen["project_ns"] = project_ns
            return {"project": project_ns, "messages": []}

        monkeypatch.setattr(api, "lead_history", _fake_history)
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            _run_pumped(
                lambda: _get_status(
                    _url(srv, "/sek/api/lead/history?project=proj-b"),
                    {"Authorization": "Bearer tok"},
                )
            )
        finally:
            srv.stop()
        assert seen["project_ns"] == "proj-b"

    def test_works_in_view_mode(self, monkeypatch):
        monkeypatch.setattr(
            api,
            "lead_history",
            lambda orch, project_ns, limit: {"project": project_ns, "messages": []},
        )
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="view")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            status, _body = _run_pumped(
                lambda: _get_status(
                    _url(srv, "/sek/api/lead/history"), {"Authorization": "Bearer tok"}
                )
            )
        finally:
            srv.stop()
        assert status == 200

    def test_requires_password_when_configured(self, monkeypatch):
        from agent_takkub.remote.auth import hash_password

        monkeypatch.setattr(api, "lead_history", lambda orch, project_ns, limit: {"messages": []})
        config = RemoteConfig(
            bind_port=0,
            secret_path="sek",
            token="tok",
            mode="view",
            password_hash=hash_password("hunter2"),
        )
        srv = http_server.start_server(config, _FakeOrch())
        try:
            status, body = _get_status(
                _url(srv, "/sek/api/lead/history"), {"Authorization": "Bearer tok"}
            )
        finally:
            srv.stop()
        assert status == 403
        assert json.loads(body)["msg"] == "password_required"


class TestPasswordGate:
    """Third auth factor (addendum 2): a cockpit-set password gates every
    authenticated route besides `/api/verify-password` itself, and is never
    embedded in the pairing URL/QR — see `auth.py`'s `check_password`/
    `password_ok`."""

    @pytest.fixture
    def pw_server(self, monkeypatch):
        from agent_takkub.remote.auth import hash_password

        monkeypatch.setattr(api, "pulse", lambda orch, project: {"working": 1, "total": 2})
        config = RemoteConfig(
            bind_port=0,
            secret_path="sek",
            token="tok",
            mode="control",
            password_hash=hash_password("hunter2"),
        )
        srv = http_server.start_server(config, _FakeOrch())
        yield srv
        srv.stop()

    def _verify_password(self, server, password: str) -> tuple[int, dict]:
        req = urllib.request.Request(
            _url(server, "/sek/api/verify-password"),
            data=json.dumps({"password": password}).encode("utf-8"),
            headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_pulse_blocked_before_password_verified(self, pw_server):
        status, body = _get_status(
            _url(pw_server, "/sek/api/pulse"), {"Authorization": "Bearer tok"}
        )
        assert status == 403
        assert json.loads(body)["msg"] == "password_required"

    def test_wrong_password_is_401_and_stays_blocked(self, pw_server):
        # verify-password itself never touches the bridge (no _run_pumped
        # needed) — it's a plain thread-safe AuthGate check.
        status, body = self._verify_password(pw_server, "wrong")
        assert status == 401
        assert body["ok"] is False
        status2, _ = _get_status(_url(pw_server, "/sek/api/pulse"), {"Authorization": "Bearer tok"})
        assert status2 == 403

    def test_correct_password_unlocks_pulse_with_session_header(self, pw_server):
        status, body = self._verify_password(pw_server, "hunter2")
        assert status == 200
        assert body["ok"] is True
        session = body["session"]
        assert session

        status2, pulse_body = _run_pumped(
            lambda: _get_status(
                _url(pw_server, "/sek/api/pulse"),
                {"Authorization": "Bearer tok", "X-Session": session},
            )
        )
        assert status2 == 200
        assert json.loads(pulse_body) == {"working": 1, "total": 2}

    def test_bearer_alone_without_session_stays_blocked_after_another_client_verified(
        self, pw_server
    ):
        """H1 fix: this is the exact leaked-link repro from the audit — a
        holder of just the bearer token must NOT be let in merely because
        some other client already verified the password this server run."""
        status, body = self._verify_password(pw_server, "hunter2")
        assert status == 200
        assert body["session"]

        # A different client — same bearer token, no session of its own.
        status2, body2 = _get_status(
            _url(pw_server, "/sek/api/pulse"), {"Authorization": "Bearer tok"}
        )
        assert status2 == 403
        assert json.loads(body2)["msg"] == "password_required"

    def test_forged_session_header_is_rejected(self, pw_server):
        self._verify_password(pw_server, "hunter2")
        status, body = _get_status(
            _url(pw_server, "/sek/api/pulse"),
            {"Authorization": "Bearer tok", "X-Session": "totally-made-up"},
        )
        assert status == 403
        assert json.loads(body)["msg"] == "password_required"

    def test_sse_ticket_requires_session_when_password_configured(self, pw_server):
        req = urllib.request.Request(
            _url(pw_server, "/sek/api/sse-ticket"),
            data=b"",
            headers={"Authorization": "Bearer tok"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            pytest.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
            assert json.loads(exc.read())["msg"] == "password_required"

    def test_sse_ticket_issued_once_session_present(self, pw_server):
        _, body = self._verify_password(pw_server, "hunter2")
        session = body["session"]

        def _do():
            req = urllib.request.Request(
                _url(pw_server, "/sek/api/sse-ticket"),
                data=b"",
                headers={"Authorization": "Bearer tok", "X-Session": session},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())

        status, body2 = _run_pumped(_do)
        assert status == 200
        assert body2["ticket"]

    def test_verify_password_requires_bearer_too(self, pw_server):
        req = urllib.request.Request(
            _url(pw_server, "/sek/api/verify-password"),
            data=json.dumps({"password": "hunter2"}).encode("utf-8"),
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            pytest.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404  # no/wrong bearer -> zero-surface 404 (§7.5)

    def test_no_password_configured_skips_the_gate(self, server):
        """`server` fixture (no password_hash) — pulse works with bearer
        alone, exactly as before this feature existed."""
        status, _ = _run_pumped(
            lambda: _get_status(_url(server, "/sek/api/pulse"), {"Authorization": "Bearer tok"})
        )
        assert status == 200

    def test_verify_password_with_no_password_configured_is_a_no_op_success(self, server):
        status, body = self._verify_password(server, "anything")
        assert status == 200
        assert body["ok"] is True


class TestSSEBroadcaster:
    def test_drops_oldest_when_full(self):
        broadcaster = http_server.SSEBroadcaster()
        q = broadcaster.register("proj")
        for i in range(http_server._SSE_QUEUE_MAXSIZE + 50):
            broadcaster.push("lead", str(i))
        assert q.qsize() <= http_server._SSE_QUEUE_MAXSIZE
        items = []
        while not q.empty():
            items.append(q.get_nowait()[1])
        assert items[-1] == json.dumps({"text": str(http_server._SSE_QUEUE_MAXSIZE + 49)})

    def test_evicts_oldest_beyond_max_clients(self):
        # At the cap the broadcaster admits the newcomer by evicting the oldest
        # slot (cloudflared keeps dead origin sockets around after the phone
        # reloads, so a full table is stale reconnects, not real viewers). The
        # newcomer must get a live queue — never a 503-triggering None — and
        # the evicted client's handler must be woken with the close sentinel.
        broadcaster = http_server.SSEBroadcaster()
        clients = [broadcaster.register("proj") for _ in range(http_server._MAX_SSE_CLIENTS)]
        assert all(c is not None for c in clients)
        oldest = clients[0]
        newcomer = broadcaster.register("proj")
        assert newcomer is not None
        # oldest was woken with the (None, None) close sentinel so its SSE
        # handler thread breaks out of q.get() and unregisters itself.
        assert oldest.get_nowait() == (None, None)
        # table stays at the cap (oldest dropped, newcomer added).
        assert len(broadcaster._clients) == http_server._MAX_SSE_CLIENTS

    def test_evicted_client_is_woken_even_when_its_queue_is_full(self):
        # codex MED: `put_nowait((None, None))` used to be dropped silently
        # if the evicted client's queue was already full, leaving its
        # handler thread blocked in `q.get()` until the next 15s keepalive
        # instead of exiting promptly.
        broadcaster = http_server.SSEBroadcaster()
        clients = [broadcaster.register("proj") for _ in range(http_server._MAX_SSE_CLIENTS)]
        oldest = clients[0]
        for i in range(http_server._SSE_QUEUE_MAXSIZE):
            oldest.put_nowait(("lead", str(i)))
        assert oldest.full()

        newcomer = broadcaster.register("proj")
        assert newcomer is not None

        saw_sentinel = False
        while not oldest.empty():
            if oldest.get_nowait() == (None, None):
                saw_sentinel = True
                break
        assert saw_sentinel

    def test_close_all_wakes_a_client_even_when_its_queue_is_full(self):
        broadcaster = http_server.SSEBroadcaster()
        q = broadcaster.register("proj")
        for i in range(http_server._SSE_QUEUE_MAXSIZE):
            q.put_nowait(("lead", str(i)))
        assert q.full()

        broadcaster.close_all()

        saw_sentinel = False
        while not q.empty():
            if q.get_nowait() == (None, None):
                saw_sentinel = True
                break
        assert saw_sentinel

    def test_cross_project_events_are_filtered(self):
        """H-A: a client registered for one project's ticket must never see
        a `done`/`lead` event stamped with a different project."""
        broadcaster = http_server.SSEBroadcaster()
        q_a = broadcaster.register("proj-a")
        q_b = broadcaster.register("proj-b")

        broadcaster.push("done", "backend: shipped it", "proj-a")

        assert q_a.get_nowait() == ("done", json.dumps({"text": "backend: shipped it"}))
        assert q_b.empty()

    def test_push_without_project_ns_reaches_every_client(self):
        broadcaster = http_server.SSEBroadcaster()
        q_a = broadcaster.register("proj-a")
        q_b = broadcaster.register("proj-b")

        broadcaster.push("lead", "hello")

        assert not q_a.empty()
        assert not q_b.empty()

    def test_unknown_event_name_is_dropped(self):
        """Defense-in-depth allowlist (H-C): only `done`/`lead` are ever
        forwarded to a client, regardless of what a caller passes in."""
        broadcaster = http_server.SSEBroadcaster()
        q = broadcaster.register("proj")
        broadcaster.push("evil\nevent: fake", "x")
        assert q.empty()

    def test_data_with_embedded_newline_is_json_encoded_not_broken_in_two(self):
        """H-C: a payload containing a raw newline must never produce a
        second `data:`/`event:` line — it comes back as one JSON string."""
        broadcaster = http_server.SSEBroadcaster()
        q = broadcaster.register("proj")
        broadcaster.push("lead", "line one\nline two\nevent: fake\ndata: injected")
        event, payload = q.get_nowait()
        assert event == "lead"
        assert "\n" not in payload
        decoded = json.loads(payload)
        assert decoded == {"text": "line one\nline two\nevent: fake\ndata: injected"}

    def test_close_all_wakes_every_registered_client(self):
        broadcaster = http_server.SSEBroadcaster()
        q = broadcaster.register("proj")
        broadcaster.close_all()
        assert q.get_nowait() == (None, None)


class TestSSEEndToEnd:
    def test_ticket_flow_delivers_a_pushed_event(self, server):
        ticket = _issue_ticket(server)

        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            conn.request("GET", f"/sek/api/lead?ticket={ticket}")
            resp = conn.getresponse()
            assert resp.status == 200

            server.broadcaster.push("lead", "hello mobile", "default")

            expected = f"event: lead\ndata: {json.dumps({'text': 'hello mobile'})}\n\n".encode()
            chunk = resp.read(len(expected))
            assert chunk == expected
        finally:
            conn.close()

    def test_ticket_is_single_use(self, server):
        ticket = _issue_ticket(server)

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


class TestSSEClosesOnStop:
    def test_sse_connection_closes_promptly_on_server_stop(self, monkeypatch):
        """M-4: stop() must wake an open `/api/lead` handler thread instead
        of leaving it blocked for up to `_SSE_KEEPALIVE_SEC` — the client
        must observe the connection close well before that."""
        monkeypatch.setattr(api, "pulse", lambda orch, project: {"working": 0, "total": 0})
        monkeypatch.setattr(
            api,
            "projects",
            lambda project, mode: {"projects": [], "mode": mode, "open_tabs": []},
        )
        monkeypatch.setattr(api, "lead_say", lambda orch, text, project: {"ok": True})
        config = RemoteConfig(bind_port=0, secret_path="sek", token="tok", mode="control")
        srv = http_server.start_server(config, _FakeOrch())
        try:
            ticket = _issue_ticket(srv)

            sock = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
            sock.sendall(f"GET /sek/api/lead?ticket={ticket} HTTP/1.0\r\n\r\n".encode())
            sock.recv(4096)  # response headers

            srv.stop()

            sock.settimeout(2.0)  # well under _SSE_KEEPALIVE_SEC (15s)
            tail = sock.recv(4096)
            assert tail == b"", "handler must close the connection, not hang until keepalive"
            sock.close()
        finally:
            try:
                srv.stop()
            except Exception:
                pass


class TestBridgeOffMainThreadDispatch:
    def test_pulse_dispatch_never_blocks_the_calling_thread(self, monkeypatch):
        """M-5: `_Bridge._handle` runs on the Qt main thread — `pulse`'s
        loopback socket I/O must be kicked off on a worker thread instead of
        executing inline, or a slow/stuck cli_server would freeze the GUI."""
        gate = threading.Event()

        def _slow_pulse(orch, project):
            gate.wait(timeout=5)
            return {"working": 0, "total": 0}

        monkeypatch.setattr(api, "pulse", _slow_pulse)
        bridge = http_server._Bridge(_FakeOrch())
        pending = http_server._PendingRequest(action="pulse", params={})

        start = time.time()
        bridge._handle(pending)
        elapsed = time.time() - start
        assert elapsed < 1.0

        assert pending.reply.empty(), "api.pulse should still be blocked on the gate"
        gate.set()
        status, payload = pending.reply.get(timeout=5)
        assert (status, payload) == (200, {"working": 0, "total": 0})


class TestStaticFileTraversal:
    def test_path_traversal_outside_static_root_rejected(self, server):
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
            sock.sendall(b"GET /sek/../config.py HTTP/1.0\r\n\r\n")
            resp = sock.recv(4096)
        assert resp.startswith(b"HTTP/1.0 404")

    def test_missing_static_file_is_404(self, server):
        status, _ = _get_status(_url(server, "/sek/does-not-exist.js"))
        assert status == 404


class TestStaticSecurityHeaders:
    """L2: a network-exposed page that innerHTMLs Lead-authored text should
    carry a strict CSP as defense-in-depth (cheap, since the shell has no
    inline <script>)."""

    def test_static_response_carries_csp_header(self, server):
        with urllib.request.urlopen(_url(server, "/sek/"), timeout=5) as resp:
            csp = resp.headers.get("Content-Security-Policy")
        assert csp is not None
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "object-src 'none'" in csp

    def test_json_response_carries_csp_and_nosniff_headers(self, server):
        """L2: `_send_json` used to omit CSP entirely — the one response
        type with no CSP at all. Uses verify-password with no password
        configured (server fixture) so this never touches the bridge."""
        req = urllib.request.Request(
            _url(server, "/sek/api/verify-password"),
            data=b"{}",
            headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            csp = resp.headers.get("Content-Security-Policy")
            assert csp is not None
            assert "default-src 'self'" in csp
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"


class TestHandlerSocketTimeout:
    """M1: no timeout on `_RemoteHandler` lets a trickled request line/body
    pin a handler thread forever, pre-auth — bound every socket read."""

    def test_handler_has_a_bounded_socket_timeout(self):
        assert http_server._RemoteHandler.timeout == 30
