"""agy (Antigravity CLI) Remote mirror + resume-picker previews (2026-08-19).

Two silent-blank-phone bugs are pinned here:

1. agy moved its conversation store to
   ``~/.gemini/antigravity-cli/{conversations,brain}`` with a new record
   schema. Nothing errored — the old resolver kept resolving a months-old
   file under ``~/.gemini/tmp/.../chats``, so every gemini Lead mirrored an
   empty chat.
2. The Mobile resume picker previewed the cockpit's own synthetic opening
   line, so the list read as the same sentence repeated. Claude's `ai-title`
   is used instead, matching the desktop `/resume` picker.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent_takkub import gemini_helper
from agent_takkub.remote import notify as notify_mod


def _write_agy_session(root: Path, session_id: str, workspace: str, records: list[dict]) -> Path:
    """Create one agy conversation db + transcript, as the CLI lays them out."""
    conversations = root / "conversations"
    conversations.mkdir(parents=True, exist_ok=True)
    db_path = conversations / f"{session_id}.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB)")
    blob = b"\n\xaf\x01\n3file:///" + workspace.encode("utf-8") + b"\x12\x33more-protobuf"
    con.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (blob,))
    con.commit()
    con.close()

    transcript = root / "brain" / session_id / ".system_generated" / "logs" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return transcript


def _user(text: str) -> dict:
    return {
        "type": "USER_INPUT",
        "source": "USER_EXPLICIT",
        "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nnoise\n</ADDITIONAL_METADATA>",
    }


def _model(text: str, thinking: str = "hidden reasoning") -> dict:
    return {"type": "PLANNER_RESPONSE", "source": "MODEL", "content": text, "thinking": thinking}


@pytest.fixture
def agy_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "antigravity-cli"
    root.mkdir()
    monkeypatch.setattr(gemini_helper, "antigravity_root", lambda: root)
    return root


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setattr("agent_takkub.config.lead_cwd", lambda project=None: str(cwd))
    return cwd.resolve().as_posix()


class TestAgyMirror:
    def test_resolves_the_new_store_and_reads_both_sides(self, agy_root, workspace):
        transcript = _write_agy_session(
            agy_root,
            "11111111-1111-1111-1111-111111111111",
            workspace,
            [
                _user("[remote → lead] ทดสอบ"),
                {"type": "CHECKPOINT", "source": "SYSTEM", "content": "state dump"},
                _model("สวัสดีครับ"),
            ],
        )

        assert notify_mod._resolve_gemini_jsonl_path("proj", None) == transcript
        assert notify_mod.read_recent_lead_messages(transcript, provider="gemini") == [
            {"text": "ทดสอบ", "kind": "me"},
            {"text": "สวัสดีครับ", "kind": "lead"},
        ]

    def test_thinking_and_system_records_never_reach_the_phone(self, agy_root, workspace):
        transcript = _write_agy_session(
            agy_root,
            "22222222-2222-2222-2222-222222222222",
            workspace,
            [
                _model("visible answer", thinking="SECRET chain of thought"),
                {"type": "ERROR_MESSAGE", "source": "SYSTEM", "content": "boom"},
            ],
        )
        messages = notify_mod.read_recent_lead_messages(transcript, provider="gemini")
        assert messages == [{"text": "visible answer", "kind": "lead"}]

        rec = {"type": "PLANNER_RESPONSE", "source": "MODEL", "content": "x", "thinking": "SECRET"}
        assert notify_mod._gemini_live_text_blocks(rec) == ["x"]

    def test_another_workspace_is_never_mirrored(self, agy_root, workspace, tmp_path: Path):
        other = (tmp_path / "other-project").resolve().as_posix()
        _write_agy_session(
            agy_root, "33333333-3333-3333-3333-333333333333", other, [_model("not yours")]
        )
        assert notify_mod._resolve_gemini_jsonl_path("proj", None) is None

    def test_picker_lists_lead_sessions_only(self, agy_root, workspace):
        _write_agy_session(
            agy_root, "44444444-4444-4444-4444-444444444444", workspace, [_user("โหลดงานหน่อย")]
        )
        _write_agy_session(
            agy_root,
            "55555555-5555-5555-5555-555555555555",
            workspace,
            [_user("[ROLE: backend] อ่าน task spec")],
        )
        sessions = notify_mod._list_recent_gemini_sessions("proj", 10)
        assert [s["preview"] for s in sessions] == ["โหลดงานหน่อย"]


class TestResumePickerPreview:
    def _claude_session(self, tmp_path: Path, name: str, records: list[dict]) -> Path:
        path = tmp_path / f"{name}.jsonl"
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        return path

    def _user_rec(self, text: str) -> dict:
        return {"type": "user", "message": {"role": "user", "content": text}}

    def test_ai_title_wins_over_the_synthetic_opening_line(self, tmp_path: Path):
        path = self._claude_session(
            tmp_path,
            "sess",
            [
                self._user_rec(notify_mod._SPAWN_TASK_TRIGGER),
                {"type": "ai-title", "aiTitle": "โหลๆ", "sessionId": "sess"},
            ],
        )
        assert notify_mod._first_user_preview(path) == "โหลๆ"

    def test_without_a_title_the_generated_opener_is_skipped(self, tmp_path: Path):
        path = self._claude_session(
            tmp_path,
            "sess",
            [
                self._user_rec(notify_mod._SPAWN_TASK_TRIGGER),
                self._user_rec("แก้บั๊ก remote ให้หน่อย"),
            ],
        )
        assert notify_mod._first_user_preview(path) == "แก้บั๊ก remote ให้หน่อย"

    def test_teammate_sessions_are_still_filtered_when_a_title_exists(self, tmp_path: Path):
        # Regression guard: the picker used to classify sessions by the string
        # it displayed. Now that an ai-title can replace that string, the
        # teammate check must read the first user line instead — otherwise
        # every assigned task reappears in the Lead picker.
        path = self._claude_session(
            tmp_path,
            "sess",
            [
                self._user_rec("[ROLE: qa] รัน full suite"),
                {"type": "ai-title", "aiTitle": "Run the suite", "sessionId": "sess"},
            ],
        )
        assert notify_mod._is_teammate_session_line(notify_mod._first_user_line(path)) is True

    def test_trigger_literal_matches_the_spawn_engine(self):
        # Read the constant from source rather than importing spawn_engine:
        # that module pulls in Qt WebEngine, which cannot be imported after a
        # QCoreApplication exists. The pin still fails loudly if either side
        # of the mirrored literal is edited alone.
        source = (
            Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "spawn_engine.py"
        ).read_text(encoding="utf-8")
        assert f'_CURRENT_TASK_TRIGGER = "{notify_mod._SPAWN_TASK_TRIGGER}"' in source
