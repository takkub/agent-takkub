"""Tests for `takkub session-report` (cli.cmd_session_report) — the
SessionStart hook command wired into every claude-backed pane
(hook_wiring.py), fixing session_uuid drift on manual /resume.

Matrix covered: role present/absent, missing/malformed session_id, and the
mandatory fail-open behaviour (never raises, never emits stray stdout) on
any transport error.
"""

from __future__ import annotations

import io
import json

import pytest

from agent_takkub import cli


def _run(monkeypatch: pytest.MonkeyPatch, stdin_payload: dict | str, **env) -> None:
    if isinstance(stdin_payload, dict):
        stdin_text = json.dumps(stdin_payload)
    else:
        stdin_text = stdin_payload
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(stdin_text))
    for key in ("TAKKUB_ROLE", "TAKKUB_PROJECT"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)


class TestNoRoleEnv:
    def test_manual_invocation_is_a_noop(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _run(monkeypatch, {"hook_event_name": "SessionStart", "session_id": "abc"})
        calls: list[dict] = []
        monkeypatch.setattr(cli, "_hook_request", lambda p, **kw: calls.append(p) or None)

        resp = cli.cmd_session_report(None)

        assert resp == {"ok": True, "msg": ""}
        assert calls == []  # never even contacted the orchestrator
        assert capsys.readouterr().out == ""


class TestReportsSessionId:
    def test_forwards_session_id_source_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _run(
            monkeypatch,
            {
                "hook_event_name": "SessionStart",
                "session_id": "11111111-1111-4111-8111-111111111111",
                "source": "resume",
                "cwd": "/proj",
            },
            TAKKUB_ROLE="backend",
            TAKKUB_PROJECT="myproj",
        )
        calls: list[dict] = []
        monkeypatch.setattr(cli, "_hook_request", lambda p, **kw: calls.append(p) or {"ok": True})

        resp = cli.cmd_session_report(None)

        assert resp == {"ok": True, "msg": ""}
        assert len(calls) == 1
        payload = calls[0]
        assert payload["cmd"] == "session-report"
        assert payload["session_id"] == "11111111-1111-4111-8111-111111111111"
        assert payload["source"] == "resume"
        assert payload["cwd"] == "/proj"
        assert payload["from"] == "backend"
        assert payload["from_project"] == "myproj"

    def test_fires_on_startup_source_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Idempotent no-op case: startup source with the same uuid the
        # orchestrator already stamped at spawn time — still reported
        # (harmless; the CLI has no way to know it's a repeat).
        _run(
            monkeypatch,
            {"hook_event_name": "SessionStart", "session_id": "xyz", "source": "startup"},
            TAKKUB_ROLE="qa",
        )
        calls: list[dict] = []
        monkeypatch.setattr(cli, "_hook_request", lambda p, **kw: calls.append(p) or None)

        resp = cli.cmd_session_report(None)

        assert resp == {"ok": True, "msg": ""}
        assert calls[0]["source"] == "startup"


class TestMissingSessionId:
    def test_empty_session_id_never_contacts_server(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _run(monkeypatch, {"hook_event_name": "SessionStart"}, TAKKUB_ROLE="backend")
        calls: list[dict] = []
        monkeypatch.setattr(cli, "_hook_request", lambda p, **kw: calls.append(p) or None)

        resp = cli.cmd_session_report(None)

        assert resp == {"ok": True, "msg": ""}
        assert calls == []
        assert capsys.readouterr().out == ""


class TestFailOpen:
    def test_malformed_stdin_json_is_treated_as_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _run(monkeypatch, "{not valid json", TAKKUB_ROLE="backend")
        calls: list[dict] = []
        monkeypatch.setattr(cli, "_hook_request", lambda p, **kw: calls.append(p) or None)

        resp = cli.cmd_session_report(None)

        assert resp == {"ok": True, "msg": ""}
        assert calls == []  # no session_id recovered from bad JSON

    def test_hook_request_exception_never_propagates(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        def _boom(payload, **kw):
            raise RuntimeError("socket exploded")

        _run(
            monkeypatch,
            {"hook_event_name": "SessionStart", "session_id": "abc"},
            TAKKUB_ROLE="backend",
        )
        monkeypatch.setattr(cli, "_hook_request", _boom)

        resp = cli.cmd_session_report(None)

        assert resp == {"ok": True, "msg": ""}
        assert capsys.readouterr().out == ""

    def test_no_cockpit_running_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _run(
            monkeypatch,
            {"hook_event_name": "SessionStart", "session_id": "abc"},
            TAKKUB_ROLE="backend",
        )
        monkeypatch.setattr(cli, "_hook_request", lambda p, **kw: None)

        resp = cli.cmd_session_report(None)

        assert resp == {"ok": True, "msg": ""}

    def test_main_always_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end: `takkub session-report` via main() must exit 0 even
        when the orchestrator call blows up — a hook must never break the
        pane's session start."""
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({"session_id": "abc"})))
        monkeypatch.setenv("TAKKUB_ROLE", "backend")

        def _boom(payload, **kw):
            raise RuntimeError("nope")

        monkeypatch.setattr(cli, "_hook_request", _boom)

        code = cli.main(["session-report"])

        assert code == 0


class TestRoleGateAllowsEveryRole:
    """`session-report` is neither LEAD_ONLY nor TEAMMATE_ONLY — every pane
    (Lead included) fires SessionStart and must be able to report it."""

    def test_gate_passes_with_no_role_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        assert cli._enforce_role_gate("session-report") is None

    @pytest.mark.parametrize("role", ["lead", "backend", "frontend", "qa", "devops"])
    def test_gate_passes_for_every_role(self, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", role)
        assert cli._enforce_role_gate("session-report") is None
