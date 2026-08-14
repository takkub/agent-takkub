"""Tests for `agent_takkub.remote.api` — the loopback cli_server client and
the project-list reader. Central focus (finding B2): `pulse()` must never
leak role/task/state/transcript text, only `{working, total, provider}`.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
from pathlib import Path

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
    """PULSE_SHOW_TEAM defaults True (#200) — these pin the default,
    count-every-pane behavior. No monkeypatch needed: this *is* the default
    now, unlike the pre-#200 world where it required flipping a gate off."""

    def test_counts_only_working_panes(self, monkeypatch, fake_orch):
        srv = _FakeCliServer(
            {"ok": True, "msg": "status", "status": {"frontend": "working", "backend": "idle"}}
        )
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.pulse(fake_orch, None)
        finally:
            srv.close()
        assert result == {"working": 1, "total": 2, "provider": "claude"}

    def test_stalled_state_still_counts_as_working(self, monkeypatch, fake_orch):
        srv = _FakeCliServer(
            {"ok": True, "msg": "status", "status": {"qa": "working (stalled 12m)"}}
        )
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.pulse(fake_orch, None)
        finally:
            srv.close()
        assert result == {"working": 1, "total": 1, "provider": "claude"}

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
        assert set(result.keys()) == {"working", "total", "provider"}
        dumped = json.dumps(result)
        for leaked in ("implement", "secret internal chatter", "shot.png"):
            assert leaked not in dumped


class TestPulseShowTeamGate:
    """PULSE_SHOW_TEAM defaults True (#200, 2026-08-14) — the opposite of the
    pre-#200 default. These pin the `PULSE_SHOW_TEAM=False` fallback: scoped
    down to the `lead` entry only, same contract the old always-on
    LEAD_ONLY_STREAM gate used to guarantee for `/api/pulse`."""

    def test_gate_defaults_to_full_team(self):
        assert api._remote_config.PULSE_SHOW_TEAM is True

    def test_counts_lead_only_ignores_working_teammates(self, monkeypatch, fake_orch):
        monkeypatch.setattr(api._remote_config, "PULSE_SHOW_TEAM", False)
        srv = _FakeCliServer(
            {
                "ok": True,
                "msg": "status",
                "status": {
                    "lead": "idle",
                    "frontend": "working",
                    "backend": "working",
                    "qa": "working",
                },
            }
        )
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.pulse(fake_orch, None)
        finally:
            srv.close()
        assert result == {"working": 0, "total": 1, "provider": "claude"}

    def test_counts_lead_working(self, monkeypatch, fake_orch):
        monkeypatch.setattr(api._remote_config, "PULSE_SHOW_TEAM", False)
        srv = _FakeCliServer(
            {"ok": True, "msg": "status", "status": {"lead": "working", "backend": "idle"}}
        )
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.pulse(fake_orch, None)
        finally:
            srv.close()
        assert result == {"working": 1, "total": 1, "provider": "claude"}

    def test_lead_stalled_state_still_counts_as_working(self, monkeypatch, fake_orch):
        monkeypatch.setattr(api._remote_config, "PULSE_SHOW_TEAM", False)
        srv = _FakeCliServer(
            {"ok": True, "msg": "status", "status": {"lead": "working (stalled 12m)"}}
        )
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.pulse(fake_orch, None)
        finally:
            srv.close()
        assert result == {"working": 1, "total": 1, "provider": "claude"}

    def test_no_lead_pane_yields_zero_even_with_teammates_working(self, monkeypatch, fake_orch):
        monkeypatch.setattr(api._remote_config, "PULSE_SHOW_TEAM", False)
        srv = _FakeCliServer(
            {"ok": True, "msg": "status", "status": {"frontend": "working", "backend": "working"}}
        )
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.pulse(fake_orch, None)
        finally:
            srv.close()
        assert result == {"working": 0, "total": 0, "provider": "claude"}

    def test_total_never_reveals_team_size(self, monkeypatch, fake_orch):
        monkeypatch.setattr(api._remote_config, "PULSE_SHOW_TEAM", False)
        status = {f"backend#{i}": "working" for i in range(5)}
        status["lead"] = "idle"
        srv = _FakeCliServer({"ok": True, "msg": "status", "status": status})
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.pulse(fake_orch, None)
        finally:
            srv.close()
        assert result["total"] == 1

    def test_malformed_response_yields_zero_counts(self, monkeypatch, fake_orch):
        srv = _FakeCliServer({"ok": False, "msg": "bad"})
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.pulse(fake_orch, None)
        finally:
            srv.close()
        assert result == {"working": 0, "total": 0, "provider": "claude"}

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

    def test_exposes_scoped_lead_provider(self, monkeypatch, fake_orch):
        seen = {}

        def _provider(orch, project):
            seen["project"] = project
            return "codex"

        monkeypatch.setattr(api.notify, "lead_provider_name", _provider)
        srv = _FakeCliServer({"ok": True, "msg": "status", "status": {"lead": "idle"}})
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.pulse(fake_orch, "proj-b")
        finally:
            srv.close()
        assert seen["project"] == "proj-b"
        assert result["provider"] == "codex"

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
    def __init__(self, state: str, working_start: float | None, provider: str = "claude") -> None:
        self.state = state
        self._working_start = working_start
        self.model = type("FakePaneModel", (), {"provider_name": provider})()
        # decoys — activity() must never surface any of these
        self.last_note = "implement /auth/login"
        self._transcript_path = "C:/secret/transcript.jsonl"
        self.cwd = "/repos/secret-project"


class _FakeOrchWithPanes:
    def __init__(self, panes_by_project: dict) -> None:
        self._panes_by_project = panes_by_project


class TestActivity:
    """Pulse page (project-grouped open panes). DATA-MIN: role + project +
    state + runtime only — never task text, cwd, command, or fine-grained
    status detail. PULSE_SHOW_TEAM defaults True (#200, 2026-08-14): `roles`
    lists every open teammate pane, working or idle, not just working ones."""

    def test_groups_leads_and_teammates_by_project(self, monkeypatch):
        now = 1_000_000.0
        monkeypatch.setattr(api.time, "time", lambda: now)
        orch = _FakeOrchWithPanes(
            {
                "proj-a": {
                    "lead": _FakePane("working", now - 30),
                    "backend": _FakePane("working", now - 30),
                },
                "proj-b": {
                    "lead": _FakePane("idle", None),
                    "qa": _FakePane("working", now - 120),
                },
            }
        )
        result = api.activity(orch)
        assert result == {
            "projects": [
                {
                    "project": "proj-a",
                    "roles": [
                        {
                            "role": "backend",
                            "state": "working",
                            "runtime_sec": 30,
                            "provider": "claude",
                        }
                    ],
                    "lead": {"state": "working", "runtime_sec": 30, "provider": "claude"},
                },
                {
                    "project": "proj-b",
                    "roles": [
                        {"role": "qa", "state": "working", "runtime_sec": 120, "provider": "claude"}
                    ],
                    "lead": {"state": "idle", "runtime_sec": 0, "provider": "claude"},
                },
            ]
        }

    def test_idle_teammates_are_included_too(self, monkeypatch):
        """#200: the whole point is that idle panes show up, not just working
        ones — a project with three idle teammates still shows three chips."""
        orch = _FakeOrchWithPanes(
            {"proj-a": {"backend": _FakePane("idle", None), "frontend": _FakePane("done", None)}}
        )
        result = api.activity(orch)
        assert result == {
            "projects": [
                {
                    "project": "proj-a",
                    "roles": [
                        {
                            "role": "backend",
                            "state": "idle",
                            "runtime_sec": 0,
                            "provider": "claude",
                        },
                        {
                            "role": "frontend",
                            "state": "idle",
                            "runtime_sec": 0,
                            "provider": "claude",
                        },
                    ],
                }
            ]
        }

    def test_project_with_working_teammates_but_no_lead_is_shown(self, monkeypatch):
        """#200: teammates alone are now a reason to show a card — the whole
        point is visibility into what's open even without Lead in frame."""
        now = 1_000_000.0
        monkeypatch.setattr(api.time, "time", lambda: now)
        orch = _FakeOrchWithPanes(
            {"proj-a": {"backend": _FakePane("working", now - 30)}},
        )
        assert api.activity(orch) == {
            "projects": [
                {
                    "project": "proj-a",
                    "roles": [
                        {
                            "role": "backend",
                            "state": "working",
                            "runtime_sec": 30,
                            "provider": "claude",
                        }
                    ],
                }
            ]
        }

    def test_working_pane_without_a_start_ts_reports_zero_runtime(self, monkeypatch):
        # Defensive: set_state("working") always stamps _working_start, but
        # activity() must not fabricate a runtime if it's ever None/missing —
        # it still shows the pane (#200), just with runtime_sec: 0.
        orch = _FakeOrchWithPanes({"proj-a": {"backend": _FakePane("working", None)}})
        result = api.activity(orch)
        assert result == {
            "projects": [
                {
                    "project": "proj-a",
                    "roles": [
                        {
                            "role": "backend",
                            "state": "working",
                            "runtime_sec": 0,
                            "provider": "claude",
                        }
                    ],
                }
            ]
        }

    def test_teammates_omitted_when_show_team_is_off(self, monkeypatch):
        """PULSE_SHOW_TEAM=False reverts to the pre-#200 Lead-only default."""
        monkeypatch.setattr(api._remote_config, "PULSE_SHOW_TEAM", False)
        orch = _FakeOrchWithPanes(
            {"proj-a": {"backend": _FakePane("working", None), "lead": _FakePane("idle", None)}}
        )
        result = api.activity(orch)
        assert result == {
            "projects": [
                {
                    "project": "proj-a",
                    "roles": [],
                    "lead": {"state": "idle", "runtime_sec": 0, "provider": "claude"},
                }
            ]
        }

    def test_no_open_panes_returns_empty_projects(self):
        result = api.activity(_FakeOrchWithPanes({}))
        assert result == {"projects": []}

    def test_never_leaks_task_cwd_or_transcript_fields(self, monkeypatch):
        now = 500.0
        monkeypatch.setattr(api.time, "time", lambda: now)
        orch = _FakeOrchWithPanes(
            {
                "proj-a": {
                    "lead": _FakePane("working", now - 10),
                    "backend": _FakePane("working", now - 5),
                }
            }
        )
        result = api.activity(orch)
        dumped = json.dumps(result)
        assert set(result["projects"][0]["lead"].keys()) == {
            "state",
            "runtime_sec",
            "provider",
        }
        assert set(result["projects"][0]["roles"][0].keys()) == {
            "role",
            "state",
            "runtime_sec",
            "provider",
        }
        for leaked in ("implement", "/auth/login", "transcript.jsonl", "secret-project"):
            assert leaked not in dumped

    def test_working_lead_included_with_runtime(self, monkeypatch):
        now = 1_000.0
        monkeypatch.setattr(api.time, "time", lambda: now)
        orch = _FakeOrchWithPanes({"proj-a": {"lead": _FakePane("working", now - 45)}})
        result = api.activity(orch)
        assert result == {
            "projects": [
                {
                    "project": "proj-a",
                    "roles": [],
                    "lead": {"state": "working", "runtime_sec": 45, "provider": "claude"},
                }
            ]
        }

    def test_idle_lead_included_with_zero_runtime_not_stale_working_start(self, monkeypatch):
        # W4: an idle Lead's _working_start is cleared by set_state, but even
        # if a caller left a stale value there, idle must never reuse it.
        now = 1_000.0
        monkeypatch.setattr(api.time, "time", lambda: now)
        orch = _FakeOrchWithPanes({"proj-a": {"lead": _FakePane("idle", now - 500)}})
        result = api.activity(orch)
        assert result == {
            "projects": [
                {
                    "project": "proj-a",
                    "roles": [],
                    "lead": {"state": "idle", "runtime_sec": 0, "provider": "claude"},
                }
            ]
        }

    def test_multiple_teammates_all_shown_beside_lead(self, monkeypatch):
        """#200: `roles` now carries every open teammate, not just one."""
        now = 2_000.0
        monkeypatch.setattr(api.time, "time", lambda: now)
        orch = _FakeOrchWithPanes(
            {
                "proj-a": {
                    "lead": _FakePane("idle", None),
                    "backend": _FakePane("working", now - 10),
                    "qa": _FakePane("working", now - 20),
                }
            }
        )
        result = api.activity(orch)
        roles_by_role = {r["role"]: r for r in result["projects"][0]["roles"]}
        assert roles_by_role["backend"] == {
            "role": "backend",
            "state": "working",
            "runtime_sec": 10,
            "provider": "claude",
        }
        assert roles_by_role["qa"] == {
            "role": "qa",
            "state": "working",
            "runtime_sec": 20,
            "provider": "claude",
        }
        assert result["projects"][0]["lead"] == {
            "state": "idle",
            "runtime_sec": 0,
            "provider": "claude",
        }

    def test_teammates_reappear_when_show_team_flag_flips_back_on(self, monkeypatch):
        """The switch is real, not a hard-coded deletion — proven by toggling
        PULSE_SHOW_TEAM off then back on inside the same test."""
        now = 2_000.0
        monkeypatch.setattr(api.time, "time", lambda: now)
        orch = _FakeOrchWithPanes(
            {
                "proj-a": {
                    "lead": _FakePane("idle", None),
                    "backend": _FakePane("working", now - 10),
                }
            }
        )
        monkeypatch.setattr(api._remote_config, "PULSE_SHOW_TEAM", False)
        assert api.activity(orch)["projects"][0]["roles"] == []

        monkeypatch.setattr(api._remote_config, "PULSE_SHOW_TEAM", True)
        result = api.activity(orch)
        assert result == {
            "projects": [
                {
                    "project": "proj-a",
                    "roles": [
                        {
                            "role": "backend",
                            "state": "working",
                            "runtime_sec": 10,
                            "provider": "claude",
                        }
                    ],
                    "lead": {"state": "idle", "runtime_sec": 0, "provider": "claude"},
                }
            ]
        }

    def test_project_with_only_idle_lead_and_no_working_roles_is_not_omitted(self, monkeypatch):
        orch = _FakeOrchWithPanes({"proj-a": {"lead": _FakePane("idle", None)}})
        result = api.activity(orch)
        assert result == {
            "projects": [
                {
                    "project": "proj-a",
                    "roles": [],
                    "lead": {"state": "idle", "runtime_sec": 0, "provider": "claude"},
                }
            ]
        }

    def test_uses_provider_recorded_on_each_live_pane(self, monkeypatch):
        now = 2_000.0
        monkeypatch.setattr(api.time, "time", lambda: now)
        orch = _FakeOrchWithPanes(
            {
                "proj-a": {
                    "lead": _FakePane("idle", None, "codex"),
                    "backend": _FakePane("working", now - 10, "gemini"),
                }
            }
        )
        project = api.activity(orch)["projects"][0]
        assert project["lead"]["provider"] == "codex"
        assert project["roles"][0]["provider"] == "gemini"


class TestUsage:
    """`/api/usage` must NEVER trigger a live provider fetch itself — it
    only reads `provider_usage.get_store()`'s cache (design doc §4: a phone
    poll must never become an extra rate-limit hit)."""

    def test_reads_from_store_cache_without_fetching(self, monkeypatch):
        from agent_takkub import provider_usage

        class _FakeStore:
            def get_all(self):
                return {
                    "claude": provider_usage.ProviderUsage(
                        provider="claude", status="active", utilization=7.0
                    )
                }

        def _boom():
            raise AssertionError("usage() must not call fetch_provider_usage directly")

        monkeypatch.setattr(provider_usage, "get_store", lambda: _FakeStore())
        monkeypatch.setattr(provider_usage, "fetch_provider_usage", _boom)

        result = api.usage()
        by_provider = {p["provider"]: p for p in result["providers"]}
        assert by_provider["claude"]["status"] == "active"
        assert by_provider["claude"]["utilization"] == 7.0

    def test_provider_missing_from_cache_reports_loading_not_an_error(self, monkeypatch):
        from agent_takkub import provider_usage

        class _FakeStore:
            def get_all(self):
                return {}

        monkeypatch.setattr(provider_usage, "get_store", lambda: _FakeStore())

        result = api.usage()
        by_provider = {p["provider"]: p for p in result["providers"]}
        assert set(by_provider) == set(provider_usage.PROVIDER_NAMES)
        for entry in by_provider.values():
            assert entry["status"] == "loading"
            assert entry["utilization"] is None

    def test_response_covers_every_registered_provider(self, monkeypatch):
        from agent_takkub import provider_usage

        class _FakeStore:
            def get_all(self):
                return {}

        monkeypatch.setattr(provider_usage, "get_store", lambda: _FakeStore())
        result = api.usage()
        assert len(result["providers"]) == len(provider_usage.PROVIDER_NAMES)

    def test_reports_cockpit_version(self, monkeypatch):
        """#192: the phone had no way to tell whether it's talking to an old
        cockpit build — rides along on this already-polled endpoint."""
        from agent_takkub import __version__, provider_usage

        class _FakeStore:
            def get_all(self):
                return {}

        monkeypatch.setattr(provider_usage, "get_store", lambda: _FakeStore())
        result = api.usage()
        assert result["cockpit_version"] == __version__


class TestLeadSay:
    def test_empty_message_rejected(self, fake_orch):
        with pytest.raises(api.RemoteApiError) as excinfo:
            api.lead_say(fake_orch, "   ", None)
        assert excinfo.value.status == 400

    def test_success_sends_as_remote_to_lead(self, monkeypatch, fake_orch):
        monkeypatch.setattr(api.notify, "lead_provider_name", lambda orch, ns: "claude")
        srv = _FakeCliServer({"ok": True, "msg": "sent to lead"})
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.lead_say(fake_orch, "hello lead", None)
        finally:
            srv.close()
        assert result == {
            "ok": True,
            "provider": "claude",
            "mirror_supported": True,
            "lead_provider_note": None,
        }
        assert len(srv.received) == 1
        sent = srv.received[0]
        assert sent["cmd"] == "send"
        assert sent["to"] == "lead"
        assert sent["from"] == "remote"
        assert sent["msg"] == "hello lead"

    def test_success_flags_unsupported_mirror_provider(self, monkeypatch, fake_orch):
        """2026-08-13 remote-mirror fix: a provider with no history scanner
        (opencode/kimi/cursor) still delivers the message, but the response
        must tell the PWA not to expect a live mirrored reply — this is the
        signal that stops the phone's spinner from hanging forever with zero
        explanation (the reported BlueParking/OpenCode bug)."""
        monkeypatch.setattr(api.notify, "lead_provider_name", lambda orch, ns: "opencode")
        srv = _FakeCliServer({"ok": True, "msg": "sent to lead"})
        _patch_port(monkeypatch, srv.port)
        try:
            result = api.lead_say(fake_orch, "hello lead", None)
        finally:
            srv.close()
        assert result["ok"] is True
        assert result["provider"] == "opencode"
        assert result["mirror_supported"] is False
        assert result["lead_provider_note"] is not None
        assert "opencode" in result["lead_provider_note"]

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


class TestLeadUploadImage:
    _PNG = b"\x89PNG\r\n\x1a\n" + b"test-pixels"

    @staticmethod
    def _data_url(data: bytes) -> str:
        return "data:image/png;base64," + base64.b64encode(data).decode("ascii")

    def test_saves_under_project_artifacts_and_sends_exact_path(
        self, monkeypatch, tmp_path: Path, fake_orch
    ):
        monkeypatch.setattr(api._config, "RUNTIME_DIR", tmp_path / "runtime")
        seen: dict = {}

        def _fake_say(orch, text, project):
            seen.update(text=text, project=project)
            return {
                "ok": True,
                "provider": "claude",
                "mirror_supported": True,
                "lead_provider_note": None,
            }

        monkeypatch.setattr(api, "lead_say", _fake_say)
        result = api.lead_upload_image(
            fake_orch,
            self._data_url(self._PNG),
            "../../phone.png",
            "ช่วยดู error นี้",
            "proj-a",
        )

        images = list((tmp_path / "runtime" / "exports").glob("*/proj-a/screenshots/*.png"))
        assert len(images) == 1
        assert images[0].read_bytes() == self._PNG
        assert str(images[0]) in seen["text"]
        assert "ช่วยดู error นี้" in seen["text"]
        assert seen["project"] == "proj-a"
        assert result == {
            "ok": True,
            "name": "phone.png",
            "mirror_supported": True,
            "lead_provider_note": None,
        }

    def test_caption_control_chars_are_stripped_before_reaching_lead_pty(
        self, monkeypatch, tmp_path: Path, fake_orch
    ):
        monkeypatch.setattr(api._config, "RUNTIME_DIR", tmp_path / "runtime")
        seen: dict = {}

        def _fake_say(orch, text, project):
            seen.update(text=text)
            return {
                "ok": True,
                "provider": "claude",
                "mirror_supported": True,
                "lead_provider_note": None,
            }

        monkeypatch.setattr(api, "lead_say", _fake_say)
        api.lead_upload_image(
            fake_orch,
            self._data_url(self._PNG),
            "phone.png",
            "ดูด้วย\x03\x1b[2Jrm -rf /\tok",
            "proj-a",
        )

        assert "\x03" not in seen["text"]
        assert "\x1b" not in seen["text"]
        assert "\t" not in seen["text"]
        # ESC itself is stripped; the printable text that followed it in the
        # escape sequence is not control data and is left intact.
        assert "ดูด้วย[2Jrm -rf /ok" in seen["text"]

    def test_rejects_mime_magic_mismatch_without_writing(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(api._config, "RUNTIME_DIR", tmp_path / "runtime")
        with pytest.raises(api.RemoteApiError) as excinfo:
            api.lead_upload_image(None, self._data_url(b"not a png"), "x.png", "", "proj-a")
        assert excinfo.value.status == 400
        assert not (tmp_path / "runtime").exists()

    def test_removes_new_file_when_lead_delivery_fails(
        self, monkeypatch, tmp_path: Path, fake_orch
    ):
        monkeypatch.setattr(api._config, "RUNTIME_DIR", tmp_path / "runtime")

        def _fail(*_args):
            raise api.RemoteApiError(502, "lead is not running")

        monkeypatch.setattr(api, "lead_say", _fail)
        with pytest.raises(api.RemoteApiError) as excinfo:
            api.lead_upload_image(fake_orch, self._data_url(self._PNG), "phone.png", "", "proj-a")
        assert excinfo.value.status == 502
        assert list((tmp_path / "runtime" / "exports").glob("**/*.*")) == []


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


class _FakeMainWindowClose:
    def __init__(self, ok: bool = True, msg: str = "closed") -> None:
        self.closed: list[str] = []
        self._ok = ok
        self._msg = msg

    def _close_project_tab(self, project: str, confirm: bool = False):
        self.closed.append(project)
        assert confirm is False, "remote close must never trigger the desktop Qt confirm dialog"
        return self._ok, self._msg


class TestCloseProject:
    def test_rejects_project_not_in_projects_json(self, monkeypatch):
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a"])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: ["proj-a"])
        with pytest.raises(api.RemoteApiError) as excinfo:
            api.close_project(_FakeOrchWithParent(_FakeMainWindowClose()), "ghost-project")
        assert excinfo.value.status == 400

    def test_rejects_non_string_project(self, monkeypatch):
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a"])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: ["proj-a"])
        with pytest.raises(api.RemoteApiError) as excinfo:
            api.close_project(_FakeOrchWithParent(_FakeMainWindowClose()), 123)
        assert excinfo.value.status == 400

    def test_already_closed_is_idempotent_noop(self, monkeypatch):
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a"])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: [])
        main_window = _FakeMainWindowClose()
        result = api.close_project(_FakeOrchWithParent(main_window), "proj-a")
        assert result == {"ok": True, "project": "proj-a"}
        assert main_window.closed == [], (
            "already-closed project must not re-trigger _close_project_tab"
        )

    def test_success_closes_via_main_window_without_confirm_dialog(self, monkeypatch):
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a"])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: ["proj-a"])
        main_window = _FakeMainWindowClose(ok=True, msg="closed tab · proj-a")
        result = api.close_project(_FakeOrchWithParent(main_window), "proj-a")
        assert result == {"ok": True, "project": "proj-a"}
        assert main_window.closed == ["proj-a"]

    def test_teardown_failure_surfaces_as_conflict(self, monkeypatch):
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a"])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: ["proj-a"])
        main_window = _FakeMainWindowClose(ok=False, msg="no open tab for project 'proj-a'")
        with pytest.raises(api.RemoteApiError) as excinfo:
            api.close_project(_FakeOrchWithParent(main_window), "proj-a")
        assert excinfo.value.status == 409

    def test_main_window_unreachable_raises_server_error(self, monkeypatch):
        monkeypatch.setattr(api._config, "list_project_names", lambda: ["proj-a"])
        monkeypatch.setattr(api._config, "get_open_tabs", lambda: ["proj-a"])

        class _NoCloseTabMethod:
            pass

        with pytest.raises(api.RemoteApiError) as excinfo:
            api.close_project(_FakeOrchWithParent(_NoCloseTabMethod()), "proj-a")
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
        monkeypatch.setattr(
            api.notify, "lead_history_snapshot", lambda orch, ns, limit: ("claude", [])
        )
        monkeypatch.setattr(
            api.notify,
            "lead_mirror_diagnosis",
            lambda orch, ns: {"code": None, "provider": "claude"},
        )
        result = api.lead_history(self._Orch(), "proj-a")
        assert result == {
            "project": "proj-a",
            "provider": "claude",
            "messages": [],
            "working": False,
            "lead_provider_note": None,
            "empty_reason": None,
        }

    def test_reads_recent_messages_oldest_first_with_kind_field(self, monkeypatch):
        monkeypatch.setattr(
            api.notify,
            "lead_history_snapshot",
            lambda orch, ns, limit: (
                "claude",
                [
                    {"text": "first", "kind": "me"},
                    {"text": "second", "kind": "lead"},
                ],
            ),
        )
        result = api.lead_history(self._Orch(), "proj-a", limit=2)
        assert result == {
            "project": "proj-a",
            "provider": "claude",
            "messages": [
                {"text": "first", "kind": "me"},
                {"text": "second", "kind": "lead"},
            ],
            "working": False,
            "lead_provider_note": None,
            "empty_reason": None,
        }

    def test_reports_current_lead_pane_working_state(self, monkeypatch):
        monkeypatch.setattr(
            api.notify, "lead_history_snapshot", lambda orch, ns, limit: ("claude", [])
        )
        monkeypatch.setattr(
            api.notify,
            "lead_mirror_diagnosis",
            lambda orch, ns: {"code": None, "provider": "claude"},
        )
        orch = _FakeOrchWithPanes({"proj-a": {"lead": _FakePane("working", None)}})

        assert api.lead_history(orch, "proj-a")["working"] is True

    def test_provider_note_set_for_provider_without_saved_history(self, monkeypatch):
        """A provider using the live visible-screen fallback still explains
        that saved history/session browsing is unavailable."""
        monkeypatch.setattr(
            api.notify, "lead_history_snapshot", lambda orch, ns, limit: ("opencode", [])
        )
        monkeypatch.setattr(
            api.notify,
            "lead_mirror_diagnosis",
            lambda orch, ns: {"code": "provider_unsupported", "provider": "opencode"},
        )
        result = api.lead_history(self._Orch(), "proj-a")
        assert result["provider"] == "opencode"
        assert result["messages"] == []
        assert result["lead_provider_note"] is not None
        assert "opencode" in result["lead_provider_note"]

    def test_empty_reason_omitted_when_messages_present(self, monkeypatch):
        """Diagnosis is skipped entirely for a populated chat — it must never
        run (and never override) once real messages came back."""
        called = []
        monkeypatch.setattr(
            api.notify,
            "lead_history_snapshot",
            lambda orch, ns, limit: ("claude", [{"text": "hi", "kind": "lead"}]),
        )
        monkeypatch.setattr(
            api.notify,
            "lead_mirror_diagnosis",
            lambda orch, ns: (
                called.append(1) or {"code": "transcript_missing", "provider": "claude"}
            ),
        )
        result = api.lead_history(self._Orch(), "proj-a")
        assert result["empty_reason"] is None
        assert called == []

    def test_empty_reason_no_session_uuid(self, monkeypatch):
        monkeypatch.setattr(
            api.notify, "lead_history_snapshot", lambda orch, ns, limit: ("claude", [])
        )
        monkeypatch.setattr(
            api.notify,
            "lead_mirror_diagnosis",
            lambda orch, ns: {"code": "no_session_uuid", "provider": "claude"},
        )
        result = api.lead_history(self._Orch(), "proj-a")
        assert result["empty_reason"]["code"] == "no_session_uuid"
        assert isinstance(result["empty_reason"]["text"], str) and result["empty_reason"]["text"]

    def test_empty_reason_transcript_missing_includes_short_uuid_not_full(self, monkeypatch):
        """Data-min: only an 8-char prefix may reach the phone, never the
        full session_uuid."""
        monkeypatch.setattr(
            api.notify, "lead_history_snapshot", lambda orch, ns, limit: ("claude", [])
        )
        monkeypatch.setattr(
            api.notify,
            "lead_mirror_diagnosis",
            lambda orch, ns: {
                "code": "transcript_missing",
                "provider": "claude",
                "session_uuid_short": "abc12345",
            },
        )
        result = api.lead_history(self._Orch(), "proj-a")
        assert result["empty_reason"]["code"] == "transcript_missing"
        assert "abc12345" in result["empty_reason"]["text"]
        assert "abc12345-full-uuid-should-never-appear" not in result["empty_reason"]["text"]

    def test_limit_defaults_to_200(self, monkeypatch):
        seen = {}

        def _fake_snapshot(orch, ns, limit):
            seen["limit"] = limit
            return "claude", []

        monkeypatch.setattr(api.notify, "lead_history_snapshot", _fake_snapshot)
        monkeypatch.setattr(
            api.notify,
            "lead_mirror_diagnosis",
            lambda orch, ns: {"code": None, "provider": "claude"},
        )
        api.lead_history(self._Orch(), "proj-a")
        assert seen["limit"] == 200

    def test_limit_is_clamped_to_the_max(self, monkeypatch):
        seen = {}

        def _fake_snapshot(orch, ns, limit):
            seen["limit"] = limit
            return "claude", []

        monkeypatch.setattr(api.notify, "lead_history_snapshot", _fake_snapshot)
        monkeypatch.setattr(
            api.notify,
            "lead_mirror_diagnosis",
            lambda orch, ns: {"code": None, "provider": "claude"},
        )
        api.lead_history(self._Orch(), "proj-a", limit=99999)
        assert seen["limit"] == 200

    def test_non_numeric_limit_falls_back_to_default(self, monkeypatch):
        seen = {}

        def _fake_snapshot(orch, ns, limit):
            seen["limit"] = limit
            return "claude", []

        monkeypatch.setattr(api.notify, "lead_history_snapshot", _fake_snapshot)
        api.lead_history(self._Orch(), "proj-a", limit="not-a-number")
        assert seen["limit"] == 200


class TestLeadSessions:
    class _Orch:
        pass

    def test_exposes_provider_and_clean_unsupported_state(self, monkeypatch):
        monkeypatch.setattr(
            api.notify, "lead_sessions_snapshot", lambda orch, ns, limit: ("opencode", [])
        )
        result = api.lead_sessions(self._Orch(), "proj-a")
        assert result == {
            "project": "proj-a",
            "provider": "opencode",
            "sessions": [],
            "lead_provider_note": "Lead provider = opencode — remote history/session unavailable",
        }

    def test_supported_provider_has_no_degradation_note(self, monkeypatch):
        sessions = [{"uuid": "one", "mtime": 1.0, "preview": "hello"}]
        monkeypatch.setattr(
            api.notify,
            "lead_sessions_snapshot",
            lambda orch, ns, limit: ("claude", sessions),
        )
        result = api.lead_sessions(self._Orch(), "proj-a")
        assert result["provider"] == "claude"
        assert result["sessions"] == sessions
        assert result["lead_provider_note"] is None


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
