"""#341 — a write-path command must never exit 0 on an unconfirmed response.

Root cause of the original incident was one level below this (a clobbered
venv python.exe meant the CLI process never started at all — fixed in the
npm/bin launchers and bin/takkub.cmd/bin/takkub). This is the defense-in-
depth guard inside cli.main(): if a write command's response ever comes back
`ok: True` with no confirmation message (a bug elsewhere, or a degenerate
response), it must read as a failure, not a silent success.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_takkub import cli


@pytest.fixture
def no_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAKKUB_ROLE", raising=False)


class TestWriteCommandsRequireConfirmation:
    def test_send_with_empty_msg_and_ok_true_fails(
        self, monkeypatch: pytest.MonkeyPatch, no_role: None, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(cli, "_request", lambda payload: {"ok": True, "msg": ""})

        rc = cli.main(["send", "--to", "backend", "hello"])

        assert rc != 0
        out = capsys.readouterr().out
        assert "no confirmation message" in out

    def test_send_with_missing_msg_key_and_ok_true_fails(
        self, monkeypatch: pytest.MonkeyPatch, no_role: None
    ) -> None:
        monkeypatch.setattr(cli, "_request", lambda payload: {"ok": True})

        rc = cli.main(["send", "--to", "backend", "hello"])

        assert rc != 0

    def test_send_with_real_confirmation_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, no_role: None
    ) -> None:
        monkeypatch.setattr(
            cli, "_request", lambda payload: {"ok": True, "msg": "delivered to backend"}
        )

        rc = cli.main(["send", "--to", "backend", "hello"])

        assert rc == 0

    def test_done_with_empty_msg_and_ok_true_fails(
        self, monkeypatch: pytest.MonkeyPatch, no_role: None
    ) -> None:
        monkeypatch.setattr(cli, "_request", lambda payload: {"ok": True, "msg": ""})

        rc = cli.main(["done", "finished"])

        assert rc != 0

    def test_quiet_success_is_not_affected(
        self, monkeypatch: pytest.MonkeyPatch, no_role: None
    ) -> None:
        """A command that explicitly opts into a quiet success (e.g. `ma`)
        must keep exiting 0 with no message — the guard only targets the
        listed write-path commands, not every quiet response."""

        def _fake(payload: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "msg": "", "quiet": True}

        monkeypatch.setattr(cli, "_request", _fake)

        rc = cli.main(["status"])

        assert rc == 0

    def test_read_only_command_unaffected_by_empty_msg(
        self, monkeypatch: pytest.MonkeyPatch, no_role: None
    ) -> None:
        """`status` isn't a write-path command — an empty msg there is normal
        (list/status responses are rendered from `report`/`status`, not `msg`)."""
        monkeypatch.setattr(cli, "_request", lambda payload: {"ok": True, "msg": ""})

        rc = cli.main(["status"])

        assert rc == 0
