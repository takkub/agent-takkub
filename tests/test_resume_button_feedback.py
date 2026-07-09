"""Tests for the ↻ Resume status-bar button.

The button now drives the native W3 resume path (same as the mobile PWA):
pop a Qt session picker of the active project's recent Lead sessions, then
respawn the Lead pane with ``--resume <uuid>``. This replaced the old
approach of typing ``/resume`` into the pane and driving claude's TUI
picker, which #113 showed was fragile (the submitting Enter read as an
empty confirm) and, once the auto-Enter was dropped, left the button feeling
dead — you clicked it and nothing happened until you pressed Enter yourself.

Minimal stub mixing in `UserActionsMixin` — same spirit as
`test_remote_chip.py`'s `_Stub`, no `MainWindow`/Qt construction needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QPushButton

import agent_takkub.user_actions as ua_mod
from agent_takkub.roles import LEAD

# newest first, matching list_recent_lead_sessions' contract
_SESSIONS = [
    {"uuid": "uuid-newest", "mtime": 1_700_000_200.0, "preview": "แก้ปุ่ม resume"},
    {"uuid": "uuid-older", "mtime": 1_700_000_100.0, "preview": "ทำ dark mode"},
]


class _Stub(ua_mod.UserActionsMixin):
    def __init__(self) -> None:
        self._status = MagicMock()
        self.orch = MagicMock()
        self.orch.spawn.return_value = (True, "")  # default: spawn succeeds
        self._btn_resume = QPushButton("↻ Resume")


@pytest.fixture()
def wired(monkeypatch: pytest.MonkeyPatch):
    """Patch active project, session list, and lead cwd to known values, and
    return a helper to choose the picker outcome (which index, and ok/cancel).

    The helper also stashes the label list the handler built into ``seen`` so
    a test can assert how sessions were rendered.
    """
    monkeypatch.setattr(ua_mod, "active_project", lambda: ("takkub", "/repo"))

    import agent_takkub.chatlog_scanner as scanner_mod

    monkeypatch.setattr(
        scanner_mod, "list_recent_lead_sessions", lambda name, *a, **k: list(_SESSIONS)
    )

    import agent_takkub.config as config_mod

    monkeypatch.setattr(config_mod, "lead_cwd", lambda name=None: "/repo")

    seen: dict[str, list] = {}

    def _pick(index: int | None = 0, ok: bool = True):
        def _fake_getitem(parent, title, prompt, items, current, editable):
            seen["items"] = list(items)
            picked = (
                items[index]
                if (ok and index is not None and items)
                else (items[0] if items else "")
            )
            return picked, ok

        monkeypatch.setattr(ua_mod.QInputDialog, "getItem", _fake_getitem)
        return seen

    return _pick


class TestOnResumeClicked:
    def test_picking_newest_closes_then_respawns_lead_with_that_uuid(self, wired) -> None:
        seen = wired(index=0, ok=True)
        stub = _Stub()
        stub._on_resume_clicked()

        # picker rendered newest first, "N. <ts> · <preview>"
        assert seen["items"][0].endswith("แก้ปุ่ม resume")
        assert seen["items"][1].endswith("ทำ dark mode")

        stub.orch.close.assert_called_once()
        c_args, c_kwargs = stub.orch.close.call_args
        assert c_args[0] == LEAD.name
        assert c_kwargs.get("force") is True

        stub.orch.spawn.assert_called_once()
        s_args, s_kwargs = stub.orch.spawn.call_args
        assert s_args[0] == LEAD.name
        assert s_kwargs.get("resume_uuid") == "uuid-newest"
        assert s_kwargs.get("cwd") == "/repo"

        assert stub._btn_resume.isEnabled() is True
        assert stub._btn_resume.text() == "↻ Resume"

    def test_picking_older_resumes_that_uuid(self, wired) -> None:
        wired(index=1, ok=True)
        stub = _Stub()
        stub._on_resume_clicked()
        assert stub.orch.spawn.call_args.kwargs.get("resume_uuid") == "uuid-older"

    def test_cancelling_the_picker_does_nothing(self, wired) -> None:
        wired(ok=False)
        stub = _Stub()
        stub._on_resume_clicked()
        stub.orch.close.assert_not_called()
        stub.orch.spawn.assert_not_called()
        assert stub._btn_resume.isEnabled() is True

    def test_no_sessions_shows_message_and_never_touches_orch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ua_mod, "active_project", lambda: ("takkub", "/repo"))
        import agent_takkub.chatlog_scanner as scanner_mod

        monkeypatch.setattr(scanner_mod, "list_recent_lead_sessions", lambda name, *a, **k: [])
        stub = _Stub()
        stub._on_resume_clicked()
        stub.orch.close.assert_not_called()
        stub.orch.spawn.assert_not_called()
        assert stub._btn_resume.isEnabled() is True
        msg = stub._status.showMessage.call_args[0][0]
        assert "ไม่พบ session" in msg

    def test_spawn_failure_surfaces_reason_and_restores_button(self, wired) -> None:
        wired(index=0, ok=True)
        stub = _Stub()
        stub.orch.spawn.return_value = (False, "provider mismatch")
        stub._on_resume_clicked()

        assert stub._btn_resume.isEnabled() is True
        assert stub._btn_resume.text() == "↻ Resume"
        msg = stub._status.showMessage.call_args[0][0]
        assert "Resume ไม่สำเร็จ" in msg
        assert "provider mismatch" in msg

    def test_repeat_click_while_disabled_is_a_no_op(self, wired) -> None:
        stub = _Stub()
        stub._btn_resume.setEnabled(False)  # simulate an in-flight resume
        stub._on_resume_clicked()
        stub.orch.close.assert_not_called()
        stub.orch.spawn.assert_not_called()
