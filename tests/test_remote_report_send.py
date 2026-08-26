"""Tests for #390 (`takkub report publish --send` / `takkub send --to user
--file`): pushing a just-published Remote Report as a native attachment
into the connected mobile PWA's live feed instead of only a link.

Layers covered end-to-end at the unit level (no live Qt event loop / real
socket needed for any of these):

  * `Orchestrator.push_report` — emits `reportShared`, never raises.
  * `remote.notify.LeadNotifier._on_report_shared` — turns that signal into
    an SSE `report` event, scoped per-project (H-A).
  * `cli_server.CliServer._dispatch`'s `report-send` cmd — Lead-only gate +
    routes to `orch.push_report`.
  * `cli.py`'s `_push_report_to_mobile` — the CLI-side IPC caller, and its
    fallback behaviour when the cockpit isn't reachable.
  * `cli.py`'s `cmd_send`'s `--to user --file` sugar — redirects to
    `cmd_report` instead of the pane-messaging IPC.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import cli
from agent_takkub import orchestrator as orch_mod
from agent_takkub.cli_server import CliServer
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.remote.notify import LeadNotifier

from ._qt_timer_leak_guard import stop_timers_after
from .test_remote_notify import _FakeBroadcaster, _FakeOrch


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    return app or QCoreApplication([])


@pytest.fixture(autouse=True)
def _stop_leaked_timers(monkeypatch):
    # Both LeadNotifier and CliServer start a QTimer unconditionally in
    # __init__ (#344/#345) — every instance built in this file otherwise
    # leaves it running for the rest of the pytest session.
    finalize_notifier = stop_timers_after(monkeypatch, LeadNotifier, "_timer")
    finalize_server = stop_timers_after(monkeypatch, CliServer, "shutdown_timers")
    yield
    finalize_notifier()
    finalize_server()


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
    with (
        patch.object(Orchestrator, "_start_hot_md_timer", lambda self: None, create=True),
        patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None),
        patch(
            "agent_takkub.orchestrator.Orchestrator._start_browser_mcps",
            lambda self: None,
            create=True,
        ),
    ):
        o = Orchestrator.__new__(Orchestrator)
        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
    return o


class TestPushReport:
    def test_emits_reportShared_with_resolved_project_and_payload(self, orch: Orchestrator):
        received: list[tuple[str, dict]] = []
        orch.reportShared.connect(lambda ns, payload: received.append((ns, payload)))
        ok, msg = orch.push_report(
            "status.html", "https://x/r/p/status.html?k=tok", "แผน", 1234, False, project="p"
        )
        assert ok is True
        assert "status.html" in msg
        assert received == [
            (
                "p",
                {
                    "name": "status.html",
                    "url": "https://x/r/p/status.html?k=tok",
                    "label": "แผน",
                    "size_bytes": 1234,
                    "attachment": False,
                },
            )
        ]

    def test_never_raises_with_no_project_given(self, orch: Orchestrator):
        ok, _msg = orch.push_report("a.zip", "https://x/a.zip", "", 0, True)
        assert ok is True


class TestLeadNotifierReportShared:
    def test_on_report_shared_pushes_to_broadcaster(self, qapp):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            payload = {
                "name": "status.html",
                "url": "https://x/r/demo/status.html?k=tok",
                "label": "",
                "size_bytes": 10,
                "attachment": False,
            }
            orch.reportShared.emit("demo", payload)
            assert broadcaster.events == [("report", payload, "demo")]
        finally:
            notifier.stop()

    def test_stop_disconnects_report_shared_without_raising(self, qapp):
        orch = _FakeOrch()
        notifier = LeadNotifier(orch, _FakeBroadcaster())
        notifier.stop()  # must not raise
        orch.reportShared.emit("demo", {})  # disconnected — no listener left to call


class TestCliServerReportSendDispatch:
    def _srv_sock(self):
        mock_orch = MagicMock()
        mock_orch._lead_token = "lead-tok-390"
        mock_orch.push_report.return_value = (True, "pushed")
        srv = CliServer(mock_orch)

        class _FakeSock:
            def __init__(self):
                self._buf = b""

            def write(self, data):
                self._buf += data

            def flush(self):
                pass

            def last_response(self):
                return json.loads(self._buf.split(b"\n", 1)[0].decode("utf-8"))

        return srv, _FakeSock(), mock_orch

    def test_report_send_requires_lead_role(self, qapp):
        srv, sock, mock_orch = self._srv_sock()
        srv._dispatch(
            sock,
            {"cmd": "report-send", "from": "backend", "name": "a.html", "url": "https://x"},
        )
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "only lead" in resp["msg"].lower()
        mock_orch.push_report.assert_not_called()
        srv.shutdown_timers()

    def test_report_send_requires_valid_lead_token(self, qapp):
        srv, sock, mock_orch = self._srv_sock()
        srv._dispatch(
            sock,
            {
                "cmd": "report-send",
                "from": "lead",
                "auth": "wrong-token",
                "name": "a.html",
                "url": "https://x",
            },
        )
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "unauthorized" in resp["msg"].lower()
        mock_orch.push_report.assert_not_called()
        srv.shutdown_timers()

    def test_report_send_routes_to_push_report(self, qapp):
        srv, sock, mock_orch = self._srv_sock()
        srv._dispatch(
            sock,
            {
                "cmd": "report-send",
                "from": "lead",
                "auth": "lead-tok-390",
                "name": "a.html",
                "url": "https://x/a.html",
                "label": "L",
                "size_bytes": 42,
                "attachment": True,
                "project": "proj-x",
            },
        )
        resp = sock.last_response()
        assert resp["ok"] is True
        mock_orch.push_report.assert_called_once_with(
            "a.html", "https://x/a.html", "L", 42, True, project="proj-x"
        )
        srv.shutdown_timers()

    def test_report_send_falls_back_to_from_project_when_no_explicit_project(self, qapp):
        srv, sock, mock_orch = self._srv_sock()
        srv._dispatch(
            sock,
            {
                "cmd": "report-send",
                "from": "lead",
                "auth": "lead-tok-390",
                "from_project": "auto-proj",
                "name": "a.html",
                "url": "https://x/a.html",
            },
        )
        mock_orch.push_report.assert_called_once_with(
            "a.html", "https://x/a.html", "", 0, False, project="auto-proj"
        )
        srv.shutdown_timers()


class TestPushReportToMobileHelper:
    def test_cockpit_not_running_falls_back_cleanly(self, monkeypatch):
        def _raise_connect(*_a, **_k):
            raise RuntimeError("agent-takkub cockpit is not running (no port file)")

        monkeypatch.setattr(cli, "_connect", _raise_connect)
        ok, msg = cli._push_report_to_mobile(
            name="a.html", url="https://x", label="", size_bytes=1, attachment=False, project=None
        )
        assert ok is False
        assert "cockpit" in msg.lower()

    def test_server_rejection_surfaces_its_message(self, monkeypatch):
        monkeypatch.setattr(cli, "_request", lambda payload, **kw: {"ok": False, "msg": "nope"})
        ok, msg = cli._push_report_to_mobile(
            name="a.html", url="https://x", label="", size_bytes=1, attachment=False, project=None
        )
        assert ok is False
        assert msg == "nope"

    def test_success_returns_server_message(self, monkeypatch):
        monkeypatch.setattr(
            cli, "_request", lambda payload, **kw: {"ok": True, "msg": "pushed to proj p"}
        )
        ok, msg = cli._push_report_to_mobile(
            name="a.html", url="https://x", label="", size_bytes=1, attachment=False, project="p"
        )
        assert ok is True
        assert msg == "pushed to proj p"


class TestCmdReportSendFlag:
    def _publish_args(self, tmp_path, *, send: bool):
        src = tmp_path / "status.html"
        src.write_text("<html>hi</html>", encoding="utf-8")
        return argparse.Namespace(
            report_action="publish",
            file=str(src),
            name=None,
            project="demo",
            expires=None,
            label="",
            attachment=False,
            send=send,
        )

    def test_skips_push_when_remote_disabled(self, tmp_path, monkeypatch):
        from agent_takkub.remote.config import RemoteConfig

        RemoteConfig(enabled=False).save()
        called = []
        monkeypatch.setattr(cli, "_push_report_to_mobile", lambda **kw: called.append(kw))
        resp = cli.cmd_report(self._publish_args(tmp_path, send=True))
        assert resp["ok"] is True
        assert "push: ข้าม" in resp["msg"]
        assert called == []

    def test_attempts_push_when_remote_enabled_and_tunnel_up(self, tmp_path, monkeypatch):
        from agent_takkub.remote import tunnel as tunnel_mod
        from agent_takkub.remote.config import RemoteConfig

        RemoteConfig(
            enabled=True, public_url="https://takkub.example.com", secret_path="sek"
        ).save()
        monkeypatch.setattr(tunnel_mod, "is_tunnel_alive", lambda: True)
        calls = []

        def _fake_push(**kw):
            calls.append(kw)
            return True, "pushed"

        monkeypatch.setattr(cli, "_push_report_to_mobile", _fake_push)
        resp = cli.cmd_report(self._publish_args(tmp_path, send=True))
        assert resp["ok"] is True
        assert "ส่งเข้ามือถือแล้ว" in resp["msg"]
        assert len(calls) == 1
        assert calls[0]["name"] == "status.html"

    def test_no_send_flag_never_calls_push_helper(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(cli, "_push_report_to_mobile", lambda **kw: called.append(kw))
        resp = cli.cmd_report(self._publish_args(tmp_path, send=False))
        assert resp["ok"] is True
        assert called == []
        assert "push:" not in resp["msg"]


class TestCmdSendUserFileAlias:
    def test_to_user_without_file_is_rejected(self):
        args = argparse.Namespace(to="user", msg="", file=None, project=None, attachment=False)
        resp = cli.cmd_send(args)
        assert resp["ok"] is False
        assert "--file" in resp["msg"]

    def test_to_user_with_file_redirects_to_cmd_report(self, monkeypatch, tmp_path):
        captured = {}

        def _fake_cmd_report(report_args):
            captured["args"] = report_args
            return {"ok": True, "msg": "published"}

        monkeypatch.setattr(cli, "cmd_report", _fake_cmd_report)
        src = tmp_path / "f.pdf"
        src.write_bytes(b"%PDF-1.4")
        args = argparse.Namespace(
            to="user", msg="a label", file=str(src), project="demo", attachment=True
        )
        resp = cli.cmd_send(args)
        assert resp == {"ok": True, "msg": "published"}
        report_args = captured["args"]
        assert report_args.report_action == "publish"
        assert report_args.file == str(src)
        assert report_args.label == "a label"
        assert report_args.attachment is True
        assert report_args.send is True
        assert report_args.project == "demo"

    def test_normal_send_without_msg_is_rejected(self):
        args = argparse.Namespace(to="backend", msg="", file=None, project=None, attachment=False)
        resp = cli.cmd_send(args)
        assert resp["ok"] is False
        assert "message" in resp["msg"].lower()

    def test_non_lead_role_is_rejected_before_publishing(self, monkeypatch, tmp_path):
        """Regression: `--to user --file` is sugar for `report publish
        --send`, which is Lead-only (`LEAD_ONLY_COMMANDS`). `main()`'s outer
        role gate only ever sees the top-level "send" subcommand (not
        "report") and would never catch this on its own — `cmd_send` must
        enforce the same gate itself before ever calling `cmd_report`, or
        any teammate pane could publish arbitrary local files to the
        reports store, something a direct `takkub report publish` could
        never do."""
        monkeypatch.setenv("TAKKUB_ROLE", "backend")
        called = []
        monkeypatch.setattr(cli, "cmd_report", lambda report_args: called.append(report_args))
        src = tmp_path / "f.txt"
        src.write_text("hi", encoding="utf-8")
        args = argparse.Namespace(to="user", msg="", file=str(src), project=None, attachment=False)
        resp = cli.cmd_send(args)
        assert resp["ok"] is False
        assert "only lead" in resp["msg"].lower()
        assert called == []

    def test_lead_role_is_allowed_through(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TAKKUB_ROLE", "lead")
        called = []
        monkeypatch.setattr(
            cli, "cmd_report", lambda report_args: called.append(report_args) or {"ok": True}
        )
        src = tmp_path / "f.txt"
        src.write_text("hi", encoding="utf-8")
        args = argparse.Namespace(to="user", msg="", file=str(src), project=None, attachment=False)
        resp = cli.cmd_send(args)
        assert resp == {"ok": True}
        assert len(called) == 1

    def test_normal_send_still_uses_pane_messaging_ipc(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            cli, "_request", lambda payload: captured.setdefault("payload", payload)
        )
        args = argparse.Namespace(
            to="backend", msg="hello", file=None, project=None, attachment=False
        )
        cli.cmd_send(args)
        assert captured["payload"]["cmd"] == "send"
        assert captured["payload"]["to"] == "backend"
        assert captured["payload"]["msg"] == "hello"
