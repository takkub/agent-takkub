"""`takkub pm send --project X "<msg>"`: the PM pane hands work to project X's
Lead. Covers the cockpit-side gate (`CliServer._dispatch` → `_pm_send`) and
the CLI caller."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import cli, config
from agent_takkub.cli_server import PM_SEND_PREFIX, CliServer

from ._qt_timer_leak_guard import stop_timers_after

TOKEN = "lead-tok-pm"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture(autouse=True)
def _stop_leaked_timers(monkeypatch):
    finalize = stop_timers_after(monkeypatch, CliServer, "shutdown_timers")
    yield
    finalize()


@pytest.fixture(autouse=True)
def _projects(monkeypatch):
    monkeypatch.setattr(
        config,
        "load_projects",
        lambda: {"projects": {"PM": {"kind": "pm"}, "wash-locker": {}, "unirecon": {}}},
    )


class _FakeSock:
    def __init__(self):
        self._buf = b""

    def write(self, data):
        self._buf += data

    def flush(self):
        pass

    def last_response(self):
        return json.loads(self._buf.split(b"\n", 1)[0].decode("utf-8"))


def _server(lead_alive: bool = True):
    orch = MagicMock()
    orch._lead_token = TOKEN
    orch._pane_state = {}
    orch.send.return_value = (True, "sent")
    panes = {"lead": SimpleNamespace(session=SimpleNamespace(is_alive=True))} if lead_alive else {}
    orch._project_panes.return_value = panes
    return CliServer(orch), orch


def _req(**over):
    req = {
        "cmd": "pm-send",
        "from": "lead",
        "auth": TOKEN,
        "from_project": "PM",
        "project": "wash-locker",
        "msg": "รัน Jest ที่ค้างอยู่",
    }
    req.update(over)
    return req


def _dispatch(srv, req):
    sock = _FakeSock()
    srv._dispatch(sock, req)
    srv.shutdown_timers()
    return sock.last_response()


def test_delivers_to_target_lead_with_pm_label(qapp):
    srv, orch = _server()
    resp = _dispatch(srv, _req())
    assert resp["ok"] is True
    assert "wash-locker" in resp["msg"]
    orch.send.assert_called_once_with(
        "lead",
        msg=PM_SEND_PREFIX + "รัน Jest ที่ค้างอยู่",
        from_role="PM",
        project="wash-locker",
        kind="instruction",
    )


def test_closed_tab_reply_says_it_waits_for_the_tab(qapp):
    srv, orch = _server(lead_alive=False)
    resp = _dispatch(srv, _req())
    assert resp["ok"] is True
    assert "ยังไม่เปิด" in resp["msg"]
    assert "takkub assign" not in resp["msg"]
    orch.send.assert_called_once()


@pytest.mark.parametrize(
    "over",
    [
        {"from": "backend"},  # not a Lead
        {"auth": "forged"},  # Lead claim without the token
        {"from_project": "wash-locker"},  # an ordinary project's Lead
        {"project": "PM"},  # into the PM tab itself
        {"project": "no-such-project"},
        {"project": ""},
        {"msg": "   "},
    ],
)
def test_refused(qapp, over):
    srv, orch = _server()
    resp = _dispatch(srv, _req(**over))
    assert resp["ok"] is False
    orch.send.assert_not_called()


def test_cli_builds_pm_send_request(monkeypatch):
    captured = {}

    def fake_request(payload):
        captured.update(payload)
        return {"ok": True, "msg": "x"}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(cli, "_from_role", lambda: "lead")
    monkeypatch.setattr(cli, "_from_project", lambda: "PM")
    args = argparse.Namespace(
        pm_action="send", project="unirecon", msg="ปิด backlog", from_file=None
    )
    assert cli.cmd_pm(args)["ok"] is True
    assert captured["cmd"] == "pm-send"
    assert captured["project"] == "unirecon"
    assert captured["msg"] == "ปิด backlog"
    assert captured["from_project"] == "PM"


def test_cli_requires_a_message(monkeypatch):
    with patch.object(cli, "_request") as req:
        args = argparse.Namespace(pm_action="send", project="unirecon", msg=None, from_file=None)
        assert cli.cmd_pm(args)["ok"] is False
        req.assert_not_called()


def test_parser_accepts_pm_send():
    from agent_takkub.cli_parser import build_parser

    ns = build_parser(cli).parse_args(["pm", "send", "--project", "unirecon", "งาน"])
    assert ns.pm_action == "send" and ns.project == "unirecon" and ns.msg == "งาน"
