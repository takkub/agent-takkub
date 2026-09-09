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
    yield srv, sock, mock_orch
    # #345: CliServer.__init__ starts _reaper/_spawn_health unconditionally —
    # stop them (and any pending spawn-stagger timer) so they don't outlive
    # this test as a leaked, still-active QTimer.
    srv.shutdown_timers()


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


class TestStatusExitedPane:
    """Issue #541 & #542: status shows transcript path and exit hint for exited panes, and cleans spinner progress."""

    def test_status_shows_transcript_and_exit_hint_for_exited_pane(
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
                        "codex": {
                            "state": "exited",
                            "display_state": "exited",
                            "stall_minutes": None,
                            "last_progress_ts": 0.0,
                            "last_progress_human": "5m ago",
                            "last_progress_abs": "10:05:12",
                            "transcript_path": "C:/runtime/sessions/2026-09-09/myproj/codex-100512.transcript.log",
                            "exit_hint": "unauthorized error\nprocess exited with code 1",
                            "transcript_tail": "unauthorized error\nprocess exited with code 1",
                        }
                    },
                },
            }

        monkeypatch.setattr(cli, "_request", _fake)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        rc = cli.main(["status"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "[codex] exited" in out
        assert (
            "transcript: C:/runtime/sessions/2026-09-09/myproj/codex-100512.transcript.log" in out
        )
        assert "exit hint:" in out
        assert "process exited with code 1" in out

    def test_status_cleans_spinner_progress_line(
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
                        "codex": {
                            "state": "working",
                            "display_state": "working",
                            "stall_minutes": None,
                            "last_progress_ts": 0.0,
                            "last_progress_human": "1s ago",
                            "last_progress_abs": "10:11:05",
                            "transcript_tail": "W  Wo •Wor  •Work  •Worki  Workin •Working  •Working 7 •Working  •Working orking •",
                        }
                    },
                },
            }

        monkeypatch.setattr(cli, "_request", _fake)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        rc = cli.main(["status"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "•Working 7" in out
        # progressive fragments should not be printed as raw pollution
        assert "•Worki " not in out
        assert "•Wor " not in out
        assert " Workin " not in out


class TestTailCommand:
    """Issue #541: takkub tail --role <r> [--lines N]."""

    def test_tail_sends_default_20_lines(self, fake_request: list[dict]) -> None:
        cli.main(["tail", "--role", "codex"])
        payload = fake_request[-1]
        assert payload["cmd"] == "tail"
        assert payload["role"] == "codex"
        assert payload["lines"] == 20

    def test_tail_sends_custom_lines(self, fake_request: list[dict]) -> None:
        cli.main(["tail", "--role", "qa", "--lines", "50"])
        payload = fake_request[-1]
        assert payload["cmd"] == "tail"
        assert payload["role"] == "qa"
        assert payload["lines"] == 50

    def test_tail_prints_path_and_lines(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        def _fake(payload: dict) -> dict:
            return {
                "ok": True,
                "msg": "ok",
                "path": "C:/sessions/codex-120000.transcript.log",
                "lines": ["line 1", "line 2", "error: died"],
            }

        monkeypatch.setattr(cli, "_request", _fake)
        monkeypatch.delenv("TAKKUB_ROLE", raising=False)
        rc = cli.main(["tail", "--role", "codex"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "[codex] C:/sessions/codex-120000.transcript.log" in out
        assert "line 1" in out
        assert "error: died" in out

    def test_tail_role_gate_blocks_teammate(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "frontend")
        rc = cli.main(["tail", "--role", "backend"])
        err = capsys.readouterr().err
        assert rc == 1
        assert "only lead can run" in err

    def test_tail_role_gate_allows_lead(
        self, monkeypatch: pytest.MonkeyPatch, fake_request: list[dict]
    ) -> None:
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        rc = cli.main(["tail", "--role", "backend"])
        assert rc == 0
        assert fake_request[-1]["cmd"] == "tail"


class TestCliServerTail:
    """Issue #541: CliServer tail dispatch."""

    def test_tail_dispatch_success(self, srv_sock) -> None:
        srv, sock, mock_orch = srv_sock
        mock_orch.tail_role_transcript.return_value = (
            True,
            "ok",
            {"path": "/p/t.log", "lines": ["out 1", "out 2"]},
        )
        req = {"cmd": "tail", "role": "codex", "lines": 10, "from": "lead", "auth": "tok"}
        srv._dispatch(sock, req)
        resp = sock.last_response()
        assert resp["ok"] is True
        assert resp["path"] == "/p/t.log"
        assert resp["lines"] == ["out 1", "out 2"]
        mock_orch.tail_role_transcript.assert_called_once_with("codex", project=None, lines=10)

    def test_tail_dispatch_rejects_teammate(self, srv_sock) -> None:
        srv, sock, _mock_orch = srv_sock
        req = {"cmd": "tail", "role": "codex", "from": "backend"}
        srv._dispatch(sock, req)
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "role gate" in resp["msg"]
