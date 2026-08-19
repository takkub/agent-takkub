"""Tests for Cursor helper (`agent_takkub.cursor_helper`) and Remote mirror integration.

Covers:
- Project directory resolution and CWD normalization
- Session resolution by UUID or newest file matching not_before
- Parsing user and assistant messages (handling <timestamp>, <user_query>, [REDACTED])
- Live text, user, and activity extractors
- Reading history messages with role and remote prefix stripping
- Listing recent sessions with teammate task filtering
- One-shot execution (cursor_exec)
- LeadNotifier integration with Cursor JSONL transcripts
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal

from agent_takkub import cursor_helper
from agent_takkub.provider_spec import PROVIDER_REGISTRY, cursor_spec
from agent_takkub.remote import notify as notify_mod
from agent_takkub.remote.notify import LeadNotifier


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


class _PaneState:
    def __init__(self, session_uuid: str | None) -> None:
        self.session_uuid = session_uuid


class _FakePane:
    def __init__(self, provider: str = "cursor") -> None:
        self.model = type("FakePaneModel", (), {"provider_name": provider, "spawn_ts": 0.0})()
        self.state = "idle"
        self.session = None


class _FakeOrch(QObject):
    agentDone = pyqtSignal(str, str, str)
    statusChanged = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._panes_by_project: dict = {}
        self._pane_state: dict = {}

    def set_lead(self, project: str, session_uuid: str | None, provider: str = "cursor") -> None:
        self._panes_by_project.setdefault(project, {})["lead"] = _FakePane(provider)
        self._pane_state[f"{project}::lead"] = _PaneState(session_uuid)


class _FakeBroadcaster:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str | None]] = []

    def push(self, event: str, data: str, project_ns: str | None = None) -> None:
        self.events.append((event, data, project_ns))


class TestCursorDirectoryResolution:
    def test_normalize_cwd(self):
        norm = cursor_helper.normalize_cursor_cwd("/Volumes/Data/Project//")
        assert not norm.endswith("/")
        assert norm == cursor_helper.normalize_cursor_cwd("/Volumes/Data/Project")
        assert cursor_helper.normalize_cursor_cwd("") == ""
        assert cursor_helper.normalize_cursor_cwd(None) == ""

    def test_projects_root_env_override(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom_cursor"
        monkeypatch.setenv("CURSOR_HOME", str(custom))
        assert cursor_helper.cursor_projects_root() == custom / "projects"

    def test_find_executable(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which", lambda n: "/usr/local/bin/cursor-agent" if n == "cursor-agent" else None
        )
        assert cursor_helper.find_cursor_executable() == "/usr/local/bin/cursor-agent"

    def test_provider_spec_has_remote_history(self):
        assert cursor_spec.supports_remote_history is True
        assert cursor_spec.produces_jsonl_transcript is True
        assert PROVIDER_REGISTRY["cursor"].supports_remote_history is True
        assert notify_mod.supports_remote_history("cursor") is True

    def test_find_project_dir_exact(self, tmp_path):
        cwd = "/Volumes/Data/test-project"
        encoded = "Volumes-Data-test-project"
        proj_dir = tmp_path / encoded
        proj_dir.mkdir(parents=True)

        found = cursor_helper.find_cursor_project_dir(cwd, root=tmp_path)
        assert found == proj_dir

    def test_find_project_dir_trusted_workspace(self, tmp_path):
        cwd = "/Volumes/Data/MyWorkspace"
        proj_dir = tmp_path / "some-opaque-folder"
        proj_dir.mkdir(parents=True)
        trusted = proj_dir / ".workspace-trusted"
        trusted.write_text(json.dumps({"workspacePath": cwd}), encoding="utf-8")

        found = cursor_helper.find_cursor_project_dir(cwd, root=tmp_path)
        assert found == proj_dir


class TestCursorSessionResolution:
    def test_resolve_by_session_uuid(self, tmp_path):
        cwd = "/Volumes/Data/project-a"
        encoded = "Volumes-Data-project-a"
        transcripts = tmp_path / encoded / "agent-transcripts"
        uuid = "12345678-abcd-ef01-2345-6789abcdef01"
        session_dir = transcripts / uuid
        session_dir.mkdir(parents=True)
        jsonl = session_dir / f"{uuid}.jsonl"
        jsonl.write_text('{"role":"user","message":{"content":"hello"}}\n', encoding="utf-8")

        resolved = cursor_helper.resolve_cursor_jsonl_for_cwd(cwd, uuid, root=tmp_path)
        assert resolved == jsonl

    def test_resolve_by_flat_session_uuid(self, tmp_path):
        cwd = "/Volumes/Data/project-a"
        encoded = "Volumes-Data-project-a"
        transcripts = tmp_path / encoded / "agent-transcripts"
        transcripts.mkdir(parents=True)
        uuid = "flat-uuid"
        jsonl = transcripts / f"{uuid}.jsonl"
        jsonl.write_text('{"role":"user","message":{"content":"hello"}}\n', encoding="utf-8")

        resolved = cursor_helper.resolve_cursor_jsonl_for_cwd(cwd, uuid, root=tmp_path)
        assert resolved == jsonl

    def test_resolve_newest_without_uuid(self, tmp_path):
        cwd = "/Volumes/Data/project-a"
        encoded = "Volumes-Data-project-a"
        transcripts = tmp_path / encoded / "agent-transcripts"

        s1 = transcripts / "s1"
        s1.mkdir(parents=True)
        j1 = s1 / "s1.jsonl"
        j1.write_text('{"role":"user","message":{"content":"one"}}\n', encoding="utf-8")

        s2 = transcripts / "s2"
        s2.mkdir(parents=True)
        j2 = s2 / "s2.jsonl"
        j2.write_text('{"role":"user","message":{"content":"two"}}\n', encoding="utf-8")

        resolved = cursor_helper.resolve_cursor_jsonl_for_cwd(cwd, None, root=tmp_path)
        assert resolved in (j1, j2)


class TestCursorRecordParsing:
    def test_parse_user_query_and_timestamp(self):
        line = {
            "role": "user",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "<timestamp>Monday, Jul 27, 2026, 5:42 PM (UTC+7)</timestamp>\n<user_query>\n[remote → lead] hello cursor\n</user_query>",
                    }
                ]
            },
        }
        res = cursor_helper.parse_cursor_record_message(line)
        assert res == ("me", "[remote → lead] hello cursor")

    def test_parse_assistant_response_and_strip_redacted(self):
        line = {
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Ready to help\n\n[REDACTED]"},
                    {"type": "tool_use", "name": "ReadFile", "input": {"path": "a.txt"}},
                ]
            },
        }
        res = cursor_helper.parse_cursor_record_message(line)
        assert res == ("lead", "Ready to help")

    def test_live_text_blocks(self):
        line = {
            "role": "assistant",
            "message": {"content": [{"type": "text", "text": "Task finished successfully"}]},
        }
        assert cursor_helper.cursor_live_text_blocks(line) == ["Task finished successfully"]

    def test_live_users_with_remote_flag(self):
        remote_user = {
            "role": "user",
            "message": {"content": [{"type": "text", "text": "[remote → lead] test msg"}]},
        }
        desktop_user = {
            "role": "user",
            "message": {"content": [{"type": "text", "text": "desktop msg"}]},
        }
        assert cursor_helper.cursor_live_users(remote_user) == [
            {"text": "test msg", "remote": True}
        ]
        assert cursor_helper.cursor_live_users(desktop_user) == [
            {"text": "desktop msg", "remote": False}
        ]

    def test_live_activity(self):
        read_line = {
            "role": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "ReadFile", "input": {}}]},
        }
        grep_line = {
            "role": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Grep", "input": {}}]},
        }
        shell_line = {
            "role": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Shell", "input": {}}]},
        }
        edit_line = {
            "role": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Write", "input": {}}]},
        }
        assert cursor_helper.cursor_live_activity(read_line) == "reading"
        assert cursor_helper.cursor_live_activity(grep_line) == "reading"
        assert cursor_helper.cursor_live_activity(shell_line) == "running"
        assert cursor_helper.cursor_live_activity(edit_line) == "editing"


class TestCursorHistoryAndSessionListing:
    def test_read_recent_messages(self, tmp_path):
        jsonl = tmp_path / "session.jsonl"
        lines = [
            json.dumps(
                {
                    "role": "user",
                    "message": {"content": [{"type": "text", "text": "[remote → lead] hi"}]},
                }
            ),
            json.dumps(
                {
                    "role": "assistant",
                    "message": {"content": [{"type": "text", "text": "hello back"}]},
                }
            ),
            json.dumps({"type": "turn_ended", "status": "success"}),
        ]
        jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")

        messages = cursor_helper.read_recent_cursor_messages(jsonl, limit=10)
        assert len(messages) == 2
        assert messages[0] == {"text": "hi", "kind": "me"}
        assert messages[1] == {"text": "hello back", "kind": "lead"}

    def test_list_recent_sessions_filters_teammates(self, tmp_path):
        cwd = "/Volumes/Data/project-x"
        encoded = "Volumes-Data-project-x"
        transcripts = tmp_path / encoded / "agent-transcripts"

        lead_sess = transcripts / "lead-1"
        lead_sess.mkdir(parents=True)
        (lead_sess / "lead-1.jsonl").write_text(
            json.dumps({"role": "user", "message": {"content": "Build feature X"}}) + "\n",
            encoding="utf-8",
        )

        team_sess = transcripts / "team-1"
        team_sess.mkdir(parents=True)
        (team_sess / "team-1.jsonl").write_text(
            json.dumps({"role": "user", "message": {"content": "[ROLE: qa] Run tests"}}) + "\n",
            encoding="utf-8",
        )

        sessions = cursor_helper.list_recent_cursor_sessions(cwd, limit=10, root=tmp_path)
        assert len(sessions) == 1
        assert sessions[0]["uuid"] == "lead-1"
        assert sessions[0]["preview"] == "Build feature X"


class TestCursorExec:
    def test_cursor_exec_success(self, monkeypatch):
        monkeypatch.setattr(
            "agent_takkub.cursor_helper.find_cursor_executable",
            lambda: "/usr/local/bin/cursor-agent",
        )
        fake_res = type("Proc", (), {"returncode": 0, "stdout": "result text", "stderr": ""})()
        with patch("subprocess.run", return_value=fake_res) as mock_run:
            ok, output = cursor_helper.cursor_exec("do work", model="claude-3-5-sonnet")
            assert ok is True
            assert output == "result text"
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "--model" in args
            assert "claude-3-5-sonnet" in args


class TestCursorLeadNotifierIntegration:
    def test_notifier_tails_cursor_transcript(self, qapp, tmp_path, monkeypatch):
        cwd = "/Volumes/Data/project-live"
        encoded = "Volumes-Data-project-live"
        transcripts = tmp_path / "projects" / encoded / "agent-transcripts"
        uuid = "sess-live"
        sdir = transcripts / uuid
        sdir.mkdir(parents=True)
        jsonl = sdir / f"{uuid}.jsonl"
        jsonl.write_text("", encoding="utf-8")

        monkeypatch.setenv("CURSOR_HOME", str(tmp_path))
        monkeypatch.setattr("agent_takkub.config.lead_cwd", lambda p: cwd)

        orch = _FakeOrch()
        orch.set_lead("project-live", uuid, provider="cursor")
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)

        try:
            # Append user turn
            with jsonl.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "role": "user",
                            "message": {"content": [{"type": "text", "text": "how are you"}]},
                        }
                    )
                    + "\n"
                )
            notifier._poll_all()
            user_events = [e for e in broadcaster.events if e[0] == "user"]
            assert len(user_events) == 1
            assert user_events[0][1] == {"text": "how are you", "remote": False}

            # Append assistant response
            with jsonl.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "role": "assistant",
                            "message": {
                                "content": [{"type": "text", "text": "I am working on it"}]
                            },
                        }
                    )
                    + "\n"
                )
            notifier._poll_all()
            lead_events = [e for e in broadcaster.events if e[0] == "lead"]
            assert len(lead_events) == 1
            assert lead_events[0][1] == "I am working on it"

        finally:
            notifier.stop()
