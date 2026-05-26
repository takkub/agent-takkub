"""Tests for `takkub list` stall indicator and `takkub status` command."""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_takkub import cli


@pytest.fixture
def fake_request(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture outbound payloads and stub the response."""
    sent: list[dict[str, Any]] = []

    def _fake(payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return {"ok": True, "msg": "stubbed"}

    monkeypatch.setattr(cli, "_request", _fake)
    monkeypatch.delenv("TAKKUB_ROLE", raising=False)
    return sent


class TestListStallDisplay:
    def test_list_shows_stalled_state(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """When the server returns a stalled state string, the CLI prints it."""

        def _fake(payload: dict) -> dict:
            return {
                "ok": True,
                "msg": "status",
                "status": {
                    "qa": "working (stalled 7m)",
                    "frontend": "active",
                },
            }

        monkeypatch.setattr(cli, "_request", _fake)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        rc = cli.main(["list"])
        out = capsys.readouterr().out
        assert "stalled 7m" in out
        assert "frontend" in out
        assert rc == 0

    def test_list_cmd_sent_correctly(self, fake_request: list[dict]) -> None:
        cli.main(["list"])
        assert fake_request[-1]["cmd"] == "list"


class TestStatusCommand:
    def test_status_sends_status_cmd(self, fake_request: list[dict]) -> None:
        cli.main(["status"])
        assert fake_request[-1]["cmd"] == "status"

    def test_status_with_since_sends_since(self, fake_request: list[dict]) -> None:
        cli.main(["status", "--since", "18:00"])
        payload = fake_request[-1]
        assert payload["cmd"] == "status"
        assert payload["since"] == "18:00"

    def test_status_without_since_has_no_since(self, fake_request: list[dict]) -> None:
        cli.main(["status"])
        assert "since" not in fake_request[-1]

    def test_status_report_prints_roles(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        def _fake(payload: dict) -> dict:
            return {
                "ok": True,
                "msg": "status report",
                "report": {
                    "project": "myproj",
                    "any_stalled": False,
                    "panes": {
                        "backend": {
                            "state": "working",
                            "stall_minutes": None,
                            "last_progress_ts": 0.0,
                            "last_progress_human": "2m ago",
                            "last_progress_abs": "18:42:00",
                            "transcript_tail": "line a\nline b",
                            "last_screenshot": "",
                            "done_events": [],
                        }
                    },
                },
            }

        monkeypatch.setattr(cli, "_request", _fake)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        rc = cli.main(["status"])
        out = capsys.readouterr().out
        assert "backend" in out
        assert "2m ago" in out
        assert rc == 0

    def test_status_exit_1_when_any_stalled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake(payload: dict) -> dict:
            return {
                "ok": True,
                "msg": "status report",
                "report": {
                    "project": "myproj",
                    "any_stalled": True,
                    "panes": {
                        "qa": {
                            "state": "working",
                            "stall_minutes": 9,
                            "last_progress_ts": 0.0,
                            "last_progress_human": "9m ago",
                            "last_progress_abs": "18:38:00",
                            "transcript_tail": "",
                            "last_screenshot": "",
                            "done_events": [],
                        }
                    },
                },
            }

        monkeypatch.setattr(cli, "_request", _fake)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        rc = cli.main(["status"])
        assert rc == 1  # any_stalled → exit 1

    def test_status_report_shows_stall_indicator(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        def _fake(payload: dict) -> dict:
            return {
                "ok": True,
                "msg": "status report",
                "report": {
                    "project": "myproj",
                    "any_stalled": True,
                    "panes": {
                        "qa": {
                            "state": "working",
                            "stall_minutes": 7,
                            "last_progress_ts": 0.0,
                            "last_progress_human": "7m ago",
                            "last_progress_abs": "18:41:00",
                            "transcript_tail": "",
                            "last_screenshot": "/runtime/exports/2026-05-26/myproj/screenshots/s1-10.png",
                            "done_events": ["qa-181000.md"],
                        }
                    },
                },
            }

        monkeypatch.setattr(cli, "_request", _fake)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        cli.main(["status"])
        out = capsys.readouterr().out
        assert "stalled 7m" in out
        assert "s1-10.png" in out
        assert "qa-181000.md" in out


class _FakeSock:
    """Minimal socket stub that captures written bytes."""

    def __init__(self) -> None:
        self._buf = b""

    def write(self, data: bytes) -> None:
        self._buf += data

    def flush(self) -> None:
        pass

    def last_response(self) -> dict:
        line = self._buf.split(b"\n", 1)[0]
        return json.loads(line.decode("utf-8"))

    def reset(self) -> None:
        self._buf = b""


@pytest.fixture
def srv_sock(qapp):
    from agent_takkub.cli_server import CliServer

    mock_orch = MagicMock()
    mock_orch._lead_token = "tok"
    mock_orch.pane_status_report.return_value = {
        "project": "default",
        "any_stalled": False,
        "panes": {},
    }
    srv = CliServer(mock_orch)
    sock = _FakeSock()
    return srv, sock, mock_orch


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtCore import QCoreApplication

    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


class TestSinceFutureTimeWraps:
    def test_future_since_wraps_to_previous_day(self, srv_sock) -> None:
        """--since HH:MM in the future must resolve to the same HH:MM yesterday."""
        srv, sock, mock_orch = srv_sock
        req = {"cmd": "status", "from": "backend", "since": "23:59"}
        srv._dispatch(sock, req)
        call_args = mock_orch.pane_status_report.call_args
        since_ts_used = call_args.kwargs.get("since_ts") or call_args[1].get("since_ts")
        assert since_ts_used is not None
        assert since_ts_used <= time.time(), (
            "since_ts must be in the past when HH:MM resolves to a future time"
        )
