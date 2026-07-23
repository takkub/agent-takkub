"""Tests for `takkub _guard` (cli.cmd_guard) — the PreToolUse/Bash hook wired
into every claude-backed pane (hook_wiring.py).

`pane_guard.py` owns the *rules* (tests/test_pane_guard.py); this file owns the
*wiring*: reading the hook's stdin payload, resolving the caller's role from
`TAKKUB_ROLE`, and reporting a denial the way Claude Code actually understands
(exit code 2 + reason on stderr).

The fail-open contract is the important half. This hook fires on **every** Bash
call in every pane — a crash, a malformed payload, or an unexpected schema must
let the command through, never wedge the pane's shell.
"""

from __future__ import annotations

import io
import json

import pytest

from agent_takkub import cli


def _run(monkeypatch: pytest.MonkeyPatch, payload: dict | str, **env) -> None:
    stdin_text = json.dumps(payload) if isinstance(payload, dict) else payload
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(stdin_text))
    for key in ("TAKKUB_ROLE", "TAKKUB_PROJECT"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)


def _payload(command: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


class TestDeny:
    def test_blocks_npx_playwright_for_frontend(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _run(monkeypatch, _payload("npx --yes playwright"), TAKKUB_ROLE="frontend")

        resp = cli.cmd_guard(None)

        assert resp["exit_code"] == 2, "exit 2 is the PreToolUse blocking contract"
        err = capsys.readouterr().err
        assert "takkub guard" in err
        assert "browser_driver" in err
        assert "qa" in err, "the reason must name the hand-off, not just say no"

    def test_blocks_whole_disk_find(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _run(
            monkeypatch,
            _payload("find / -maxdepth 6 -iname playwright -type d"),
            TAKKUB_ROLE="backend",
        )

        resp = cli.cmd_guard(None)

        assert resp["exit_code"] == 2
        assert "disk_scan" in capsys.readouterr().err

    def test_shard_role_is_still_guarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _run(monkeypatch, _payload("npx playwright test"), TAKKUB_ROLE="frontend#3")
        assert cli.cmd_guard(None)["exit_code"] == 2


class TestAllow:
    def test_allows_ordinary_command(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _run(monkeypatch, _payload("npm run build"), TAKKUB_ROLE="frontend")

        resp = cli.cmd_guard(None)

        assert resp == {"ok": True, "msg": ""}
        assert "exit_code" not in resp
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""

    def test_allows_browser_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _run(monkeypatch, _payload("npx playwright test"), TAKKUB_ROLE="qa")
        assert cli.cmd_guard(None) == {"ok": True, "msg": ""}

    def test_lead_is_never_guarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _run(monkeypatch, _payload("npx --yes playwright"), TAKKUB_ROLE="lead")
        assert cli.cmd_guard(None) == {"ok": True, "msg": ""}


class TestFailOpen:
    def test_no_role_env_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A person running `takkub _guard` by hand is not a pane."""
        _run(monkeypatch, _payload("npx --yes playwright"))
        assert cli.cmd_guard(None) == {"ok": True, "msg": ""}

    @pytest.mark.parametrize(
        "payload",
        [
            "",  # empty stdin
            "not json at all",
            {},  # no tool_input
            {"tool_input": None},
            {"tool_input": "a string, not a dict"},
            {"tool_input": {}},  # no command key
            {"tool_input": {"command": None}},
        ],
    )
    def test_malformed_payload_allows(
        self, monkeypatch: pytest.MonkeyPatch, payload: dict | str
    ) -> None:
        _run(monkeypatch, payload, TAKKUB_ROLE="frontend")
        assert cli.cmd_guard(None) == {"ok": True, "msg": ""}

    def test_guard_exception_allows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the rule engine itself blows up, the shell keeps working."""
        from agent_takkub import pane_guard

        def boom(*_a, **_k):
            raise RuntimeError("regex engine on fire")

        monkeypatch.setattr(pane_guard, "classify", boom)
        _run(monkeypatch, _payload("npx --yes playwright"), TAKKUB_ROLE="frontend")

        assert cli.cmd_guard(None) == {"ok": True, "msg": ""}


class TestCliDispatch:
    def test_guard_is_registered_and_not_lead_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every teammate pane has to be able to run it — a lead-only gate
        would make the guard fail open for exactly the roles it exists for."""
        assert "_guard" not in cli.LEAD_ONLY_COMMANDS
        monkeypatch.setenv("TAKKUB_ROLE", "frontend")
        assert cli._enforce_role_gate("_guard") is None

    def test_main_returns_exit_code_2_on_deny(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end through argparse: the block has to survive dispatch."""
        _run(monkeypatch, _payload("npx --yes playwright"), TAKKUB_ROLE="frontend")
        assert cli.main(["_guard"]) == 2

    def test_main_returns_zero_when_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _run(monkeypatch, _payload("npm test"), TAKKUB_ROLE="frontend")
        assert cli.main(["_guard"]) == 0
