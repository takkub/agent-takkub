"""Tests for Kimi CLI helper (`agent_takkub.kimi_helper`).

Schema pinned from kimi-cli 1.49.0's own installed source
(`kimi_cli/metadata.py`, `kimi_cli/wire/{file,types}.py`) and cross-checked
against a real recorded line from a genuine prior teammate task on this
machine — see `kimi_helper.py`'s module docstring
(`docs/v2/2.0.0-migration-plan.md` §1.1, epic #309).
"""

from __future__ import annotations

import json
from hashlib import md5

from agent_takkub import kimi_helper


def _write_metadata(share_dir, *work_dirs) -> None:
    (share_dir / "kimi.json").write_text(
        json.dumps({"work_dirs": list(work_dirs)}), encoding="utf-8"
    )


class TestShareDirAndCwdNormalization:
    def test_share_dir_env_override(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom_kimi"
        monkeypatch.setenv("KIMI_SHARE_DIR", str(custom))
        assert kimi_helper.kimi_share_dir() == custom

    def test_share_dir_defaults_to_home(self, monkeypatch):
        monkeypatch.delenv("KIMI_SHARE_DIR", raising=False)
        assert kimi_helper.kimi_share_dir().name == ".kimi"

    def test_normalize_cwd_empty(self):
        assert kimi_helper.normalize_kimi_cwd("") == ""
        assert kimi_helper.normalize_kimi_cwd(None) == ""

    def test_normalize_cwd_is_absolute(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        norm = kimi_helper.normalize_kimi_cwd(".")
        assert norm == str(tmp_path)


class TestResolveSessionDir:
    def test_resolves_by_md5_of_registered_work_dir(self, tmp_path, monkeypatch):
        share = tmp_path / "share"
        share.mkdir()
        monkeypatch.setenv("KIMI_SHARE_DIR", str(share))
        cwd = str(tmp_path / "project")
        (tmp_path / "project").mkdir()
        _write_metadata(share, {"path": cwd, "kaos": "local", "last_session_id": None})

        digest = md5(cwd.encode("utf-8")).hexdigest()
        session_dir = share / "sessions" / digest / "session-1"
        session_dir.mkdir(parents=True)
        (session_dir / "wire.jsonl").write_text('{"type": "metadata"}\n', encoding="utf-8")

        found = kimi_helper.resolve_kimi_session_dir(cwd, "session-1")
        assert found == session_dir

    def test_unregistered_cwd_returns_none(self, tmp_path, monkeypatch):
        share = tmp_path / "share"
        share.mkdir()
        monkeypatch.setenv("KIMI_SHARE_DIR", str(share))
        _write_metadata(share)
        assert kimi_helper.resolve_kimi_session_dir(str(tmp_path), None) is None

    def test_no_session_id_picks_newest_by_wire_file_mtime(self, tmp_path, monkeypatch):
        share = tmp_path / "share"
        share.mkdir()
        monkeypatch.setenv("KIMI_SHARE_DIR", str(share))
        cwd = str(tmp_path / "project")
        (tmp_path / "project").mkdir()
        _write_metadata(share, {"path": cwd, "kaos": "local", "last_session_id": None})
        digest = md5(cwd.encode("utf-8")).hexdigest()
        sessions_root = share / "sessions" / digest

        old_dir = sessions_root / "old-session"
        old_dir.mkdir(parents=True)
        (old_dir / "wire.jsonl").write_text("{}\n", encoding="utf-8")

        new_dir = sessions_root / "new-session"
        new_dir.mkdir(parents=True)
        (new_dir / "wire.jsonl").write_text("{}\n", encoding="utf-8")
        # force a deterministic mtime ordering regardless of filesystem clock resolution
        import os
        import time

        now = time.time()
        os.utime(old_dir / "wire.jsonl", (now - 100, now - 100))
        os.utime(new_dir / "wire.jsonl", (now, now))

        assert kimi_helper.resolve_kimi_session_dir(cwd, None) == new_dir

    def test_session_dir_without_wire_file_is_not_matched(self, tmp_path, monkeypatch):
        share = tmp_path / "share"
        share.mkdir()
        monkeypatch.setenv("KIMI_SHARE_DIR", str(share))
        cwd = str(tmp_path / "project")
        (tmp_path / "project").mkdir()
        _write_metadata(share, {"path": cwd, "kaos": "local", "last_session_id": None})
        digest = md5(cwd.encode("utf-8")).hexdigest()
        empty_session = share / "sessions" / digest / "no-wire-file"
        empty_session.mkdir(parents=True)
        (empty_session / "context.jsonl").write_text("{}\n", encoding="utf-8")

        assert kimi_helper.resolve_kimi_session_dir(cwd, None) is None
        assert kimi_helper.resolve_kimi_session_dir(cwd, "no-wire-file") is None


class TestParseWireRecord:
    def test_metadata_header_line_is_not_a_message(self):
        assert (
            kimi_helper.parse_kimi_wire_record({"type": "metadata", "protocol_version": "1.10"})
            is None
        )

    def test_turn_begin_with_string_user_input(self):
        rec = {"timestamp": 1.0, "message": {"type": "TurnBegin", "payload": {"user_input": "hi"}}}
        assert kimi_helper.parse_kimi_wire_record(rec) == ("me", "hi")

    def test_turn_begin_with_content_part_list_user_input(self):
        # real shape observed on this machine: user_input as a list of TextParts
        rec = {
            "timestamp": 1.0,
            "message": {
                "type": "TurnBegin",
                "payload": {"user_input": [{"type": "text", "text": "hi there"}]},
            },
        }
        assert kimi_helper.parse_kimi_wire_record(rec) == ("me", "hi there")

    def test_steer_input_is_also_a_user_turn(self):
        rec = {
            "timestamp": 1.0,
            "message": {"type": "SteerInput", "payload": {"user_input": "follow-up"}},
        }
        assert kimi_helper.parse_kimi_wire_record(rec) == ("me", "follow-up")

    def test_text_part_is_assistant_reply(self):
        rec = {
            "timestamp": 2.0,
            "message": {"type": "TextPart", "payload": {"type": "text", "text": "hello back"}},
        }
        assert kimi_helper.parse_kimi_wire_record(rec) == ("lead", "hello back")

    def test_turn_end_has_no_text(self):
        rec = {"timestamp": 3.0, "message": {"type": "TurnEnd", "payload": {}}}
        assert kimi_helper.parse_kimi_wire_record(rec) is None

    def test_think_part_is_never_surfaced(self):
        # hidden reasoning must never reach a message consumer (provider-integration
        # skill's "text only" non-negotiable)
        rec = {
            "timestamp": 2.0,
            "message": {
                "type": "ThinkPart",
                "payload": {"type": "think", "think": "secret reasoning", "encrypted": None},
            },
        }
        assert kimi_helper.parse_kimi_wire_record(rec) is None

    def test_tool_call_is_never_surfaced(self):
        rec = {
            "timestamp": 2.0,
            "message": {
                "type": "ToolCall",
                "payload": {"id": "1", "function": {"name": "Read", "arguments": "{}"}},
            },
        }
        assert kimi_helper.parse_kimi_wire_record(rec) is None

    def test_empty_text_part_is_not_surfaced(self):
        rec = {"timestamp": 2.0, "message": {"type": "TextPart", "payload": {"text": ""}}}
        assert kimi_helper.parse_kimi_wire_record(rec) is None

    def test_non_dict_is_none(self):
        assert kimi_helper.parse_kimi_wire_record("not a dict") is None
        assert kimi_helper.parse_kimi_wire_record(None) is None


class TestReadRecentMessages:
    def test_reads_turn_and_reply_in_order(self, tmp_path):
        path = tmp_path / "wire.jsonl"
        lines = [
            json.dumps({"type": "metadata", "protocol_version": "1.10"}),
            json.dumps(
                {
                    "timestamp": 1.0,
                    "message": {"type": "TurnBegin", "payload": {"user_input": "hi"}},
                }
            ),
            json.dumps(
                {
                    "timestamp": 2.0,
                    "message": {
                        "type": "TextPart",
                        "payload": {"type": "text", "text": "hello back"},
                    },
                }
            ),
            json.dumps({"timestamp": 3.0, "message": {"type": "TurnEnd", "payload": {}}}),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = kimi_helper.read_recent_kimi_messages(path)
        assert out == [{"text": "hi", "kind": "me"}, {"text": "hello back", "kind": "lead"}]

    def test_missing_file_returns_empty(self, tmp_path):
        assert kimi_helper.read_recent_kimi_messages(tmp_path / "missing.jsonl") == []
