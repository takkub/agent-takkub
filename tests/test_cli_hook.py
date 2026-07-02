"""Tests for `takkub _hook` (cli.cmd_hook) — the Stop/Notification hook
command wired into every claude-backed pane (hook_wiring.py).

Matrix covered: role present/absent, stop_hook_active guard, and the
orchestrator's block/no-block decision, plus the mandatory fail-open
behaviour (never raises, never emits stray stdout) on any transport error.
"""

from __future__ import annotations

import io
import json

import pytest

from agent_takkub import cli


def _run_hook(monkeypatch: pytest.MonkeyPatch, stdin_payload: dict | str, **env) -> None:
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
        _run_hook(monkeypatch, {"hook_event_name": "Stop"})
        calls: list[dict] = []
        monkeypatch.setattr(cli, "_hook_request", lambda p, **kw: calls.append(p) or None)

        resp = cli.cmd_hook(None)

        assert resp == {"ok": True, "msg": ""}
        assert calls == []  # never even contacted the orchestrator
        assert capsys.readouterr().out == ""


class TestStopHookActiveGuard:
    def test_stop_hook_active_skips_server_contact(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _run_hook(
            monkeypatch,
            {"hook_event_name": "Stop", "stop_hook_active": True},
            TAKKUB_ROLE="backend",
        )
        calls: list[dict] = []
        monkeypatch.setattr(cli, "_hook_request", lambda p, **kw: calls.append(p) or None)

        resp = cli.cmd_hook(None)

        assert resp == {"ok": True, "msg": ""}
        assert calls == [], "must exit immediately, never re-contact the server"
        assert capsys.readouterr().out == ""


class TestBlockDecision:
    def test_block_true_emits_stop_hook_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _run_hook(monkeypatch, {"hook_event_name": "Stop"}, TAKKUB_ROLE="backend")
        monkeypatch.setattr(
            cli,
            "_hook_request",
            lambda p, **kw: {"ok": True, "block": True, "msg": "รายงานผลด้วย takkub done ก่อนจบ"},
        )

        resp = cli.cmd_hook(None)

        assert resp == {"ok": True, "msg": ""}
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert payload["decision"] == "block"
        assert payload["hookSpecificOutput"]["hookEventName"] == "Stop"
        assert "takkub done" in payload["hookSpecificOutput"]["additionalContext"]

    def test_block_false_emits_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _run_hook(monkeypatch, {"hook_event_name": "Stop"}, TAKKUB_ROLE="backend")
        monkeypatch.setattr(
            cli, "_hook_request", lambda p, **kw: {"ok": True, "block": False, "msg": ""}
        )

        resp = cli.cmd_hook(None)

        assert resp == {"ok": True, "msg": ""}
        assert capsys.readouterr().out == ""


class TestFailOpen:
    def test_no_cockpit_running_is_silent(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _run_hook(monkeypatch, {"hook_event_name": "Stop"}, TAKKUB_ROLE="backend")
        monkeypatch.setattr(cli, "_hook_request", lambda p, **kw: None)

        resp = cli.cmd_hook(None)

        assert resp == {"ok": True, "msg": ""}
        assert capsys.readouterr().out == ""

    def test_hook_request_exception_never_propagates(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        def _boom(payload, **kw):
            raise RuntimeError("socket exploded")

        _run_hook(monkeypatch, {"hook_event_name": "Stop"}, TAKKUB_ROLE="backend")
        monkeypatch.setattr(cli, "_hook_request", _boom)

        resp = cli.cmd_hook(None)

        assert resp == {"ok": True, "msg": ""}
        assert capsys.readouterr().out == ""

    def test_malformed_stdin_json_is_treated_as_empty(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _run_hook(monkeypatch, "{not valid json", TAKKUB_ROLE="backend")
        calls: list[dict] = []
        monkeypatch.setattr(
            cli, "_hook_request", lambda p, **kw: calls.append(p) or {"ok": True, "block": False}
        )

        resp = cli.cmd_hook(None)

        assert resp == {"ok": True, "msg": ""}
        assert calls[0]["event"] == ""  # no hook_event_name recovered from bad JSON

    def test_main_always_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end: `takkub _hook` via main() must exit 0 even when the
        orchestrator call blows up — a hook must never break the pane."""
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("{}"))
        monkeypatch.setenv("TAKKUB_ROLE", "backend")

        def _boom(payload, **kw):
            raise RuntimeError("nope")

        monkeypatch.setattr(cli, "_hook_request", _boom)

        code = cli.main(["_hook"])

        assert code == 0
