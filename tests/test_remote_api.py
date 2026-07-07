"""Tests for `agent_takkub.remote.api` — the loopback cli_server client and
the project-list reader. Central focus (finding B2): `pulse()` must never
leak role/task/state/transcript text, only a bare `{working, total}` count.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from agent_takkub.remote import api


class _FakeCliServer:
    """A minimal newline-JSON loopback server standing in for cli_server —
    good enough to exercise `api.py`'s actual socket client code, not a
    reimplementation of cli_server's own dispatch logic (that's
    test_cli_server.py's job)."""

    def __init__(self, response: dict) -> None:
        self._response = response
        self.received: list[dict] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve_one, daemon=True)
        self._thread.start()

    def _serve_one(self) -> None:
        try:
            conn, _addr = self._sock.accept()
        except OSError:
            return
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if buf:
                self.received.append(json.loads(buf.split(b"\n", 1)[0].decode("utf-8")))
            conn.sendall((json.dumps(self._response) + "\n").encode("utf-8"))

    def close(self) -> None:
        self._sock.close()


class _FakeOrch:
    _lead_token = "lead-tok"


@pytest.fixture
def fake_orch() -> _FakeOrch:
    return _FakeOrch()


def _patch_port(monkeypatch, port: int) -> None:
    monkeypatch.setattr(api._config, "read_port", lambda: port)


class TestPulseDataMinimization:
    def test_counts_only_working_panes(self, monkeypatch, fake_orch):
        srv = _FakeCliServer(
            {"ok": True, "msg": "status", "status": {"frontend": "working", "backend": "idle"}}
        )
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.pulse(fake_orch, None)
        finally:
            srv.close()
        assert result == {"working": 1, "total": 2}

    def test_stalled_state_still_counts_as_working(self, monkeypatch, fake_orch):
        srv = _FakeCliServer(
            {"ok": True, "msg": "status", "status": {"qa": "working (stalled 12m)"}}
        )
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.pulse(fake_orch, None)
        finally:
            srv.close()
        assert result == {"working": 1, "total": 1}

    def test_never_leaks_role_task_or_transcript_fields(self, monkeypatch, fake_orch):
        # Simulate an over-sharing / misrouted cli_server response (as if
        # `status` accidentally carried full pane_status_report-shaped data)
        # and confirm pulse() strips it down to the count regardless.
        srv = _FakeCliServer(
            {
                "ok": True,
                "msg": "status",
                "status": {"backend": "working"},
                "panes": {
                    "backend": {
                        "task": "implement /auth/login",
                        "transcript_tail": "secret internal chatter",
                        "last_screenshot": "C:/Users/monch/shot.png",
                    }
                },
            }
        )
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.pulse(fake_orch, None)
        finally:
            srv.close()
        assert set(result.keys()) == {"working", "total"}
        dumped = json.dumps(result)
        for leaked in ("implement", "secret internal chatter", "shot.png"):
            assert leaked not in dumped

    def test_malformed_response_yields_zero_counts(self, monkeypatch, fake_orch):
        srv = _FakeCliServer({"ok": False, "msg": "bad"})
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.pulse(fake_orch, None)
        finally:
            srv.close()
        assert result == {"working": 0, "total": 0}

    def test_stamps_lead_token_and_list_cmd_never_status(self, monkeypatch, fake_orch):
        srv = _FakeCliServer({"ok": True, "msg": "status", "status": {}})
        _patch_port(monkeypatch, srv.port)
        try:
            api.pulse(fake_orch, None)
        finally:
            srv.close()
        assert len(srv.received) == 1
        sent = srv.received[0]
        assert sent["cmd"] == "list", "B2: pulse must never use cmd:'status'"
        assert sent["auth"] == "lead-tok"

    def test_no_port_file_raises_service_unavailable(self, monkeypatch, fake_orch):
        monkeypatch.setattr(api._config, "read_port", lambda: None)
        with pytest.raises(api.RemoteApiError) as excinfo:
            api.pulse(fake_orch, None)
        assert excinfo.value.status == 503

    def test_missing_lead_token_raises(self, monkeypatch):
        class _NoToken:
            pass

        with pytest.raises(api.RemoteApiError) as excinfo:
            api.pulse(_NoToken(), None)
        assert excinfo.value.status == 500


class TestLeadSay:
    def test_empty_message_rejected(self, fake_orch):
        with pytest.raises(api.RemoteApiError) as excinfo:
            api.lead_say(fake_orch, "   ", None)
        assert excinfo.value.status == 400

    def test_success_sends_as_remote_to_lead(self, monkeypatch, fake_orch):
        srv = _FakeCliServer({"ok": True, "msg": "sent to lead"})
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.lead_say(fake_orch, "hello lead", None)
        finally:
            srv.close()
        assert result == {"ok": True}
        assert len(srv.received) == 1
        sent = srv.received[0]
        assert sent["cmd"] == "send"
        assert sent["to"] == "lead"
        assert sent["from"] == "remote"
        assert sent["msg"] == "hello lead"

    def test_cli_server_failure_propagates(self, monkeypatch, fake_orch):
        srv = _FakeCliServer({"ok": False, "msg": "lead is not running"})
        _patch_port(monkeypatch, srv.port)
        try:
            with pytest.raises(api.RemoteApiError) as excinfo:
                api.lead_say(fake_orch, "hi", None)
        finally:
            srv.close()
        assert excinfo.value.status == 502


class TestProjects:
    def test_reads_active_and_known_projects(self, monkeypatch):
        # M-1/M-3: each project is `{name, active}`, and `mode` rides along
        # in the same response — the PWA has no dedicated mode endpoint.
        monkeypatch.setattr(api._config, "active_project", lambda: ("proj-a", {}))
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a", "proj-b"])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: ["proj-a"])
        result = api.projects(None, "control")
        assert result == {
            "projects": [{"name": "proj-a", "active": True}, {"name": "proj-b", "active": False}],
            "mode": "control",
            "open_tabs": ["proj-a"],
        }

    def test_mode_defaults_to_view(self, monkeypatch):
        monkeypatch.setattr(api._config, "active_project", lambda: (None, {}))
        monkeypatch.setattr(api._config, "list_project_names", lambda: [])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: [])
        assert api.projects(None)["mode"] == "view"
