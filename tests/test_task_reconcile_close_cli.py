"""Issue #166 CLI surface: `takkub task reconcile [--dry-run]` and
`takkub task close --role <r> [--force] [--dry-run]`.

Covers: both subcommands are lead-only (teammate blocked before any IPC
call, `task show` stays open), the request payload cli.py builds for each,
and exit codes on success/failure.
"""

from __future__ import annotations

import pytest

from agent_takkub import cli


def _clear_role_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("TAKKUB_ROLE", "TAKKUB_PROJECT"):
        monkeypatch.delenv(key, raising=False)


class TestLeadGate:
    @pytest.mark.parametrize(
        "argv",
        [
            ["task", "reconcile"],
            ["task", "reconcile", "--dry-run"],
            ["task", "close", "--role", "backend"],
        ],
    )
    def test_teammate_is_blocked_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, argv: list[str]
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        called = []
        monkeypatch.setattr(cli, "_request", lambda payload: called.append(payload) or {})

        code = cli.main(argv)

        assert code == 1
        assert called == [], "must never reach the orchestrator for a non-lead caller"
        assert "only lead" in capsys.readouterr().out

    def test_lead_role_reaches_request(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        monkeypatch.setattr(
            cli, "_request", lambda payload: {"ok": True, "msg": "no orphaned rows to reconcile"}
        )

        code = cli.main(["task", "reconcile"])

        assert code == 0
        assert "no orphaned rows" in capsys.readouterr().out

    def test_task_show_stays_open_to_teammates(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        monkeypatch.setattr(
            cli, "_request", lambda payload: {"ok": True, "task": "do X", "task_file": None}
        )

        code = cli.main(["task", "show", "--role", "backend"])

        assert code == 0
        assert "only lead" not in capsys.readouterr().out


class TestRequestPayloads:
    def test_reconcile_dry_run_flag_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        captured = {}
        monkeypatch.setattr(
            cli,
            "_request",
            lambda payload: captured.update(payload) or {"ok": True, "msg": ""},
        )

        cli.main(["task", "reconcile", "--dry-run"])

        assert captured["cmd"] == "task-reconcile"
        assert captured["dry_run"] is True

    def test_close_role_force_and_dry_run_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        captured = {}
        monkeypatch.setattr(
            cli,
            "_request",
            lambda payload: captured.update(payload) or {"ok": True, "msg": ""},
        )

        cli.main(["task", "close", "--role", "backend", "--force", "--dry-run"])

        assert captured["cmd"] == "task-close"
        assert captured["role"] == "backend"
        assert captured["force"] is True
        assert captured["dry_run"] is True

    def test_close_failure_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_role_env(monkeypatch)
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        monkeypatch.setattr(
            cli,
            "_request",
            lambda payload: {"ok": False, "msg": "'backend' has a live pane right now"},
        )

        code = cli.main(["task", "close", "--role", "backend"])

        assert code == 1
