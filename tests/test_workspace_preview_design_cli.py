"""`takkub preview`/`takkub design` CLI IPC (#365 phase 5).

Two layers, same split `test_cli.py`/`test_cli_server_auth.py` already use:
  * argparse -> request payload shape (cli.py, orchestrator mocked out via a
    fake `_request`)
  * `CliServer._dispatch()` round-trip against a fake orchestrator + fake
    socket (no real TCP, no Qt event loop needed)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import cli
from agent_takkub.cli_server import _LEAD_ONLY_CMDS, CliServer

from ._qt_timer_leak_guard import stop_timers_after


@pytest.fixture
def fake_request(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    def _fake(payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return {"ok": True, "msg": "stubbed"}

    monkeypatch.setattr(cli, "_request", _fake)
    monkeypatch.delenv("TAKKUB_ROLE", raising=False)
    return sent


class TestPreviewArgparse:
    def test_open_url(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["preview", "open-url", "http://127.0.0.1:3000"])
        payload = fake_request[-1]
        assert payload["cmd"] == "preview"
        assert payload["action"] == "open-url"
        assert payload["url"] == "http://127.0.0.1:3000"
        assert payload["path"] is None

    def test_open_file(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["preview", "open-file", "docs/design-review/dashboard.html"])
        payload = fake_request[-1]
        assert payload["action"] == "open-file"
        assert payload["path"] == "docs/design-review/dashboard.html"

    def test_open_url_with_device(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["preview", "open-url", "http://127.0.0.1:3000", "--device", "mobile"])
        assert fake_request[-1]["device"] == "mobile"

    def test_close(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["preview", "close"])
        assert fake_request[-1]["action"] == "close"

    def test_status(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["preview", "status"])
        assert fake_request[-1]["action"] == "status"

    def test_missing_action_errors(self, fake_request: list[dict[str, Any]]) -> None:
        with pytest.raises(SystemExit):
            cli.main(["preview"])


class TestDesignArgparse:
    def test_publish(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(
            [
                "design",
                "publish",
                "--path",
                "docs/design-review/dashboard.html",
                "--title",
                "Dashboard v2",
                "--mode",
                "html",
            ]
        )
        payload = fake_request[-1]
        assert payload["cmd"] == "design"
        assert payload["action"] == "publish"
        assert payload["path"] == "docs/design-review/dashboard.html"
        assert payload["title"] == "Dashboard v2"
        assert payload["mode"] == "html"

    def test_publish_defaults_mode_html(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["design", "publish", "--path", "x.html", "--title", "X"])
        assert fake_request[-1]["mode"] == "html"

    def test_approve(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["design", "approve", "--id", "abc123"])
        payload = fake_request[-1]
        assert payload["action"] == "approve"
        assert payload["artifact_id"] == "abc123"

    def test_revise(self, fake_request: list[dict[str, Any]]) -> None:
        cli.main(["design", "revise", "--id", "abc123", "--feedback", "move CTA up"])
        payload = fake_request[-1]
        assert payload["action"] == "revise"
        assert payload["artifact_id"] == "abc123"
        assert payload["feedback"] == "move CTA up"


# ── trust model: preview/design are NOT lead-only ────────────────────────


def test_preview_and_design_are_not_lead_only() -> None:
    assert "preview" not in cli.LEAD_ONLY_COMMANDS
    assert "design" not in cli.LEAD_ONLY_COMMANDS
    assert "preview" not in _LEAD_ONLY_CMDS
    assert "design" not in _LEAD_ONLY_CMDS


# ── CliServer._dispatch round-trip ───────────────────────────────────────


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture(autouse=True)
def _stop_cli_server_reapers(monkeypatch):
    finalize = stop_timers_after(monkeypatch, CliServer, "shutdown_timers")
    yield
    finalize()


class _FakeSock:
    def __init__(self) -> None:
        self._buf = b""

    def write(self, data: bytes) -> None:
        self._buf += data

    def flush(self) -> None:
        pass

    def last_response(self) -> dict:
        line = self._buf.split(b"\n", 1)[0]
        return json.loads(line.decode("utf-8"))


_PANE_TOKEN_DESIGNER = "test-pane-token-designer-abc"


@pytest.fixture
def server_and_sock(qapp: QCoreApplication):
    mock_orch = MagicMock()
    mock_orch._lead_token = "lead-token"
    # Pre-registered pane token bound to project "demo" — tests exercise both
    # the token-derived-identity path and the reject-with-no/wrong-token path.
    mock_orch._pane_tokens = {_PANE_TOKEN_DESIGNER: ("demo", "designer")}
    server = CliServer(mock_orch)
    return server, _FakeSock(), mock_orch


class TestPreviewDispatch:
    def test_open_url_round_trip(self, server_and_sock) -> None:
        server, sock, mock_orch = server_and_sock
        mock_orch.preview_command.return_value = (
            True,
            "preview open url: url http://127.0.0.1:3000",
            {
                "project": "demo",
                "mode": "url",
                "target": "http://127.0.0.1:3000",
                "device": "desktop",
                "approved": False,
            },
        )

        server._dispatch(
            sock,
            {
                "cmd": "preview",
                "action": "open-url",
                "url": "http://127.0.0.1:3000",
                "auth": _PANE_TOKEN_DESIGNER,
            },
        )

        mock_orch.preview_command.assert_called_once_with(
            "open-url", project="demo", url="http://127.0.0.1:3000", path=None, device=None
        )
        resp = sock.last_response()
        assert resp["ok"] is True
        assert resp["mode"] == "url"
        assert resp["target"] == "http://127.0.0.1:3000"

    def test_status_round_trip(self, server_and_sock) -> None:
        server, sock, mock_orch = server_and_sock
        mock_orch.preview_command.return_value = (True, "no open preview", {"open": False})

        server._dispatch(sock, {"cmd": "preview", "action": "status", "from_project": "demo"})

        mock_orch.preview_command.assert_called_once_with(
            "status", project="demo", url=None, path=None, device=None
        )
        resp = sock.last_response()
        assert resp["ok"] is True
        assert resp["open"] is False

    def test_status_requires_no_token(self, server_and_sock) -> None:
        """`preview status` stays at the same trust-local tier as
        `list`/`ram-status` — read-only, no pane/Lead token needed."""
        server, sock, mock_orch = server_and_sock
        mock_orch.preview_command.return_value = (True, "no open preview", {"open": False})

        server._dispatch(sock, {"cmd": "preview", "action": "status"})

        resp = sock.last_response()
        assert resp["ok"] is True

    def test_rejection_surfaces_as_not_ok(self, server_and_sock) -> None:
        server, sock, mock_orch = server_and_sock
        mock_orch.preview_command.return_value = (
            False,
            "preview URL must be loopback (127.0.0.1/localhost/::1) + port: 'http://evil.example.com'",
            {},
        )

        server._dispatch(
            sock,
            {
                "cmd": "preview",
                "action": "open-url",
                "url": "http://evil.example.com",
                "auth": _PANE_TOKEN_DESIGNER,
            },
        )

        resp = sock.last_response()
        assert resp["ok"] is False
        assert "loopback" in resp["msg"]

    def test_mutating_action_rejected_with_no_token(self, server_and_sock) -> None:
        """#365 phase 3+5 review MUST-FIX: a caller with no `auth` token at
        all used to still reach the orchestrator for `open-url`/`open-file`/
        `close` — any local process could spoof `from_project` and drive
        another project's Preview. Only `status` stays trust-local."""
        server, sock, mock_orch = server_and_sock

        server._dispatch(sock, {"cmd": "preview", "action": "close", "from_project": "demo"})

        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()
        mock_orch.preview_command.assert_not_called()

    def test_cross_project_spoof_ignored_uses_token_project(self, server_and_sock) -> None:
        """A valid pane token always derives `project` from the token's own
        registration — a caller-supplied `from_project` claiming a different
        project is silently overridden, never trusted."""
        server, sock, mock_orch = server_and_sock
        mock_orch.preview_command.return_value = (True, "preview closed", {})

        server._dispatch(
            sock,
            {
                "cmd": "preview",
                "action": "close",
                "from_project": "someone-elses-project",
                "auth": _PANE_TOKEN_DESIGNER,
            },
        )

        mock_orch.preview_command.assert_called_once_with(
            "close", project="demo", url=None, path=None, device=None
        )
        resp = sock.last_response()
        assert resp["ok"] is True

    def test_lead_token_allowed_with_caller_supplied_project(self, server_and_sock) -> None:
        server, sock, mock_orch = server_and_sock
        mock_orch.preview_command.return_value = (True, "preview closed", {})

        server._dispatch(
            sock,
            {"cmd": "preview", "action": "close", "from_project": "demo", "auth": "lead-token"},
        )

        mock_orch.preview_command.assert_called_once_with(
            "close", project="demo", url=None, path=None, device=None
        )
        resp = sock.last_response()
        assert resp["ok"] is True


class TestDesignDispatch:
    def test_publish_round_trip(self, server_and_sock) -> None:
        server, sock, mock_orch = server_and_sock
        mock_orch.design_publish.return_value = (
            True,
            "published design artifact a1",
            {
                "artifact_id": "a1",
                "project_id": "demo",
                "title": "Dashboard v2",
                "kind": "html",
                "target": "x.html",
                "status": "draft",
                "created_by_role": None,
                "created_at": "2026-08-23T00:00:00",
                "metadata": {},
            },
        )

        server._dispatch(
            sock,
            {
                "cmd": "design",
                "action": "publish",
                "path": "x.html",
                "title": "Dashboard v2",
                "mode": "html",
                "from_project": "demo",
                "auth": "lead-token",
            },
        )

        mock_orch.design_publish.assert_called_once_with(
            "demo", "x.html", "Dashboard v2", "html", from_role=None
        )
        resp = sock.last_response()
        assert resp["ok"] is True
        # nested, never flattened — a top-level "status" key would collide
        # with cli.py main()'s generic pane-status-dict printer
        assert resp["artifact"]["artifact_id"] == "a1"
        assert resp["artifact"]["status"] == "draft"

    def test_approve_round_trip(self, server_and_sock) -> None:
        server, sock, mock_orch = server_and_sock
        mock_orch.design_approve.return_value = (
            True,
            "artifact a1 approved",
            {"artifact_id": "a1", "status": "approved"},
        )

        server._dispatch(
            sock,
            {
                "cmd": "design",
                "action": "approve",
                "artifact_id": "a1",
                "auth": _PANE_TOKEN_DESIGNER,
            },
        )

        # project derived from the pane token ("demo"), not a caller-supplied
        # from_project (there wasn't one here).
        mock_orch.design_approve.assert_called_once_with("demo", "a1")
        resp = sock.last_response()
        assert resp["artifact"]["status"] == "approved"

    def test_publish_rejected_with_no_token(self, server_and_sock) -> None:
        """#365 phase 3+5 review MUST-FIX: `design` used to trust
        caller-supplied `from_project` outright — any local process could
        publish/approve/revise a design artifact under a spoofed project."""
        server, sock, mock_orch = server_and_sock

        server._dispatch(
            sock,
            {
                "cmd": "design",
                "action": "publish",
                "path": "x.html",
                "title": "Dashboard v2",
                "mode": "html",
                "from_project": "someone-elses-project",
            },
        )

        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()
        mock_orch.design_publish.assert_not_called()

    def test_approve_cross_project_spoof_ignored(self, server_and_sock) -> None:
        server, sock, mock_orch = server_and_sock
        mock_orch.design_approve.return_value = (
            True,
            "artifact a1 approved",
            {"artifact_id": "a1", "status": "approved"},
        )

        server._dispatch(
            sock,
            {
                "cmd": "design",
                "action": "approve",
                "artifact_id": "a1",
                "from_project": "someone-elses-project",
                "auth": _PANE_TOKEN_DESIGNER,
            },
        )

        mock_orch.design_approve.assert_called_once_with("demo", "a1")

    def test_revise_round_trip(self, server_and_sock) -> None:
        server, sock, mock_orch = server_and_sock
        mock_orch.design_revise.return_value = (
            True,
            "revision requested for artifact a1",
            {"artifact_id": "a1", "status": "revision_requested"},
        )

        server._dispatch(
            sock,
            {
                "cmd": "design",
                "action": "revise",
                "artifact_id": "a1",
                "feedback": "move CTA up",
                "auth": _PANE_TOKEN_DESIGNER,
            },
        )

        mock_orch.design_revise.assert_called_once_with("demo", "a1", feedback="move CTA up")
        resp = sock.last_response()
        assert resp["artifact"]["status"] == "revision_requested"

    def test_unknown_action_rejected(self, server_and_sock) -> None:
        server, sock, mock_orch = server_and_sock

        server._dispatch(
            sock,
            {
                "cmd": "design",
                "action": "delete",
                "artifact_id": "a1",
                "auth": _PANE_TOKEN_DESIGNER,
            },
        )

        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unknown design action" in resp["msg"]
        mock_orch.design_publish.assert_not_called()
        mock_orch.design_approve.assert_not_called()
        mock_orch.design_revise.assert_not_called()
