"""Core V2 Conversation ingest adapters (epic #309 Phase 6): claude/codex/
gemini/opencode transcript readers WRAP the existing per-provider readers
(`chatlog_scanner.py`, `codex_helper.py`, `gemini_helper.py`,
`opencode_helper.py`) with a byte-offset (or message-count, for opencode)
incremental cursor so ingest never double-counts a line already seen.

Codex's two schema forms (0.146 `agent_message`/`user_message` vs 0.147+
`item_completed`) are the D3 schema-drift lesson — both are exercised here
so a future flip fails a core test, not just the remote-mirror one.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_takkub import (
    chatlog_scanner,
    codex_helper,
    cursor_helper,
    gemini_helper,
    kimi_helper,
    opencode_helper,
)
from agent_takkub.core.conversation import ingest
from agent_takkub.core.conversation.ingest import (
    claude_adapter,
    codex_adapter,
    cursor_adapter,
    gemini_adapter,
    kimi_adapter,
)
from agent_takkub.core.conversation.ingest import opencode_adapter as opencode_ingest
from agent_takkub.core.conversation.ingest.cursor_store import get_cursor, set_cursor
from agent_takkub.core.conversation.store import ConversationStore
from agent_takkub.core.models.conversation import MessageRole

# ── registry ─────────────────────────────────────────────────────────────


def test_supported_providers_covers_all_6():
    # epic #309's core.conversation.ingest gap closed 2026-08-22 (plan §1.1):
    # cursor + kimi both now have real adapters, alongside the original 4.
    assert set(ingest.supported_providers()) == {
        "claude",
        "codex",
        "gemini",
        "opencode",
        "cursor",
        "kimi",
    }


def test_adapter_for_unknown_provider_is_none():
    assert ingest.adapter_for("not-a-real-provider") is None


# ── claude adapter ───────────────────────────────────────────────────────


def _claude_line(role: str, text: str) -> str:
    return json.dumps({"type": role, "message": {"role": role, "content": text}})


def test_claude_read_new_parses_user_and_assistant(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(_claude_line("user", "hello") + "\n" + _claude_line("assistant", "hi") + "\n")
    batch = claude_adapter.read_new(str(path), None)
    assert [(m.role, m.text) for m in batch.messages] == [
        (MessageRole.USER, "hello"),
        (MessageRole.ASSISTANT, "hi"),
    ]


def test_claude_read_new_is_idempotent_via_cursor(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(_claude_line("user", "hello") + "\n")
    first = claude_adapter.read_new(str(path), None)
    assert len(first.messages) == 1

    with open(path, "a") as f:
        f.write(_claude_line("assistant", "hi") + "\n")
    second = claude_adapter.read_new(str(path), first.next_cursor)
    assert [(m.role, m.text) for m in second.messages] == [(MessageRole.ASSISTANT, "hi")]


def test_claude_read_new_skips_non_conversation_records(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps({"type": "queue-operation"}) + "\n" + _claude_line("user", "real message") + "\n"
    )
    batch = claude_adapter.read_new(str(path), None)
    assert [m.text for m in batch.messages] == ["real message"]


def test_claude_read_new_missing_file_returns_empty(tmp_path):
    batch = claude_adapter.read_new(str(tmp_path / "missing.jsonl"), None)
    assert batch.messages == []


# ── codex adapter: both schema generations ─────────────────────────────


def test_codex_read_new_parses_legacy_schema(tmp_path):
    path = tmp_path / "rollout-1.jsonl"
    lines = [
        json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}}),
        json.dumps(
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "hello back"}}
        ),
    ]
    path.write_text("\n".join(lines) + "\n")
    batch = codex_adapter.read_new(str(path), None)
    assert [(m.role, m.text) for m in batch.messages] == [
        (MessageRole.USER, "hi"),
        (MessageRole.ASSISTANT, "hello back"),
    ]


def test_codex_read_new_parses_0147_item_completed_schema(tmp_path):
    path = tmp_path / "rollout-1.jsonl"
    lines = [
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "UserMessage", "content": [{"type": "text", "text": "hi"}]},
                },
            }
        ),
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "content": [{"type": "Text", "text": "hello back"}],
                    },
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")
    batch = codex_adapter.read_new(str(path), None)
    assert [(m.role, m.text) for m in batch.messages] == [
        (MessageRole.USER, "hi"),
        (MessageRole.ASSISTANT, "hello back"),
    ]


def test_codex_read_new_parses_0148_paginated_schema(tmp_path):
    """Regression guard for #319: codex 0.148.0's `migrate-rollouts`/background
    pagination rewrites `session_meta` to add a duplicate `session_id` key
    (alongside `id`) plus a new `history_mode` field, and stamps every record
    with an `ordinal` index — verified by migrating a real on-disk 0.147
    rollout to the paginated form with `codex migrate-rollouts --apply` and
    diffing the result. The `item_completed`/`AgentMessage`/`UserMessage`
    shape itself is UNCHANGED; this pins that the new/extra fields are
    harmlessly ignored rather than silently breaking the parse."""
    path = tmp_path / "rollout-1.jsonl"
    lines = [
        json.dumps(
            {
                "timestamp": "2026-08-20T08:00:00.000Z",
                "ordinal": 0,
                "type": "session_meta",
                "payload": {
                    "session_id": "01a01402-984c-7452-aa94-ae022605e438",
                    "id": "01a01402-984c-7452-aa94-ae022605e438",
                    "cwd": "/fake/project",
                    "originator": "codex-tui",
                    "cli_version": "0.148.0",
                    "history_mode": "paginated",
                },
            }
        ),
        json.dumps(
            {
                "timestamp": "2026-08-20T08:00:01.000Z",
                "ordinal": 1,
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "01a01402-984c-7452-aa94-ae022605e438",
                    "turn_id": "turn-1",
                    "completed_at_ms": 1787000000000,
                    "item": {
                        "type": "UserMessage",
                        "id": "item-1",
                        "content": [{"type": "text", "text": "hi"}],
                    },
                },
            }
        ),
        json.dumps(
            {
                "timestamp": "2026-08-20T08:00:02.000Z",
                "ordinal": 2,
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "01a01402-984c-7452-aa94-ae022605e438",
                    "turn_id": "turn-1",
                    "completed_at_ms": 1787000001000,
                    "item": {
                        "type": "AgentMessage",
                        "id": "item-2",
                        "content": [{"type": "Text", "text": "hello back"}],
                    },
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")
    batch = codex_adapter.read_new(str(path), None)
    assert [(m.role, m.text) for m in batch.messages] == [
        (MessageRole.USER, "hi"),
        (MessageRole.ASSISTANT, "hello back"),
    ]


def test_codex_read_new_mixed_schema_file_never_double_counts(tmp_path):
    """A file containing BOTH generations (codex exec still writes the old
    shape post-0.147 while codex-tui writes the new one) must parse each
    line exactly once via its own shape — never double-count a turn."""
    path = tmp_path / "rollout-1.jsonl"
    lines = [
        json.dumps(
            {"type": "event_msg", "payload": {"type": "user_message", "message": "old-style"}}
        ),
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "content": [{"type": "text", "text": "new-style"}],
                    },
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")
    batch = codex_adapter.read_new(str(path), None)
    assert [m.text for m in batch.messages] == ["old-style", "new-style"]
    assert len(batch.messages) == 2  # not 4 — each line contributes exactly one message


def test_codex_read_new_ignores_reasoning_and_command_items(tmp_path):
    path = tmp_path / "rollout-1.jsonl"
    line = json.dumps(
        {
            "type": "event_msg",
            "payload": {"type": "item_completed", "item": {"type": "Reasoning", "content": []}},
        }
    )
    path.write_text(line + "\n")
    batch = codex_adapter.read_new(str(path), None)
    assert batch.messages == []


def test_codex_resolve_source_falls_back_to_archived_sessions(tmp_path, monkeypatch):
    """`codex archive` (0.148+) moves a rollout out of `sessions/` into a flat
    `archived_sessions/` dir (verified against a real store, #319) — an
    exact id+cwd lookup must still find it there."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setattr(codex_adapter, "codex_sessions_root", lambda: tmp_path / "sessions")
    monkeypatch.setattr(
        codex_adapter, "codex_archived_sessions_root", lambda: tmp_path / "archived_sessions"
    )
    archived = tmp_path / "archived_sessions"
    archived.mkdir()
    path = archived / "rollout-archived.jsonl"
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "uuid-1", "cwd": str(cwd)}}) + "\n"
    )

    assert codex_adapter.resolve_source(str(cwd), "uuid-1") == str(path)


def test_codex_resolve_source_no_id_never_checks_archived_sessions(tmp_path, monkeypatch):
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setattr(codex_adapter, "codex_sessions_root", lambda: tmp_path / "sessions")
    monkeypatch.setattr(
        codex_adapter, "codex_archived_sessions_root", lambda: tmp_path / "archived_sessions"
    )
    archived = tmp_path / "archived_sessions"
    archived.mkdir()
    (archived / "rollout-archived.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "uuid-1", "cwd": str(cwd)}}) + "\n"
    )

    assert codex_adapter.resolve_source(str(cwd), None) is None


def test_codex_read_new_cursor_prevents_double_count(tmp_path):
    path = tmp_path / "rollout-1.jsonl"
    path.write_text(
        json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}})
        + "\n"
    )
    first = codex_adapter.read_new(str(path), None)
    second = codex_adapter.read_new(str(path), first.next_cursor)
    assert len(first.messages) == 1
    assert second.messages == []


# ── gemini adapter: antigravity + legacy shapes ─────────────────────────


def test_gemini_read_new_parses_antigravity_shape(tmp_path):
    path = tmp_path / "transcript.jsonl"
    lines = [
        json.dumps(
            {
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "content": "<USER_REQUEST>hi</USER_REQUEST>",
            }
        ),
        json.dumps({"type": "PLANNER_RESPONSE", "source": "MODEL", "content": "hello back"}),
    ]
    path.write_text("\n".join(lines) + "\n")
    batch = gemini_adapter.read_new(str(path), None)
    assert [(m.role, m.text) for m in batch.messages] == [
        (MessageRole.USER, "hi"),
        (MessageRole.ASSISTANT, "hello back"),
    ]


def test_gemini_read_new_parses_legacy_snapshot_shape(tmp_path):
    path = tmp_path / "chat.jsonl"
    snapshot = {
        "$set": {
            "messages": [
                {"id": "m1", "type": "user", "content": "hi"},
                {"id": "m2", "type": "gemini", "content": "hello back"},
            ]
        }
    }
    path.write_text(json.dumps(snapshot) + "\n")
    batch = gemini_adapter.read_new(str(path), None)
    assert [(m.role, m.text) for m in batch.messages] == [
        (MessageRole.USER, "hi"),
        (MessageRole.ASSISTANT, "hello back"),
    ]


def test_gemini_read_new_legacy_dedupes_repeated_snapshot_ids(tmp_path):
    path = tmp_path / "chat.jsonl"
    snapshot = {"$set": {"messages": [{"id": "m1", "type": "user", "content": "hi"}]}}
    # A snapshot record can repeat the same message id across multiple lines.
    path.write_text(json.dumps(snapshot) + "\n" + json.dumps(snapshot) + "\n")
    batch = gemini_adapter.read_new(str(path), None)
    assert len(batch.messages) == 1


def test_gemini_read_new_cursor_prevents_double_count(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps({"type": "PLANNER_RESPONSE", "content": "hello"}) + "\n")
    first = gemini_adapter.read_new(str(path), None)
    second = gemini_adapter.read_new(str(path), first.next_cursor)
    assert len(first.messages) == 1
    assert second.messages == []


# ── opencode adapter (message-count cursor) ─────────────────────────────


def test_opencode_read_new_slices_past_seen_count(monkeypatch):
    rows = [
        {"text": "hi", "kind": "me"},
        {"text": "hello back", "kind": "lead"},
        {"text": "another", "kind": "me"},
    ]
    monkeypatch.setattr(opencode_ingest, "read_opencode_session_messages", lambda *a, **kw: rows)
    batch = opencode_ingest.read_new(f"db.sqlite{opencode_ingest._SEP}sid-1", "2")
    assert [m.text for m in batch.messages] == ["another"]
    assert batch.next_cursor == "3"


def test_opencode_read_new_first_call_reads_everything(monkeypatch):
    rows = [{"text": "hi", "kind": "me"}]
    monkeypatch.setattr(opencode_ingest, "read_opencode_session_messages", lambda *a, **kw: rows)
    batch = opencode_ingest.read_new(f"db.sqlite{opencode_ingest._SEP}sid-1", None)
    assert len(batch.messages) == 1


# ── cursor adapter (full WRAP of cursor_helper.py — no duplication) ─────


def _cursor_line(role: str, text: str) -> str:
    return json.dumps({"role": role, "message": {"content": [{"type": "text", "text": text}]}})


def test_cursor_read_new_parses_user_and_assistant(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(_cursor_line("user", "hello") + "\n" + _cursor_line("assistant", "hi") + "\n")
    batch = cursor_adapter.read_new(str(path), None)
    assert [(m.role, m.text) for m in batch.messages] == [
        (MessageRole.USER, "hello"),
        (MessageRole.ASSISTANT, "hi"),
    ]


def test_cursor_read_new_is_idempotent_via_cursor(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(_cursor_line("user", "hello") + "\n")
    first = cursor_adapter.read_new(str(path), None)
    assert len(first.messages) == 1

    with open(path, "a") as f:
        f.write(_cursor_line("assistant", "hi") + "\n")
    second = cursor_adapter.read_new(str(path), first.next_cursor)
    assert [(m.role, m.text) for m in second.messages] == [(MessageRole.ASSISTANT, "hi")]


def test_cursor_read_new_skips_tool_use_only_turns(tmp_path):
    path = tmp_path / "session.jsonl"
    tool_only = json.dumps(
        {"role": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}}
    )
    path.write_text(tool_only + "\n" + _cursor_line("user", "real message") + "\n")
    batch = cursor_adapter.read_new(str(path), None)
    assert [m.text for m in batch.messages] == ["real message"]


def test_cursor_read_new_missing_file_returns_empty(tmp_path):
    batch = cursor_adapter.read_new(str(tmp_path / "missing.jsonl"), None)
    assert batch.messages == []


def test_cursor_resolve_source_uses_cursor_helper_resolver(tmp_path, monkeypatch):
    expected = tmp_path / "session.jsonl"
    monkeypatch.setattr(cursor_adapter, "resolve_cursor_jsonl_for_cwd", lambda cwd, sid: expected)
    assert cursor_adapter.resolve_source("/fake/cwd", "sid-1") == str(expected)


def test_cursor_resolve_source_none_when_no_transcript(monkeypatch):
    monkeypatch.setattr(cursor_adapter, "resolve_cursor_jsonl_for_cwd", lambda cwd, sid: None)
    assert cursor_adapter.resolve_source("/fake/cwd", None) is None


# ── kimi adapter (full WRAP of kimi_helper.py — no duplication) ─────────


def _kimi_turn_begin(text: str, ts: float = 1.0) -> str:
    return json.dumps(
        {"timestamp": ts, "message": {"type": "TurnBegin", "payload": {"user_input": text}}}
    )


def _kimi_text_part(text: str, ts: float = 2.0) -> str:
    return json.dumps(
        {
            "timestamp": ts,
            "message": {"type": "TextPart", "payload": {"type": "text", "text": text}},
        }
    )


def test_kimi_read_new_parses_user_and_assistant(tmp_path):
    path = tmp_path / "wire.jsonl"
    path.write_text(
        json.dumps({"type": "metadata", "protocol_version": "1.10"})
        + "\n"
        + _kimi_turn_begin("hello")
        + "\n"
        + _kimi_text_part("hi")
        + "\n"
        + json.dumps({"timestamp": 3.0, "message": {"type": "TurnEnd", "payload": {}}})
        + "\n"
    )
    batch = kimi_adapter.read_new(str(path), None)
    assert [(m.role, m.text) for m in batch.messages] == [
        (MessageRole.USER, "hello"),
        (MessageRole.ASSISTANT, "hi"),
    ]


def test_kimi_read_new_is_idempotent_via_cursor(tmp_path):
    path = tmp_path / "wire.jsonl"
    path.write_text(_kimi_turn_begin("hello") + "\n")
    first = kimi_adapter.read_new(str(path), None)
    assert len(first.messages) == 1

    with open(path, "a") as f:
        f.write(_kimi_text_part("hi") + "\n")
    second = kimi_adapter.read_new(str(path), first.next_cursor)
    assert [(m.role, m.text) for m in second.messages] == [(MessageRole.ASSISTANT, "hi")]


def test_kimi_read_new_never_surfaces_think_part(tmp_path):
    """Hidden reasoning must never reach the conversation store — same rule
    as the mobile mirror's "text only" non-negotiable."""
    path = tmp_path / "wire.jsonl"
    think = json.dumps(
        {
            "timestamp": 1.5,
            "message": {
                "type": "ThinkPart",
                "payload": {"type": "think", "think": "secret reasoning", "encrypted": None},
            },
        }
    )
    path.write_text(_kimi_turn_begin("hello") + "\n" + think + "\n" + _kimi_text_part("hi") + "\n")
    batch = kimi_adapter.read_new(str(path), None)
    assert [m.text for m in batch.messages] == ["hello", "hi"]


def test_kimi_read_new_missing_file_returns_empty(tmp_path):
    batch = kimi_adapter.read_new(str(tmp_path / "missing.jsonl"), None)
    assert batch.messages == []


def test_kimi_resolve_source_uses_kimi_helper_resolver(tmp_path, monkeypatch):
    session_dir = tmp_path / "session-1"
    session_dir.mkdir()
    monkeypatch.setattr(kimi_adapter, "resolve_kimi_session_dir", lambda cwd, sid: session_dir)
    assert kimi_adapter.resolve_source("/fake/cwd", "session-1") == str(session_dir / "wire.jsonl")


def test_kimi_resolve_source_none_when_no_session(monkeypatch):
    monkeypatch.setattr(kimi_adapter, "resolve_kimi_session_dir", lambda cwd, sid: None)
    assert kimi_adapter.resolve_source("/fake/cwd", None) is None


# ── ingest_provider_transcript: cursor persists across calls ────────────


def test_ingest_provider_transcript_end_to_end_no_double_count(tmp_path, monkeypatch):
    cursor_path = tmp_path / "cursors.json"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_claude_line("user", "hello") + "\n")
    monkeypatch.setattr(claude_adapter, "resolve_source", lambda cwd, sid, **kw: str(transcript))

    store = ConversationStore(root=tmp_path / "conversations")
    result1 = ingest.ingest_provider_transcript(
        "claude", "proj-a", "conv-1", cwd="/fake/cwd", store=store, cursor_path=cursor_path
    )
    assert result1.ingested == 1

    with open(transcript, "a") as f:
        f.write(_claude_line("assistant", "hi") + "\n")
    result2 = ingest.ingest_provider_transcript(
        "claude", "proj-a", "conv-1", cwd="/fake/cwd", store=store, cursor_path=cursor_path
    )
    assert result2.ingested == 1  # only the NEW line, never the first one again

    messages = store.read_messages("proj-a", "conv-1")
    assert [m.text for m in messages] == ["hello", "hi"]


def test_ingest_provider_transcript_no_source_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_adapter, "resolve_source", lambda cwd, sid, **kw: None)
    store = ConversationStore(root=tmp_path / "conversations")
    result = ingest.ingest_provider_transcript(
        "claude",
        "proj-a",
        "conv-1",
        cwd="/fake/cwd",
        store=store,
        cursor_path=tmp_path / "cursors.json",
    )
    assert result.skipped_no_source is True
    assert store.read_messages("proj-a", "conv-1") == []


def test_ingest_provider_transcript_unknown_provider_is_skipped(tmp_path):
    store = ConversationStore(root=tmp_path / "conversations")
    result = ingest.ingest_provider_transcript(
        "not-a-real-provider",
        "proj-a",
        "conv-1",
        cwd="/fake/cwd",
        store=store,
        cursor_path=tmp_path / "cursors.json",
    )
    assert result.skipped_no_source is True


def test_cursor_store_round_trip(tmp_path):
    path = tmp_path / "cursors.json"
    assert get_cursor("claude", "src-1", path=path) is None
    set_cursor("claude", "src-1", "42", path=path)
    assert get_cursor("claude", "src-1", path=path) == "42"
    # a different source under the same provider is independent
    assert get_cursor("claude", "src-2", path=path) is None


# ── real-store smoke tests: skipped when the provider isn't installed ───


def test_claude_adapter_reads_a_real_local_session_if_present():
    base = chatlog_scanner.claude_projects_dir()
    if not base.is_dir():
        pytest.skip("no local claude projects store on this machine")
    files = [p for d in base.iterdir() if d.is_dir() for p in d.glob("*.jsonl")]
    if not files:
        pytest.skip("claude projects store has no session files")
    batch = claude_adapter.read_new(str(files[0]), None)
    assert isinstance(batch.messages, list)


def test_codex_adapter_reads_a_real_local_session_if_present():
    root = codex_helper.codex_sessions_root()
    if not root.is_dir():
        pytest.skip("no local codex sessions store on this machine")
    files = list(root.rglob("rollout-*.jsonl"))
    if not files:
        pytest.skip("codex sessions store has no rollout files")
    batch = codex_adapter.read_new(str(files[0]), None)
    assert isinstance(batch.messages, list)


def test_gemini_adapter_reads_a_real_local_session_if_present():
    root = gemini_helper.antigravity_root()
    if not root.is_dir():
        pytest.skip("no local agy antigravity store on this machine")
    files = list(root.rglob("transcript.jsonl"))
    if not files:
        pytest.skip("agy store has no transcript files")
    batch = gemini_adapter.read_new(str(files[0]), None)
    assert isinstance(batch.messages, list)


def test_opencode_adapter_reads_a_real_local_db_if_present():
    db_path = opencode_helper.opencode_db_path()
    if db_path is None or not db_path.is_file():
        pytest.skip("no local opencode.db on this machine")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
        cur = conn.cursor()
        cur.execute("SELECT id FROM session LIMIT 1")
        row = cur.fetchone()
        conn.close()
    except sqlite3.Error:
        pytest.skip("opencode.db not readable")
    if row is None:
        pytest.skip("opencode.db has no sessions")
    source_id = f"{db_path}{opencode_ingest._SEP}{row[0]}"
    batch = opencode_ingest.read_new(source_id, None)
    assert isinstance(batch.messages, list)


def test_cursor_adapter_reads_a_real_local_session_if_present():
    root = cursor_helper.cursor_projects_root()
    if not root.is_dir():
        pytest.skip("no local cursor projects store on this machine")
    files = list(root.rglob("*.jsonl"))
    if not files:
        pytest.skip("cursor projects store has no transcript files")
    batch = cursor_adapter.read_new(str(files[0]), None)
    assert isinstance(batch.messages, list)


def test_kimi_adapter_reads_a_real_local_session_if_present():
    """Verified live on this machine (2026-08-22, plan §1.1 investigation):
    resolves a real prior teammate task's `TurnBegin` from `~/.kimi`. No
    `TextPart` (assistant reply) has been observed here yet — see
    `kimi_helper.py`'s module docstring for why — so this only pins that a
    real user turn parses; it can't yet pin the assistant branch against a
    live example."""
    root = kimi_helper.kimi_share_dir() / "sessions"
    if not root.is_dir():
        pytest.skip("no local kimi sessions store on this machine")
    files = list(root.rglob("wire.jsonl"))
    if not files:
        pytest.skip("kimi sessions store has no wire.jsonl files")
    batch = kimi_adapter.read_new(str(files[0]), None)
    assert isinstance(batch.messages, list)
