"""Tests for OpenCode helper (`agent_takkub.opencode_helper`) and Remote mirror integration.

Covers:
- Database path resolution and CWD normalization
- Session resolution by UUID or directory
- Reading history messages with role and remote prefix parsing
- Listing recent sessions with teammate task filtering
- Incremental delta polling (lead, user, working, blocked_on_picker)
- One-shot execution (opencode_exec)
- LeadNotifier integration with OpenCode SQLite transcripts
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import opencode_helper
from agent_takkub.provider_spec import PROVIDER_REGISTRY, opencode_spec
from agent_takkub.remote import notify as notify_mod
from agent_takkub.remote.notify import LeadNotifier


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _create_test_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            directory TEXT,
            title TEXT,
            time_created INTEGER,
            time_updated INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            data TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            data TEXT
        )
        """
    )
    conn.commit()
    return conn


class TestOpencodeDatabaseResolution:
    def test_normalize_cwd(self):
        norm = opencode_helper.normalize_opencode_cwd("/Volumes/Data/Project//")
        assert not norm.endswith("/")
        assert norm == opencode_helper.normalize_opencode_cwd("/Volumes/Data/Project")
        assert opencode_helper.normalize_opencode_cwd("") == ""
        assert opencode_helper.normalize_opencode_cwd(None) == ""

    def test_db_path_env_override(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom_opencode.db"
        custom.write_bytes(b"")
        monkeypatch.setenv("OPENCODE_DB_PATH", str(custom))
        assert opencode_helper.opencode_db_path() == custom

    def test_find_executable(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda n: "/usr/local/bin/opencode" if n == "opencode" else None)
        assert opencode_helper.find_opencode_executable() == "/usr/local/bin/opencode"

    def test_provider_spec_has_remote_history(self):
        assert opencode_spec.supports_remote_history is True
        assert opencode_spec.supports_resume is True
        assert PROVIDER_REGISTRY["opencode"].supports_remote_history is True
        assert notify_mod.supports_remote_history("opencode") is True


class TestOpencodeSessionResolution:
    def test_resolve_by_session_uuid(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        conn = _create_test_db(db_path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            ("ses_1", str(tmp_path / "myproj"), "Session 1", 1000, 2000),
        )
        conn.commit()
        conn.close()

        resolved = opencode_helper.resolve_opencode_session(
            str(tmp_path / "myproj"), session_id="ses_1", db_path=db_path
        )
        assert resolved is not None
        assert resolved[0] == db_path
        assert resolved[1] == "ses_1"

    def test_resolve_by_cwd_newest_updated(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        conn = _create_test_db(db_path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            ("ses_old", str(tmp_path / "myproj"), "Old Session", 1000, 1500),
        )
        cur.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            ("ses_new", str(tmp_path / "myproj"), "New Session", 2000, 3000),
        )
        cur.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            ("ses_other", str(tmp_path / "otherproj"), "Other Session", 2000, 4000),
        )
        conn.commit()
        conn.close()

        resolved = opencode_helper.resolve_opencode_session(
            str(tmp_path / "myproj"), db_path=db_path
        )
        assert resolved is not None
        assert resolved[1] == "ses_new"


class TestOpencodeMessageReading:
    def test_read_messages_formats_kinds_and_strips_prefix(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        conn = _create_test_db(db_path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            ("ses_1", str(tmp_path / "myproj"), "Session 1", 1000, 3000),
        )
        # User message
        cur.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("msg_u1", "ses_1", 1000, json.dumps({"role": "user"})),
        )
        cur.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            ("prt_u1", "msg_u1", "ses_1", 1000, json.dumps({"type": "text", "text": "[remote → lead] hello world"})),
        )
        # Assistant message with reasoning + tool + text
        cur.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("msg_a1", "ses_1", 2000, json.dumps({"role": "assistant"})),
        )
        cur.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            ("prt_a1_reason", "msg_a1", "ses_1", 2000, json.dumps({"type": "reasoning", "text": "Thinking..."})),
        )
        cur.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            ("prt_a1_tool", "msg_a1", "ses_1", 2050, json.dumps({"type": "tool", "tool": "read"})),
        )
        cur.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            ("prt_a1_text", "msg_a1", "ses_1", 2100, json.dumps({"type": "text", "text": "Hello! How can I help?"})),
        )
        conn.commit()
        conn.close()

        msgs = opencode_helper.read_opencode_session_messages(db_path, "ses_1", limit=10)
        assert len(msgs) == 2
        assert msgs[0] == {"kind": "me", "text": "hello world"}
        assert msgs[1] == {"kind": "lead", "text": "Hello! How can I help?"}


class TestOpencodeListSessions:
    def test_list_recent_sessions_skips_teammate_tasks(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        conn = _create_test_db(db_path)
        cur = conn.cursor()
        proj_dir = str(tmp_path / "myproj")
        cur.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            ("ses_normal", proj_dir, "Normal Session", 1000, 2000),
        )
        cur.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("msg_n", "ses_normal", 1000, json.dumps({"role": "user"})),
        )
        cur.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            ("prt_n", "msg_n", "ses_normal", 1000, json.dumps({"type": "text", "text": "Can you design a dashboard?"})),
        )

        cur.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            ("ses_teammate", proj_dir, "Teammate Session", 3000, 4000),
        )
        cur.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("msg_t", "ses_teammate", 3000, json.dumps({"role": "user"})),
        )
        cur.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            ("prt_t", "msg_t", "ses_teammate", 3000, json.dumps({"type": "text", "text": "[ROLE: frontend — fix bug #123]"})),
        )
        conn.commit()
        conn.close()

        sessions = opencode_helper.list_recent_opencode_sessions(proj_dir, db_path=db_path)
        assert len(sessions) == 1
        assert sessions[0]["uuid"] == "ses_normal"
        assert "Can you design a dashboard?" in sessions[0]["preview"]


class TestOpencodeDeltaPolling:
    def test_poll_delta_events(self, tmp_path):
        db_path = tmp_path / "opencode.db"
        conn = _create_test_db(db_path)
        cur = conn.cursor()
        proj_dir = str(tmp_path / "myproj")
        cur.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            ("ses_1", proj_dir, "Session 1", 1000, 2000),
        )
        # Initial messages
        cur.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("msg_1", "ses_1", 1000, json.dumps({"role": "user"})),
        )
        cur.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            ("prt_1", "msg_1", "ses_1", 1000, json.dumps({"type": "text", "text": "[remote → lead] hi"})),
        )
        conn.commit()

        # Initial latest part time
        latest_time = opencode_helper.get_opencode_latest_part_time(db_path, "ses_1")
        assert latest_time == 1000

        # Poll with since_time_ms=1000 -> no new events
        new_max, events = opencode_helper.poll_opencode_delta(db_path, "ses_1", since_time_ms=1000)
        assert new_max == 1000
        assert events == []

        # Add assistant tool + text
        cur.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("msg_2", "ses_1", 2000, json.dumps({"role": "assistant"})),
        )
        cur.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            ("prt_tool", "msg_2", "ses_1", 2010, json.dumps({"type": "tool", "tool": "bash"})),
        )
        cur.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            ("prt_text", "msg_2", "ses_1", 2050, json.dumps({"type": "text", "text": "Command executed successfully."})),
        )
        conn.commit()

        new_max, events = opencode_helper.poll_opencode_delta(db_path, "ses_1", since_time_ms=1000)
        assert new_max == 2050
        assert len(events) == 2
        assert events[0] == ("working", "running")
        assert events[1] == ("lead", "Command executed successfully.")

        # Test question tool (AskUserQuestion blocked_on_picker)
        cur.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("msg_3", "ses_1", 3000, json.dumps({"role": "assistant"})),
        )
        cur.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (
                "prt_q",
                "msg_3",
                "ses_1",
                3010,
                json.dumps(
                    {
                        "type": "tool",
                        "tool": "question",
                        "state": {
                            "status": "running",
                            "input": {
                                "questions": [
                                    {
                                        "question": "Which database engine should we use?",
                                        "options": [{"label": "PostgreSQL (Recommended)"}, {"label": "SQLite"}],
                                    }
                                ]
                            },
                        },
                    }
                ),
            ),
        )
        conn.commit()
        conn.close()

        new_max, events = opencode_helper.poll_opencode_delta(db_path, "ses_1", since_time_ms=2050)
        assert new_max == 3010
        assert len(events) == 1
        assert events[0][0] == "blocked_on_picker"
        assert events[0][1]["prompt"] == "Which database engine should we use?"
        assert events[0][1]["options"] == ["PostgreSQL (Recommended)", "SQLite"]


from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal


class _FakeBroadcaster:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, str | None]] = []

    def push(self, event: str, data: object, project_ns: str | None = None) -> None:
        self.events.append((event, data, project_ns))


class _FakePane:
    def __init__(self, provider: str = "opencode") -> None:
        self.model = type("FakePaneModel", (), {"provider_name": provider, "spawn_ts": 0.0})()
        self.state = "idle"


class _FakeOrch(QObject):
    agentDone = pyqtSignal(str, str, str)
    statusChanged = pyqtSignal()

    def __init__(self, project: str, provider: str = "opencode") -> None:
        super().__init__()
        self._panes_by_project = {project: {"lead": _FakePane(provider)}}
        self._pane_state = {}


class TestOpencodeLeadNotifierIntegration:
    def test_lead_notifier_polls_opencode_and_emits_sse(self, tmp_path, monkeypatch):
        db_path = tmp_path / "opencode.db"
        conn = _create_test_db(db_path)
        cur = conn.cursor()
        proj_dir = str(tmp_path / "myproj")
        cur.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            ("ses_test", proj_dir, "Test Session", 1000, 1000),
        )
        cur.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("msg_0", "ses_test", 1000, json.dumps({"role": "user"})),
        )
        cur.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            ("prt_0", "msg_0", "ses_test", 1000, json.dumps({"type": "text", "text": "start"})),
        )
        conn.commit()

        monkeypatch.setenv("OPENCODE_DB_PATH", str(db_path))
        monkeypatch.setattr("agent_takkub.config.lead_cwd", lambda p: proj_dir)

        orch = _FakeOrch("myproj", provider="opencode")
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster=broadcaster)
        notifier._timer.stop()

        # Initial resync initializes tail at latest part time (1000)
        notifier._resync()
        assert "myproj" in notifier._tails
        assert notifier._tails["myproj"].provider == "opencode"
        assert notifier._tails["myproj"].offset == 1000

        # Now append assistant reply
        cur.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("msg_1", "ses_test", 2000, json.dumps({"role": "assistant"})),
        )
        cur.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            ("prt_1", "msg_1", "ses_test", 2000, json.dumps({"type": "text", "text": "Hello from OpenCode Lead!"})),
        )
        conn.commit()
        conn.close()

        # Poll all
        notifier._poll_all()

        lead_events = [e for e in broadcaster.events if e[0] == "lead" and e[2] == "myproj"]
        assert len(lead_events) == 1
        assert lead_events[0][1] == "Hello from OpenCode Lead!"
