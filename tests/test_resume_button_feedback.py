"""Tests for the ↻ Resume status-bar button's UI feedback (#issue: button
looked dead — no disable/spinner while `/resume` waits for Lead to be
ready, and a silent drop after 45s gave zero user-visible signal).

Minimal stub mixing in `UserActionsMixin` — same spirit as
`test_remote_chip.py`'s `_Stub`, no `MainWindow`/Qt construction needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from PyQt6.QtWidgets import QPushButton

import agent_takkub.user_actions as ua_mod


class _Stub(ua_mod.UserActionsMixin):
    def __init__(self) -> None:
        self._status = MagicMock()
        self.orch = MagicMock()
        self._btn_resume = QPushButton("↻ Resume")


class TestOnResumeClicked:
    def test_disables_button_and_shows_busy_label_immediately(self) -> None:
        stub = _Stub()
        stub._on_resume_clicked()
        assert stub._btn_resume.isEnabled() is False
        assert stub._btn_resume.text() == "⏳ Resuming…"

    def test_passes_on_delivered_and_on_dropped_callbacks(self) -> None:
        stub = _Stub()
        stub._on_resume_clicked()
        _, kwargs = stub.orch.inject_slash_command_when_ready.call_args
        assert callable(kwargs["on_delivered"])
        assert callable(kwargs["on_dropped"])

    def test_on_delivered_restores_button_and_shows_success(self) -> None:
        stub = _Stub()
        stub._on_resume_clicked()
        _, kwargs = stub.orch.inject_slash_command_when_ready.call_args
        kwargs["on_delivered"]()
        assert stub._btn_resume.isEnabled() is True
        assert stub._btn_resume.text() == "↻ Resume"
        stub._status.showMessage.assert_called_once()
        assert "/resume sent to Lead" in stub._status.showMessage.call_args[0][0]

    def test_on_dropped_restores_button_and_shows_reason(self) -> None:
        stub = _Stub()
        stub._on_resume_clicked()
        _, kwargs = stub.orch.inject_slash_command_when_ready.call_args
        kwargs["on_dropped"]("timeout_not_ready")
        assert stub._btn_resume.isEnabled() is True
        assert stub._btn_resume.text() == "↻ Resume"
        msg = stub._status.showMessage.call_args[0][0]
        assert "timeout_not_ready" in msg
        assert "Resume ไม่สำเร็จ" in msg

    def test_repeat_click_while_disabled_is_a_no_op(self) -> None:
        stub = _Stub()
        stub._on_resume_clicked()
        assert stub.orch.inject_slash_command_when_ready.call_count == 1
        stub._on_resume_clicked()
        assert stub.orch.inject_slash_command_when_ready.call_count == 1

    def test_click_again_after_delivered_fires_again(self) -> None:
        stub = _Stub()
        stub._on_resume_clicked()
        _, kwargs = stub.orch.inject_slash_command_when_ready.call_args
        kwargs["on_delivered"]()
        stub._on_resume_clicked()
        assert stub.orch.inject_slash_command_when_ready.call_count == 2
