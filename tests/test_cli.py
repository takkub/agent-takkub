"""Tests for the `takkub` CLI argument parsing (offline — orchestrator is mocked)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_takkub import cli


@pytest.fixture
def fake_request(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the JSON payloads the CLI would send to the orchestrator."""
    sent: list[dict[str, Any]] = []

    def _fake(payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return {"ok": True, "msg": "stubbed"}

    monkeypatch.setattr(cli, "_request", _fake)
    return sent


class TestArgparse:
    """Argument parsing → request payload shape. Every payload now carries a
    `from_project` field (None when the CLI runs outside a cockpit-spawned
    pane); tests only assert on the fields the CLI actively populates."""

    def test_assign_requires_role_and_task(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["assign", "--role", "frontend", "make a thing"])
        payload = fake_request[-1]
        assert payload["cmd"] == "assign"
        assert payload["role"] == "frontend"
        assert payload["cwd"] is None
        assert payload["task"] == "make a thing"

    def test_assign_with_cwd(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["assign", "--role", "backend", "--cwd", "/x", "do work"])
        assert fake_request[-1]["cwd"] == "/x"

    def test_send_passes_from_role_env(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "frontend")
        cli.main(["send", "--to", "backend", "hi"])
        payload = fake_request[-1]
        assert payload["cmd"] == "send"
        assert payload["to"] == "backend"
        assert payload["msg"] == "hi"
        assert payload["from"] == "frontend"

    def test_send_without_env_passes_none_from(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        cli.main(["send", "--to", "backend", "hi"])
        assert fake_request[-1]["from"] is None

    def test_done_uses_env_role(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "qa")
        cli.main(["done", "tests passing"])
        payload = fake_request[-1]
        assert payload["cmd"] == "done"
        assert payload["from"] == "qa"
        assert payload["note"] == "tests passing"

    def test_done_without_note(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "qa")
        cli.main(["done"])
        assert fake_request[-1]["note"] == ""

    def test_list_command(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["list"])
        assert fake_request[-1]["cmd"] == "list"

    def test_close_all(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["close-all"])
        assert fake_request[-1]["cmd"] == "close-all"

    def test_close_role(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["close", "--role", "backend"])
        payload = fake_request[-1]
        assert payload["cmd"] == "close"
        assert payload["role"] == "backend"

    def test_spawn_optional_cwd(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["spawn", "--role", "frontend"])
        payload = fake_request[-1]
        assert payload["cmd"] == "spawn"
        assert payload["role"] == "frontend"
        assert payload["cwd"] is None

    def test_payload_includes_from_project_env(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_PROJECT", "unirecon")
        cli.main(["list"])
        assert fake_request[-1]["from_project"] == "unirecon"

    def test_payload_from_project_unset_is_none(
        self, fake_request: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_PROJECT", raising=False)
        cli.main(["list"])
        assert fake_request[-1]["from_project"] is None

    def test_gemini_one_shot_routes_to_helper(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `takkub gemini "<prompt>"` is pure-local (does NOT go through
        # the orchestrator socket). Mock gemini_exec and assert the CLI
        # routes the prompt + flags through correctly.
        seen: dict[str, object] = {}

        def fake_gemini_exec(prompt: str, *, cwd: str | None = None,
                             timeout: float = 120.0, model: str | None = None):
            seen["prompt"] = prompt
            seen["cwd"] = cwd
            seen["timeout"] = timeout
            seen["model"] = model
            return True, "gemini answered"

        from agent_takkub import gemini_helper
        monkeypatch.setattr(gemini_helper, "gemini_exec", fake_gemini_exec)
        rc = cli.main(["gemini", "review this approach"])
        assert rc == 0
        assert seen["prompt"] == "review this approach"
        assert seen["cwd"] is None
        assert seen["model"] is None
        out = capsys.readouterr().out
        assert "gemini answered" in out

    def test_gemini_forwards_cwd_and_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        def fake_gemini_exec(prompt: str, *, cwd: str | None = None,
                             timeout: float = 120.0, model: str | None = None):
            seen["cwd"] = cwd
            seen["model"] = model
            seen["timeout"] = timeout
            return True, ""

        from agent_takkub import gemini_helper
        monkeypatch.setattr(gemini_helper, "gemini_exec", fake_gemini_exec)
        cli.main([
            "gemini",
            "--cwd", "C:/x/proj",
            "--model", "gemini-2.5-pro",
            "--timeout", "30",
            "do thing",
        ])
        assert seen["cwd"] == "C:/x/proj"
        assert seen["model"] == "gemini-2.5-pro"
        assert seen["timeout"] == 30.0


class TestExitCodes:
    def test_ok_response_exit_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_request", lambda _p: {"ok": True, "msg": "done"})
        rc = cli.main(["list"])
        assert rc == 0

    def test_err_response_exit_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_request", lambda _p: {"ok": False, "msg": "no orchestrator"})
        rc = cli.main(["list"])
        assert rc == 1

    def test_status_payload_is_printed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            cli,
            "_request",
            lambda _p: {
                "ok": True,
                "msg": "status",
                "status": {"lead": "active", "frontend": "working"},
            },
        )
        cli.main(["list"])
        out = capsys.readouterr().out
        assert "lead" in out and "active" in out
        assert "frontend" in out and "working" in out


class TestRoleGate:
    """Lead-only commands (spawn/assign/close/close-all) must be blocked when
    invoked from a teammate pane. Prevents an agent that drifted into Lead
    behavior (e.g. after compaction at high context) from orchestrating."""

    def test_teammate_cannot_assign(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "devops")
        rc = cli.main(["assign", "--role", "devops", "--cwd", "/x", "self-assign attempt"])
        assert rc == 1
        assert fake_request == []  # never reached orchestrator
        err = capsys.readouterr().err
        assert "only lead" in err and "devops" in err

    def test_teammate_cannot_spawn(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "frontend")
        rc = cli.main(["spawn", "--role", "backend"])
        assert rc == 1
        assert fake_request == []

    def test_teammate_cannot_close(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "qa")
        rc = cli.main(["close", "--role", "frontend"])
        assert rc == 1
        assert fake_request == []

    def test_teammate_cannot_close_all(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "reviewer")
        rc = cli.main(["close-all"])
        assert rc == 1
        assert fake_request == []

    def test_lead_can_assign(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        rc = cli.main(["assign", "--role", "backend", "do work"])
        assert rc == 0
        assert fake_request[-1]["cmd"] == "assign"

    def test_unset_role_allows_everything(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """User running CLI manually from a terminal (no pane) must still work."""
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        rc = cli.main(["assign", "--role", "backend", "do work"])
        assert rc == 0
        assert fake_request[-1]["cmd"] == "assign"

    def test_teammate_can_send(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "devops")
        rc = cli.main(["send", "--to", "backend", "need env list"])
        assert rc == 0
        assert fake_request[-1]["cmd"] == "send"

    def test_teammate_can_done(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "devops")
        rc = cli.main(["done", "pipeline green"])
        assert rc == 0
        assert fake_request[-1]["cmd"] == "done"

    def test_teammate_can_list(
        self,
        fake_request: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "devops")
        rc = cli.main(["list"])
        assert rc == 0
        assert fake_request[-1]["cmd"] == "list"

    def test_teammate_can_run_gemini_one_shot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `gemini` is local — not in LEAD_ONLY_COMMANDS — so a teammate
        # pane can fire it for a second opinion mid-task.
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        from agent_takkub import gemini_helper
        monkeypatch.setattr(
            gemini_helper, "gemini_exec",
            lambda *_a, **_kw: (True, "answer"),
        )
        rc = cli.main(["gemini", "ping"])
        assert rc == 0


def test_request_payload_serialises_cleanly() -> None:
    """Smoke check: every payload we'd send is round-trippable JSON."""
    payload = {
        "cmd": "send",
        "to": "backend",
        "msg": "hi ภาษาไทย",
        "from": "frontend",
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert b"\xe0" in encoded  # Thai bytes survived
    assert json.loads(encoded.decode("utf-8")) == payload
