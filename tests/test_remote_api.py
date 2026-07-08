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
                        "last_screenshot": "C:/Users/alice/shot.png",
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

    def test_forwards_from_project_to_cli_server(self, monkeypatch, fake_orch):
        srv = _FakeCliServer({"ok": True, "msg": "status", "status": {}})
        _patch_port(monkeypatch, srv.port)
        try:
            api.pulse(fake_orch, "proj-b")
        finally:
            srv.close()
        assert srv.received[0]["from_project"] == "proj-b"

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


class _FakePane:
    def __init__(self, state: str, working_start: float | None) -> None:
        self.state = state
        self._working_start = working_start
        # decoys — activity() must never surface any of these
        self.last_note = "implement /auth/login"
        self._transcript_path = "C:/secret/transcript.jsonl"
        self.cwd = "/repos/secret-project"


class _FakeOrchWithPanes:
    def __init__(self, panes_by_project: dict) -> None:
        self._panes_by_project = panes_by_project


class TestActivity:
    """Pulse page (project-grouped active panes). DATA-MIN: role + project +
    runtime only — never task text, cwd, command, or status detail."""

    def test_groups_only_working_panes_by_project(self, monkeypatch):
        now = 1_000_000.0
        monkeypatch.setattr(api.time, "time", lambda: now)
        orch = _FakeOrchWithPanes(
            {
                "proj-a": {
                    "backend": _FakePane("working", now - 30),
                    "frontend": _FakePane("done", now - 999),
                },
                "proj-b": {
                    "qa": _FakePane("working", now - 120),
                },
            }
        )
        result = api.activity(orch)
        assert result == {
            "projects": [
                {"project": "proj-a", "roles": [{"role": "backend", "runtime_sec": 30}]},
                {"project": "proj-b", "roles": [{"role": "qa", "runtime_sec": 120}]},
            ]
        }

    def test_project_with_no_working_panes_is_omitted(self, monkeypatch):
        orch = _FakeOrchWithPanes(
            {"proj-a": {"backend": _FakePane("idle", None), "frontend": _FakePane("done", None)}}
        )
        result = api.activity(orch)
        assert result == {"projects": []}

    def test_working_pane_without_a_start_ts_is_skipped(self, monkeypatch):
        # Defensive: set_state("working") always stamps _working_start, but
        # activity() must not fabricate a runtime if it's ever None/missing.
        orch = _FakeOrchWithPanes({"proj-a": {"backend": _FakePane("working", None)}})
        result = api.activity(orch)
        assert result == {"projects": []}

    def test_no_open_panes_returns_empty_projects(self):
        result = api.activity(_FakeOrchWithPanes({}))
        assert result == {"projects": []}

    def test_never_leaks_task_cwd_or_transcript_fields(self, monkeypatch):
        now = 500.0
        monkeypatch.setattr(api.time, "time", lambda: now)
        orch = _FakeOrchWithPanes({"proj-a": {"backend": _FakePane("working", now - 10)}})
        result = api.activity(orch)
        dumped = json.dumps(result)
        assert set(result["projects"][0]["roles"][0].keys()) == {"role", "runtime_sec"}
        for leaked in ("implement", "/auth/login", "transcript.jsonl", "secret-project"):
            assert leaked not in dumped


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

    def test_forwards_from_project_to_cli_server(self, monkeypatch, fake_orch):
        srv = _FakeCliServer({"ok": True, "msg": "sent to lead"})
        _patch_port(monkeypatch, srv.port)
        try:
            api.lead_say(fake_orch, "hello", "proj-b")
        finally:
            srv.close()
        assert srv.received[0]["from_project"] == "proj-b"

    def test_cli_server_failure_propagates(self, monkeypatch, fake_orch):
        srv = _FakeCliServer({"ok": False, "msg": "lead is not running"})
        _patch_port(monkeypatch, srv.port)
        try:
            with pytest.raises(api.RemoteApiError) as excinfo:
                api.lead_say(fake_orch, "hi", None)
        finally:
            srv.close()
        assert excinfo.value.status == 502


class _FakeMainWindow:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self._on_open: object = None

    def _open_project_tab(self, project_name: str) -> None:
        self.opened.append(project_name)
        if self._on_open is not None:
            self._on_open(project_name)


class _FakeOrchWithParent:
    """`open_project` reaches main_window via `orch.parent()` — the Qt
    parent `main_window.py` passes to `Orchestrator(self)` at construction
    (never a static import, see api.py's docstring)."""

    def __init__(self, main_window) -> None:
        self._main_window = main_window

    def parent(self):
        return self._main_window


class TestOpenProject:
    def test_rejects_project_not_in_projects_json(self, monkeypatch):
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a"])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: [])
        with pytest.raises(api.RemoteApiError) as excinfo:
            api.open_project(_FakeOrchWithParent(_FakeMainWindow()), "ghost-project")
        assert excinfo.value.status == 400

    def test_rejects_non_string_project(self, monkeypatch):
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a"])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: [])
        with pytest.raises(api.RemoteApiError) as excinfo:
            api.open_project(_FakeOrchWithParent(_FakeMainWindow()), 123)
        assert excinfo.value.status == 400

    def test_already_open_is_idempotent_noop(self, monkeypatch):
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a"])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: ["proj-a"])
        main_window = _FakeMainWindow()
        result = api.open_project(_FakeOrchWithParent(main_window), "proj-a")
        assert result == {"ok": True, "project": "proj-a"}
        assert main_window.opened == [], (
            "already-open project must not re-trigger _open_project_tab"
        )

    def test_success_opens_new_project_via_main_window(self, monkeypatch):
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a", "proj-b"])
        open_tabs = ["proj-a"]
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: open_tabs)
        main_window = _FakeMainWindow()
        main_window._on_open = open_tabs.append
        result = api.open_project(_FakeOrchWithParent(main_window), "proj-b")
        assert result == {"ok": True, "project": "proj-b"}
        assert main_window.opened == ["proj-b"]

    def test_folder_missing_surfaces_as_conflict(self, monkeypatch):
        """`_open_project_tab` silently no-ops (status-bar message only) when
        the project's folder is missing on disk — `open_project` must not
        report a false `ok` in that case."""
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a"])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: [])
        main_window = _FakeMainWindow()  # opened stays [] — simulates the no-op
        with pytest.raises(api.RemoteApiError) as excinfo:
            api.open_project(_FakeOrchWithParent(main_window), "proj-a")
        assert excinfo.value.status == 409

    def test_main_window_unreachable_raises_server_error(self, monkeypatch):
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a"])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: [])

        class _NoOpenTabMethod:
            pass

        with pytest.raises(api.RemoteApiError) as excinfo:
            api.open_project(_FakeOrchWithParent(_NoOpenTabMethod()), "proj-a")
        assert excinfo.value.status == 500


class TestLeadHistory:
    """Gemini CRITICAL/HIGH: `/api/lead/history` lets the PWA repopulate its
    chat log on connect/reconnect/project-switch instead of a blank screen.
    Reuses `notify.py`'s uuid->jsonl resolution + text extraction verbatim
    so this can never disagree with the live SSE tail on what counts as a
    reply."""

    class _Orch:
        pass

    def test_no_resolvable_session_returns_empty_messages(self, monkeypatch):
        monkeypatch.setattr(api.notify, "resolve_lead_jsonl", lambda orch, ns: None)
        result = api.lead_history(self._Orch(), "proj-a")
        assert result == {"project": "proj-a", "messages": []}

    def test_reads_recent_messages_oldest_first_with_kind_field(self, monkeypatch, tmp_path):
        path = tmp_path / "uuid-1.jsonl"
        monkeypatch.setattr(api.notify, "resolve_lead_jsonl", lambda orch, ns: path)
        monkeypatch.setattr(
            api.notify,
            "read_recent_lead_messages",
            lambda p, limit: [
                {"text": "first", "kind": "me"},
                {"text": "second", "kind": "lead"},
            ],
        )
        result = api.lead_history(self._Orch(), "proj-a", limit=2)
        assert result == {
            "project": "proj-a",
            "messages": [
                {"text": "first", "kind": "me"},
                {"text": "second", "kind": "lead"},
            ],
        }

    def test_limit_defaults_to_200(self, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(api.notify, "resolve_lead_jsonl", lambda orch, ns: tmp_path)

        def _fake_read(path, limit):
            seen["limit"] = limit
            return []

        monkeypatch.setattr(api.notify, "read_recent_lead_messages", _fake_read)
        api.lead_history(self._Orch(), "proj-a")
        assert seen["limit"] == 200

    def test_limit_is_clamped_to_the_max(self, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(api.notify, "resolve_lead_jsonl", lambda orch, ns: tmp_path)

        def _fake_read(path, limit):
            seen["limit"] = limit
            return []

        monkeypatch.setattr(api.notify, "read_recent_lead_messages", _fake_read)
        api.lead_history(self._Orch(), "proj-a", limit=99999)
        assert seen["limit"] == 200

    def test_non_numeric_limit_falls_back_to_default(self, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(api.notify, "resolve_lead_jsonl", lambda orch, ns: tmp_path)

        def _fake_read(path, limit):
            seen["limit"] = limit
            return []

        monkeypatch.setattr(api.notify, "read_recent_lead_messages", _fake_read)
        api.lead_history(self._Orch(), "proj-a", limit="not-a-number")
        assert seen["limit"] == 200


class TestProjects:
    def test_reads_active_and_known_projects(self, monkeypatch):
        # M-1/M-3: each project is `{name, active, path}`, and `mode` rides
        # along in the same response — the PWA has no dedicated mode
        # endpoint. `path` is the project's real Lead cwd (project picker),
        # not the placeholder the PWA used to fake client-side.
        monkeypatch.setattr(api._config, "active_project", lambda: ("proj-a", {}))
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a", "proj-b"])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: ["proj-a"])
        monkeypatch.setattr(
            api._config,
            "lead_cwd",
            lambda name: {"proj-a": "/repos/proj-a", "proj-b": "/repos/proj-b"}.get(name),
        )
        result = api.projects(None, "control")
        assert result == {
            "projects": [
                {"name": "proj-a", "active": True, "path": "/repos/proj-a"},
                {"name": "proj-b", "active": False, "path": "/repos/proj-b"},
            ],
            "mode": "control",
            "open_tabs": ["proj-a"],
        }

    def test_path_falls_back_to_empty_string_when_unresolved(self, monkeypatch):
        monkeypatch.setattr(api._config, "active_project", lambda: (None, {}))
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a"])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: [])
        monkeypatch.setattr(api._config, "lead_cwd", lambda name: None)
        result = api.projects(None)
        assert result["projects"] == [{"name": "proj-a", "active": False, "path": ""}]

    def test_mode_defaults_to_view(self, monkeypatch):
        monkeypatch.setattr(api._config, "active_project", lambda: (None, {}))
        monkeypatch.setattr(api._config, "list_project_names", lambda: [])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: [])
        assert api.projects(None)["mode"] == "view"
