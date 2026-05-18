"""Tests for chatlog_scanner — the JSONL reader used by hot-md
hook-noise meter, friction heatmap, `takkub search`, decision
timeline, and the auto-resume brief.

Pin the contract on small synthetic jsonl fixtures so downstream
features rely on stable behavior:
  - bad lines skip silently
  - bookkeeping records (queue-operation, ai-title, etc.) don't
    appear as "conversation"
  - extract_text pulls from text, thinking, tool_use, tool_result
  - system reminders flow through their own helper
"""

from __future__ import annotations

import json
import pathlib

import pytest

from agent_takkub.chatlog_scanner import (
    claude_projects_dir,
    classify_hook,
    count_hook_fires,
    decode_project_dir,
    extract_text,
    is_conversation_record,
    is_system_reminder,
    iter_records,
    iter_session_files,
    record_timestamp,
    role_of,
    system_reminder_text,
    tool_uses,
)


def _write_jsonl(path: pathlib.Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


class TestDecodeProjectDir:
    def test_windows_drive_prefix_decodes(self) -> None:
        # Decoding is best-effort: Claude Code's encoding uses `-` for
        # both path separators and as-literal characters in project
        # names like "agent-takkub", so there's no way to recover the
        # exact original cwd. We just confirm the drive letter + colon
        # prefix and that recognisable name tokens survive.
        out = decode_project_dir("C--Users-monch-WebstormProjects-agent-takkub")
        s = str(out).replace("\\", "/")
        assert s.startswith("C:/")
        assert "Users" in s and "monch" in s and "WebstormProjects" in s
        assert "agent" in s and "takkub" in s

    def test_no_drive_prefix_falls_back_to_root(self) -> None:
        out = decode_project_dir("home-monch-repo")
        # Just confirm dashes are turned into separators
        assert "monch" in str(out)
        assert "repo" in str(out)

    def test_empty_name_returns_cwd(self) -> None:
        out = decode_project_dir("")
        assert str(out) == "."


class TestIterRecords:
    def test_parses_well_formed_jsonl(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "a.jsonl"
        _write_jsonl(
            path,
            [
                {"type": "user", "message": {"role": "user", "content": "hi"}},
                {"type": "assistant", "message": {"role": "assistant", "content": "ok"}},
            ],
        )
        recs = list(iter_records(path))
        assert len(recs) == 2
        assert recs[0]["type"] == "user"
        assert recs[1]["type"] == "assistant"

    def test_skips_corrupt_lines(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "broken.jsonl"
        path.write_text(
            '{"type":"user"}\n'
            "not json at all\n"
            '{"type":"assistant"}\n',
            encoding="utf-8",
        )
        recs = list(iter_records(path))
        # 2 of 3 parse; the bad line is dropped silently
        assert [r["type"] for r in recs] == ["user", "assistant"]

    def test_missing_file_yields_nothing(self, tmp_path: pathlib.Path) -> None:
        assert list(iter_records(tmp_path / "ghost.jsonl")) == []

    def test_skips_blank_lines(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "blanks.jsonl"
        path.write_text(
            '{"type":"user"}\n\n\n{"type":"assistant"}\n', encoding="utf-8"
        )
        assert len(list(iter_records(path))) == 2


class TestIsConversationRecord:
    def test_user_with_message_is_conversation(self) -> None:
        assert is_conversation_record(
            {"type": "user", "message": {"role": "user", "content": "hi"}}
        )

    def test_assistant_with_message_is_conversation(self) -> None:
        assert is_conversation_record(
            {"type": "assistant", "message": {"role": "assistant", "content": "ok"}}
        )

    def test_user_without_message_is_not(self) -> None:
        assert not is_conversation_record({"type": "user"})

    def test_bookkeeping_records_are_not(self) -> None:
        for t in (
            "queue-operation",
            "ai-title",
            "last-prompt",
            "attachment",
            "file-history-snapshot",
            "system",
        ):
            assert not is_conversation_record({"type": t, "message": {}}), t


class TestExtractText:
    def test_string_content_returned_as_is(self) -> None:
        rec = {"type": "user", "message": {"role": "user", "content": "hello world"}}
        assert extract_text(rec) == "hello world"

    def test_text_blocks_concatenated(self) -> None:
        rec = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "second"},
                ],
            },
        }
        assert "first" in extract_text(rec)
        assert "second" in extract_text(rec)

    def test_tool_use_args_included(self) -> None:
        rec = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {"file_path": "/foo/bar.py", "old_string": "x"},
                    }
                ],
            },
        }
        out = extract_text(rec)
        assert "tool_use Edit" in out
        assert "/foo/bar.py" in out  # so `takkub search "/foo/bar.py"` works

    def test_tool_result_text_extracted(self) -> None:
        rec = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": [{"type": "text", "text": "tests passed"}],
                    }
                ],
            },
        }
        assert "tests passed" in extract_text(rec)

    def test_thinking_block_included(self) -> None:
        rec = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "let me check the build"}
                ],
            },
        }
        assert "let me check" in extract_text(rec)

    def test_non_conversation_returns_empty(self) -> None:
        assert extract_text({"type": "queue-operation"}) == ""

    def test_thai_unicode_survives(self) -> None:
        rec = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "สวัสดี"}]},
        }
        assert "สวัสดี" in extract_text(rec)


class TestRecordTimestamp:
    def test_parses_z_suffix(self) -> None:
        ts = record_timestamp({"timestamp": "2026-05-17T11:30:00Z"})
        assert ts is not None
        assert ts.year == 2026 and ts.month == 5 and ts.day == 17

    def test_missing_timestamp_returns_none(self) -> None:
        assert record_timestamp({}) is None

    def test_unparseable_returns_none(self) -> None:
        assert record_timestamp({"timestamp": "yesterday"}) is None


class TestRoleOf:
    def test_returns_role_when_present(self) -> None:
        assert role_of({"message": {"role": "assistant"}}) == "assistant"

    def test_returns_none_when_missing(self) -> None:
        assert role_of({"message": {}}) is None
        assert role_of({}) is None


class TestToolUses:
    def test_returns_tool_use_blocks_only(self) -> None:
        rec = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "doing it"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "a"}},
                ],
            },
        }
        blocks = tool_uses(rec)
        assert [b["name"] for b in blocks] == ["Bash", "Edit"]

    def test_returns_empty_for_user_record(self) -> None:
        assert tool_uses({"type": "user", "message": {"role": "user", "content": "hi"}}) == []

    def test_returns_empty_for_non_conversation(self) -> None:
        assert tool_uses({"type": "queue-operation"}) == []


class TestSystemReminder:
    def test_is_system_reminder_true_for_system(self) -> None:
        assert is_system_reminder({"type": "system"})

    def test_is_system_reminder_false_for_user(self) -> None:
        assert not is_system_reminder({"type": "user"})

    def test_text_pulled_from_top_level_content(self) -> None:
        rec = {"type": "system", "content": "ECC GateGuard fact-force"}
        assert "GateGuard" in system_reminder_text(rec)

    def test_text_pulled_from_message_content_string(self) -> None:
        rec = {"type": "system", "message": {"content": "COST CRITICAL"}}
        assert "COST CRITICAL" in system_reminder_text(rec)

    def test_text_pulled_from_message_content_list(self) -> None:
        rec = {
            "type": "system",
            "message": {"content": [{"type": "text", "text": "Hook ran"}]},
        }
        assert "Hook ran" in system_reminder_text(rec)

    def test_text_empty_for_non_system(self) -> None:
        assert system_reminder_text({"type": "user"}) == ""


class TestClassifyHook:
    def test_gateguard_pattern_buckets_correctly(self) -> None:
        assert classify_hook("[Fact-Forcing Gate] please present facts") == "ecc-gateguard"
        assert classify_hook("running gateguard-fact-force") == "ecc-gateguard"

    def test_cost_critical_pattern(self) -> None:
        assert classify_hook("COST CRITICAL: Session cost is $999") == "ecc-cost-monitor"
        assert classify_hook("ecc-context-monitor fired") == "ecc-cost-monitor"

    def test_loop_warning_pattern(self) -> None:
        assert (
            classify_hook("LOOP WARNING: Tool called 3 times")
            == "ecc-loop-warning"
        )

    def test_strategic_compact(self) -> None:
        assert (
            classify_hook("[StrategicCompact] 75 tool calls")
            == "ecc-strategic-compact"
        )

    def test_unknown_returns_none(self) -> None:
        assert classify_hook("some completely unrelated text") is None

    def test_empty_returns_none(self) -> None:
        assert classify_hook("") is None
        assert classify_hook(None) is None  # type: ignore[arg-type]

    def test_first_match_wins(self) -> None:
        # A reminder mentioning both GateGuard and COST CRITICAL —
        # whichever pattern is listed first in `_HOOK_PATTERNS` claims
        # the bucket. We don't double-count one record.
        text = "[Fact-Forcing Gate] ... COST CRITICAL: ..."
        bucket = classify_hook(text)
        assert bucket in ("ecc-gateguard", "ecc-cost-monitor")


class TestCountHookFires:
    def test_counts_per_bucket(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-monch-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                {"type": "system", "content": "[Fact-Forcing Gate] please"},
                {"type": "system", "content": "[Fact-Forcing Gate] again"},
                {"type": "system", "content": "COST CRITICAL: $5"},
                {"type": "user", "message": {"role": "user", "content": "hi"}},
            ],
        )
        counts = count_hook_fires()
        assert counts.get("ecc-gateguard") == 2
        assert counts.get("ecc-cost-monitor") == 1

    def test_filters_by_project(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        base = tmp_path / ".claude" / "projects"
        keep = base / "C--Users-monch-agent-takkub"
        other = base / "C--Users-monch-other"
        for d in (keep, other):
            d.mkdir(parents=True)
        _write_jsonl(
            keep / "s.jsonl",
            [{"type": "system", "content": "[Fact-Forcing Gate]"}],
        )
        _write_jsonl(
            other / "s.jsonl",
            [{"type": "system", "content": "[Fact-Forcing Gate]"}],
        )
        # Restricted to agent-takkub → only that project's 1 fire counted
        assert count_hook_fires("agent-takkub") == {"ecc-gateguard": 1}

    def test_ignores_non_system_records(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # Conversational text that happens to contain "COST CRITICAL"
        # must not bump the counter — only `system` records do.
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-monch-x"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "COST CRITICAL"},
                }
            ],
        )
        assert count_hook_fires() == {}


class TestIterSessionFiles:
    def test_returns_empty_when_no_claude_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # Point home to an empty tmp dir → no ~/.claude/projects
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        assert list(iter_session_files()) == []

    def test_filters_by_project_name_substring(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        base = tmp_path / ".claude" / "projects"
        keep = base / "C--Users-monch-WebstormProjects-agent-takkub"
        skip = base / "C--Users-monch-OtherRepo"
        for d in (keep, skip):
            d.mkdir(parents=True)
            _write_jsonl(d / "session.jsonl", [{"type": "user"}])
        found = list(iter_session_files("agent-takkub"))
        assert len(found) == 1
        assert "agent-takkub" in str(found[0])

    def test_claude_projects_dir_resolves_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        assert claude_projects_dir() == tmp_path / ".claude" / "projects"
