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
    build_resume_brief,
    classify_hook,
    claude_projects_dir,
    count_hook_fires,
    count_tool_retries,
    count_user_corrections,
    decode_project_dir,
    extract_decisions,
    extract_text,
    is_conversation_record,
    is_system_reminder,
    iter_records,
    iter_session_files,
    record_timestamp,
    role_of,
    search_sessions,
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
        out = decode_project_dir("C--Users-alice-WebstormProjects-agent-takkub")
        s = str(out).replace("\\", "/")
        assert s.startswith("C:/")
        assert "Users" in s and "alice" in s and "WebstormProjects" in s
        assert "agent" in s and "takkub" in s

    def test_no_drive_prefix_falls_back_to_root(self) -> None:
        out = decode_project_dir("home-alice-repo")
        # Just confirm dashes are turned into separators
        assert "alice" in str(out)
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
            '{"type":"user"}\nnot json at all\n{"type":"assistant"}\n',
            encoding="utf-8",
        )
        recs = list(iter_records(path))
        # 2 of 3 parse; the bad line is dropped silently
        assert [r["type"] for r in recs] == ["user", "assistant"]

    def test_missing_file_yields_nothing(self, tmp_path: pathlib.Path) -> None:
        assert list(iter_records(tmp_path / "ghost.jsonl")) == []

    def test_skips_blank_lines(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "blanks.jsonl"
        path.write_text('{"type":"user"}\n\n\n{"type":"assistant"}\n', encoding="utf-8")
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
                "content": [{"type": "thinking", "thinking": "let me check the build"}],
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
        assert classify_hook("LOOP WARNING: Tool called 3 times") == "ecc-loop-warning"

    def test_strategic_compact(self) -> None:
        assert classify_hook("[StrategicCompact] 75 tool calls") == "ecc-strategic-compact"

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
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
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
        keep = base / "C--Users-alice-agent-takkub"
        other = base / "C--Users-alice-other"
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
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-x"
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


class TestUserCorrections:
    def test_counts_thai_dissatisfaction_tokens(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "ไม่ใช่ ทำไม่ถูก"}],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "พังเลย เอาออก"}],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "good work"}],
                    },
                },
            ],
        )
        assert count_user_corrections() == 2

    def test_counts_each_message_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # A single message with multiple correction tokens still
        # counts as 1 — it's the same "user pushed back" event.
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "ไม่ใช่ ผิด พังเลย เอาออก ลบให้หมด",
                            }
                        ],
                    },
                }
            ],
        )
        assert count_user_corrections() == 1

    def test_ignores_tool_results_with_correction_words(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # tool_result blocks are role=user but generated by tooling,
        # not the human. They sometimes contain "broken" or "wrong"
        # in error text — must NOT count as user corrections.
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "content": [{"type": "text", "text": "broken pipeline"}],
                            }
                        ],
                    },
                }
            ],
        )
        assert count_user_corrections() == 0

    def test_returns_zero_when_no_jsonl(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        assert count_user_corrections() == 0

    def test_english_patterns_match(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "that's wrong, revert"}],
                    },
                }
            ],
        )
        assert count_user_corrections() == 1


class TestToolRetries:
    def test_three_identical_calls_counts_one_storm(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        same_call = {
            "type": "tool_use",
            "name": "Edit",
            "input": {"file_path": "/x", "old_string": "a", "new_string": "b"},
        }
        # Three identical Edit calls in sequence → one storm event
        recs = [
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [same_call]},
            }
            for _ in range(3)
        ]
        _write_jsonl(proj / "s.jsonl", recs)
        assert count_tool_retries() == 1

    def test_different_args_does_not_count(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        recs = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": f"/x/{i}"},
                        }
                    ],
                },
            }
            for i in range(5)
        ]
        _write_jsonl(proj / "s.jsonl", recs)
        # Different args each time → no storm
        assert count_tool_retries() == 0

    def test_two_identical_calls_not_a_storm(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # Threshold is 3 identical in a row. Two isn't a storm —
        # retrying once after a failure is normal.
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        same = {
            "type": "tool_use",
            "name": "Bash",
            "input": {"command": "pytest"},
        }
        recs = [
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [same]},
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [same]},
            },
        ]
        _write_jsonl(proj / "s.jsonl", recs)
        assert count_tool_retries() == 0

    def test_returns_zero_when_no_jsonl(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        assert count_tool_retries() == 0


class TestExtractDecisions:
    def test_pulls_h2_headed_assistant_messages(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": "## Summary\n\nFixed three bugs today.",
                            }
                        ],
                    },
                    "timestamp": "2026-05-17T10:30:00Z",
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "plain reply, no heading"}],
                    },
                    "timestamp": "2026-05-17T10:31:00Z",
                },
            ],
        )
        decisions = extract_decisions()
        assert len(decisions) == 1
        assert decisions[0]["heading"] == "Summary"

    def test_ignores_user_messages_with_h2(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # User messages can contain H2 too (e.g. when pasting docs)
        # but they're not "claude decided X" — only assistant H2s
        # qualify as decisions.
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "## Things to do"}],
                    },
                    "timestamp": "2026-05-17T10:00:00Z",
                }
            ],
        )
        assert extract_decisions() == []

    def test_h1_alone_does_not_qualify(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # H1 is for top-level reply titles (often boilerplate); only
        # H2 buckets a message as a decision.
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "# Big Title"}],
                    },
                    "timestamp": "2026-05-17T10:00:00Z",
                }
            ],
        )
        assert extract_decisions() == []

    def test_first_h2_wins_for_heading(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": "intro\n## First Decision\nbody\n## Second\nmore",
                            }
                        ],
                    },
                    "timestamp": "2026-05-17T10:00:00Z",
                }
            ],
        )
        decisions = extract_decisions()
        assert len(decisions) == 1
        assert decisions[0]["heading"] == "First Decision"

    def test_limit_caps_results(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        recs = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"## Decision {i}"}],
                },
                "timestamp": f"2026-05-17T10:{i:02d}:00Z",
            }
            for i in range(5)
        ]
        _write_jsonl(proj / "s.jsonl", recs)
        assert len(extract_decisions(limit=3)) == 3


class TestSearchSessions:
    def test_returns_matching_records(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "fix the bracketed paste bug"}],
                    },
                    "timestamp": "2026-05-17T10:30:00Z",
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "unrelated reply"}],
                    },
                    "timestamp": "2026-05-17T10:31:00Z",
                },
            ],
        )
        hits = search_sessions("bracketed paste")
        assert len(hits) == 1
        assert hits[0]["role"] == "user"
        assert "bracketed paste" in hits[0]["snippet"]

    def test_case_insensitive_match(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Playwright MCP setup"}],
                    },
                    "timestamp": "2026-05-17T10:00:00Z",
                }
            ],
        )
        assert len(search_sessions("playwright")) == 1
        assert len(search_sessions("PLAYWRIGHT")) == 1

    def test_results_sorted_most_recent_first(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": [{"type": "text", "text": "MCP older"}]},
                    "timestamp": "2026-05-10T10:00:00Z",
                },
                {
                    "type": "user",
                    "message": {"role": "user", "content": [{"type": "text", "text": "MCP newer"}]},
                    "timestamp": "2026-05-17T10:00:00Z",
                },
            ],
        )
        hits = search_sessions("MCP")
        assert "newer" in hits[0]["snippet"]
        assert "older" in hits[1]["snippet"]

    def test_limit_caps_results(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        recs = [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": f"hit {i}"}],
                },
                "timestamp": f"2026-05-17T10:{i:02d}:00Z",
            }
            for i in range(10)
        ]
        _write_jsonl(proj / "s.jsonl", recs)
        assert len(search_sessions("hit", limit=3)) == 3

    def test_empty_query_returns_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        assert search_sessions("") == []

    def test_project_filter_narrows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        base = tmp_path / ".claude" / "projects"
        keep = base / "C--Users-alice-agent-takkub"
        skip = base / "C--Users-alice-other"
        for d in (keep, skip):
            d.mkdir(parents=True)
        _write_jsonl(
            keep / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "playwright"}],
                    },
                    "timestamp": "2026-05-17T10:00:00Z",
                }
            ],
        )
        _write_jsonl(
            skip / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "playwright"}],
                    },
                    "timestamp": "2026-05-17T10:00:00Z",
                }
            ],
        )
        hits = search_sessions("playwright", project_filter="agent-takkub")
        assert len(hits) == 1
        assert "agent-takkub" in hits[0]["project"]

    def test_snippet_truncates_around_match(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        long_text = "A" * 500 + " needle " + "Z" * 500
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": long_text}],
                    },
                    "timestamp": "2026-05-17T10:00:00Z",
                }
            ],
        )
        hits = search_sessions("needle")
        assert "needle" in hits[0]["snippet"]
        # ~200 chars centred plus ellipses
        assert len(hits[0]["snippet"]) <= 250


class TestBuildResumeBrief:
    def test_empty_when_no_records(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        assert build_resume_brief() == ""

    def test_renders_chronological_bullets(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "first msg"}],
                    },
                    "timestamp": "2026-05-17T10:00:00Z",
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "second msg"}],
                    },
                    "timestamp": "2026-05-17T10:01:00Z",
                },
            ],
        )
        brief = build_resume_brief()
        # Oldest first → most-recent last
        first_idx = brief.index("first msg")
        second_idx = brief.index("second msg")
        assert first_idx < second_idx
        assert "**user**" in brief
        assert "**assistant**" in brief

    def test_caps_at_last_n(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        recs = [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": f"msg {i}"}],
                },
                "timestamp": f"2026-05-17T10:{i:02d}:00Z",
            }
            for i in range(15)
        ]
        _write_jsonl(proj / "s.jsonl", recs)
        brief = build_resume_brief(last_n=5)
        # Only the last 5 should appear; the first 10 should not
        for i in range(10):
            assert f"msg {i}\n" not in brief and f"msg {i} " not in brief
        for i in range(10, 15):
            assert f"msg {i}" in brief

    def test_long_lines_truncated_to_160(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        # Keeps the brief scannable; truncates per-line at 160 chars.
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        proj = tmp_path / ".claude" / "projects" / "C--Users-alice-foo"
        proj.mkdir(parents=True)
        long = "x" * 500
        _write_jsonl(
            proj / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": long}],
                    },
                    "timestamp": "2026-05-17T10:00:00Z",
                }
            ],
        )
        brief = build_resume_brief()
        # Pull the bullet's text portion (after the role marker)
        bullet = next(line for line in brief.splitlines() if line.startswith("- `"))
        # The bullet contains some prefix + the truncated x-run
        x_run = bullet.split("— ", 1)[1] if "— " in bullet else ""
        assert len(x_run) <= 160

    def test_project_filter_narrows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        base = tmp_path / ".claude" / "projects"
        keep = base / "C--Users-alice-agent-takkub"
        skip = base / "C--Users-alice-other"
        for d in (keep, skip):
            d.mkdir(parents=True)
        _write_jsonl(
            keep / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "in-scope"}],
                    },
                    "timestamp": "2026-05-17T10:00:00Z",
                }
            ],
        )
        _write_jsonl(
            skip / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "out-of-scope"}],
                    },
                    "timestamp": "2026-05-17T10:00:00Z",
                }
            ],
        )
        brief = build_resume_brief(project_filter="agent-takkub")
        assert "in-scope" in brief
        assert "out-of-scope" not in brief


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
        keep = base / "C--Users-alice-WebstormProjects-agent-takkub"
        skip = base / "C--Users-alice-OtherRepo"
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

    def test_claude_projects_dir_installed_instance_is_isolated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Installed builds must read *this instance's* claude-config, not a
        dev checkout's ~/.claude on the same machine (isolation plan,
        finding C5)."""
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path / "agent-takkub-home")
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")

        assert (
            claude_projects_dir() == tmp_path / "agent-takkub-home" / "claude-config" / "projects"
        )
