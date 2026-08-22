"""Tests for `agent_takkub.remote.notify.LeadNotifier` (§6.5, X-check 2.1):
hooks `orch.agentDone` -> SSE `done` events, and tails each open project's
Lead pane **structured session JSONL** (not raw PTY bytes) -> SSE `lead`
events (mobile junk-elimination rewrite).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal

from agent_takkub import gemini_helper
from agent_takkub.remote import config as remote_config
from agent_takkub.remote import notify as notify_mod
from agent_takkub.remote.notify import LeadNotifier, _lead_text_blocks

from ._qt_timer_leak_guard import stop_timers_after


class _PaneState:
    def __init__(self, session_uuid: str | None) -> None:
        self.session_uuid = session_uuid


class _FakePane:
    """Placeholder — presence under panes_by_project[project]["lead"] is all
    the notifier checks; the actual session object lives in `_pane_state`."""

    def __init__(self, provider: str = "claude") -> None:
        self.model = type("FakePaneModel", (), {"provider_name": provider, "spawn_ts": 0.0})()
        self.state = "idle"
        self.session = None


class _FakeScreenSession:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def display_lines(self) -> list[str]:
        return list(self.lines)


class _FakeOrch(QObject):
    agentDone = pyqtSignal(str, str, str)
    statusChanged = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._panes_by_project: dict = {}
        self._pane_state: dict = {}

    def set_lead(self, project: str, session_uuid: str | None, provider: str = "claude") -> None:
        self._panes_by_project.setdefault(project, {})["lead"] = _FakePane(provider)
        self._pane_state[f"{project}::lead"] = _PaneState(session_uuid)

    def drop_project(self, project: str) -> None:
        self._panes_by_project.pop(project, None)
        self._pane_state.pop(f"{project}::lead", None)


class _FakeBroadcaster:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str | None]] = []

    def push(self, event: str, data: str, project_ns: str | None = None) -> None:
        self.events.append((event, data, project_ns))


class _FakeMonotonicClock:
    """Stand-in for the `time` module `_resync()` reads via `time.monotonic()`
    — controllable so a test can cross the `_UUIDLESS_RESYNC_THROTTLE_S`
    boundary deterministically instead of sleeping."""

    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value


@pytest.fixture
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture(autouse=True)
def _stop_lead_notifier_timers(monkeypatch):
    # LeadNotifier.__init__ starts self._timer (poll) unconditionally (#344)
    # — every `LeadNotifier(...)` in this file otherwise leaves one running
    # for the rest of the pytest session.
    finalize = stop_timers_after(monkeypatch, LeadNotifier, "_timer")
    yield
    finalize()


def _assistant_line(*texts: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": t} for t in texts],
            },
        }
    )


def _tool_use_line() -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Read", "input": {"file": "x.py"}}],
            },
        }
    )


def _ask_user_question_line(question: str = "ต่อยังไงดี?") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "AskUserQuestion",
                        "input": {
                            "questions": [
                                {
                                    "question": question,
                                    "header": "next step",
                                    "options": [
                                        {"label": "A", "description": "do A"},
                                        {"label": "B", "description": "do B"},
                                    ],
                                }
                            ]
                        },
                    }
                ],
            },
        }
    )


def _thinking_line() -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}]},
        }
    )


def _user_line(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
    )


def _user_string_line(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


def _tool_result_line() -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "abc",
                        "content": [{"type": "text", "text": "file contents"}],
                    }
                ],
            },
        }
    )


def _meta_user_line(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "isMeta": True,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
    )


def _command_wrapper_line(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": text},
        }
    )


def _write_jsonl(tmp_path, project_dir: str, uuid: str, lines: list[str]):
    d = tmp_path / "claude-config" / "projects" / project_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{uuid}.jsonl"
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return p


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Point every project at the same tmp CLAUDE_CONFIG_DIR (`config_dir_for`
    monkeypatched at the notify module's import site)."""

    def _fake_config_dir_for(project: str):
        return tmp_path / "claude-config"

    monkeypatch.setattr(notify_mod, "config_dir_for", _fake_config_dir_for)
    return tmp_path / "claude-config"


class TestLeadTextBlocks:
    def test_extracts_text_blocks_from_assistant_record(self):
        rec = json.loads(_assistant_line("hello lead"))
        assert _lead_text_blocks(rec) == ["hello lead"]

    def test_skips_tool_use_blocks(self):
        rec = json.loads(_tool_use_line())
        assert _lead_text_blocks(rec) == []

    def test_skips_thinking_blocks(self):
        rec = json.loads(_thinking_line())
        assert _lead_text_blocks(rec) == []

    def test_skips_non_assistant_records(self):
        rec = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        }
        assert _lead_text_blocks(rec) == []

    def test_skips_system_and_bookkeeping_records(self):
        assert _lead_text_blocks({"type": "system", "content": "reminder"}) == []
        assert _lead_text_blocks({"type": "queue-operation"}) == []


class TestDoneEvents:
    def test_lead_done_pushes_to_broadcaster(self, qapp):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.agentDone.emit("proj", "lead", "shipped /auth/login")
            assert broadcaster.events == [("done", "lead: shipped /auth/login", "proj")]
        finally:
            notifier.stop()

    def test_done_from_a_different_project_is_stamped_with_its_own_namespace(self, qapp):
        # H-A: `agentDone` fires for every project, not just whichever one
        # is active — the notifier must forward the *event's* project, so
        # the broadcaster (not the notifier) is what keeps it from leaking
        # into a different project's SSE client.
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.agentDone.emit("other-proj", "lead", "did a thing")
            assert broadcaster.events == [("done", "lead: did a thing", "other-proj")]
        finally:
            notifier.stop()

    @pytest.mark.parametrize("role", ["backend", "frontend", "qa", "reviewer", "qa#2"])
    def test_teammate_done_is_dropped_when_lead_only(self, qapp, role):
        """LEAD_ONLY_STREAM (2026-07-23): the phone mirrors Lead only. A
        teammate's `takkub done` reaches Lead as a handoff and surfaces in
        Lead's own reply — pushing it separately was duplicate noise."""
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.agentDone.emit("proj", role, "did a thing")
            assert broadcaster.events == []
        finally:
            notifier.stop()

    def test_teammate_done_still_pushes_when_flag_off(self, qapp, monkeypatch):
        """The switch is real, not a hard-coded deletion — flipping it back
        restores the whole-team stream."""
        monkeypatch.setattr(remote_config, "LEAD_ONLY_STREAM", False)
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.agentDone.emit("proj", "backend", "added /auth/login")
            assert broadcaster.events == [("done", "backend: added /auth/login", "proj")]
        finally:
            notifier.stop()


class TestTailStartOffset:
    def test_empty_file_starts_at_zero(self, tmp_path):
        path = tmp_path / "f.jsonl"
        path.write_bytes(b"")
        assert notify_mod._tail_start_offset(path, 0) == 0

    def test_file_ending_in_newline_starts_at_eof(self, tmp_path):
        path = tmp_path / "f.jsonl"
        path.write_bytes(b'{"a":1}\n')
        size = path.stat().st_size
        assert notify_mod._tail_start_offset(path, size) == size

    def test_file_with_incomplete_trailing_line_backs_up_to_previous_newline(self, tmp_path):
        path = tmp_path / "f.jsonl"
        path.write_bytes(b'{"a":1}\n{"b":2')  # no trailing newline
        size = path.stat().st_size
        assert notify_mod._tail_start_offset(path, size) == len(b'{"a":1}\n')

    def test_incomplete_first_line_backs_up_to_zero(self, tmp_path):
        path = tmp_path / "f.jsonl"
        path.write_bytes(b'{"a":1')  # no newline anywhere yet
        size = path.stat().st_size
        assert notify_mod._tail_start_offset(path, size) == 0


class TestLeadWorkingIndicator:
    """`_emit_lead_working_transitions` drives the phone's persistent "…" off
    the Lead pane's own `state == "working"` (desktop-spinner signal), pushing
    a 'working'/'idle' SSE event only on change."""

    def test_transitions_emit_only_on_change(self, qapp):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-1")
            pane = orch._panes_by_project["proj"]["lead"]

            pane.state = "idle"
            broadcaster.events.clear()
            notifier._emit_lead_working_transitions()
            assert broadcaster.events == []  # idle matches default → silent

            pane.state = "working"
            notifier._emit_lead_working_transitions()
            assert broadcaster.events == [("working", "", "proj")]

            broadcaster.events.clear()
            notifier._emit_lead_working_transitions()
            assert broadcaster.events == []  # still working → no repeat

            pane.state = "working"  # unchanged
            notifier._emit_lead_working_transitions()
            assert broadcaster.events == []

            pane.state = "idle"
            notifier._emit_lead_working_transitions()
            assert broadcaster.events == [("idle", "", "proj")]
        finally:
            notifier.stop()

    def test_closed_lead_pane_is_forgotten_without_idle_spam(self, qapp):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-1")
            orch._panes_by_project["proj"]["lead"].state = "working"
            notifier._emit_lead_working_transitions()
            assert ("working", "", "proj") in broadcaster.events
            orch.drop_project("proj")
            broadcaster.events.clear()
            notifier._emit_lead_working_transitions()
            assert broadcaster.events == []
            assert "proj" not in notifier._lead_working
        finally:
            notifier.stop()


class TestExactSessionResolutionOnly:
    """The mobile console is a *mirror* of the desktop Lead pane, so
    `_resolve_jsonl_path` returns the pane's exact recorded session or None —
    never a guess. An earlier newest-jsonl fallback (removed) dug up an
    unrelated old session on the phone when the pane had no current
    conversation (fresh open, nothing resumed), which broke the mirror. A real
    session-id drift is fixed at its source (keep pane_state.session_uuid
    accurate), never papered over with a newest-file guess here."""

    def test_exact_uuid_resolves_even_when_another_is_newer(self, tmp_path, config_dir):
        real = _write_jsonl(tmp_path, "C--proj", "real-uuid", [])
        newer = _write_jsonl(tmp_path, "C--proj", "newer-uuid", [])
        os.utime(real, (1000, 1000))
        os.utime(newer, (2000, 2000))
        # exact-uuid hit wins even though another file is newer — no mtime race
        assert notify_mod._resolve_jsonl_path("proj", "real-uuid") == real

    def test_missing_uuid_returns_none_never_guesses(self, tmp_path, config_dir):
        # A newer, unrelated session sits in the same dir — the removed
        # fallback would have surfaced it on the phone. Now a miss is a miss:
        # the mirror shows nothing, not a stale old conversation.
        other = _write_jsonl(tmp_path, "C--proj", "old-session", [_assistant_line("stale")])
        os.utime(other, (9999, 9999))
        assert notify_mod._resolve_jsonl_path("proj", "ghost-uuid") is None

    def test_no_jsonl_anywhere_returns_none(self, config_dir):
        assert notify_mod._resolve_jsonl_path("proj", "ghost-uuid") is None

    def test_history_endpoint_returns_none_on_missing_session(self, tmp_path, config_dir):
        # resolve_lead_jsonl (the /api/lead/history path): a recorded-but-
        # missing uuid yields None — the phone stays blank until the user
        # actually resumes, it never digs up an unrelated old file.
        orch = _FakeOrch()
        orch.set_lead("proj", "ghost-uuid")
        _write_jsonl(tmp_path, "C--proj", "unrelated-old", [_assistant_line("stale")])
        assert notify_mod.resolve_lead_jsonl(orch, "proj") is None

    def test_non_claude_lead_never_falls_through_to_matching_claude_jsonl(
        self, tmp_path, config_dir
    ):
        orch = _FakeOrch()
        orch.set_lead("proj", "same-uuid", provider="codex")
        _write_jsonl(tmp_path, "C--proj", "same-uuid", [_assistant_line("claude secret")])

        assert notify_mod.resolve_lead_jsonl(orch, "proj") is None
        assert notify_mod.lead_history_snapshot(orch, "proj", 20) == ("codex", [])
        assert notify_mod.lead_sessions_snapshot(orch, "proj", 20) == ("codex", [])


class TestLeadHistoryHelpers:
    def test_resolve_lead_jsonl_returns_none_without_open_lead_pane(self, config_dir):
        orch = _FakeOrch()
        assert notify_mod.resolve_lead_jsonl(orch, "proj") is None

    def test_resolve_lead_jsonl_finds_the_session_file(self, tmp_path, config_dir):
        orch = _FakeOrch()
        orch.set_lead("proj", "uuid-1")
        path = _write_jsonl(tmp_path, "C--proj", "uuid-1", [])
        assert notify_mod.resolve_lead_jsonl(orch, "proj") == path

    def test_read_recent_lead_messages_returns_oldest_first_and_respects_limit(
        self, tmp_path, config_dir
    ):
        path = _write_jsonl(
            tmp_path,
            "C--proj",
            "uuid-1",
            [
                _assistant_line("one"),
                _tool_use_line(),
                _assistant_line("two"),
                _assistant_line("three"),
            ],
        )
        assert notify_mod.read_recent_lead_messages(path) == [
            {"text": "one", "kind": "lead"},
            {"text": "two", "kind": "lead"},
            {"text": "three", "kind": "lead"},
        ]
        assert notify_mod.read_recent_lead_messages(path, limit=2) == [
            {"text": "two", "kind": "lead"},
            {"text": "three", "kind": "lead"},
        ]

    def test_read_recent_lead_messages_missing_file_is_empty(self, tmp_path):
        assert notify_mod.read_recent_lead_messages(tmp_path / "missing.jsonl") == []

    def test_read_recent_lead_messages_interleaves_user_and_assistant_in_order(
        self, tmp_path, config_dir
    ):
        path = _write_jsonl(
            tmp_path,
            "C--proj",
            "uuid-1",
            [
                _user_line("[remote → lead] hi lead"),
                _assistant_line("hi there"),
                _user_string_line("do the thing"),
                _tool_use_line(),
                _tool_result_line(),
                _assistant_line("done"),
            ],
        )
        assert notify_mod.read_recent_lead_messages(path) == [
            {"text": "hi lead", "kind": "me"},
            {"text": "hi there", "kind": "lead"},
            {"text": "do the thing", "kind": "me"},
            {"text": "done", "kind": "lead"},
        ]

    def test_read_recent_lead_messages_strips_remote_prefix_only_at_the_start(
        self, tmp_path, config_dir
    ):
        path = _write_jsonl(
            tmp_path,
            "C--proj",
            "uuid-1",
            [_user_line("[remote → lead] not [remote → lead] twice")],
        )
        assert notify_mod.read_recent_lead_messages(path) == [
            {"text": "not [remote → lead] twice", "kind": "me"}
        ]

    def test_read_recent_lead_messages_skips_tool_result_only_user_record(
        self, tmp_path, config_dir
    ):
        path = _write_jsonl(tmp_path, "C--proj", "uuid-1", [_tool_result_line()])
        assert notify_mod.read_recent_lead_messages(path) == []

    def test_read_recent_lead_messages_skips_meta_records(self, tmp_path, config_dir):
        path = _write_jsonl(
            tmp_path,
            "C--proj",
            "uuid-1",
            [
                _meta_user_line(
                    r"[Image: source: C:\Users\alice\.claude-work\image-cache\abc\1.png]"
                ),
                _meta_user_line("Continue from where you left off."),
                _user_line("real question"),
            ],
        )
        assert notify_mod.read_recent_lead_messages(path) == [
            {"text": "real question", "kind": "me"}
        ]


class TestLeadMirrorDiagnosis:
    """#192: classify *why* the phone has nothing to mirror instead of a
    silent blank chat. Covers the same three layers `takkub doctor --live`
    already proves out (`test_remote_mirror_diagnostics.py`), computed
    in-process here rather than through the cli_server round trip."""

    def test_no_scanner_is_provider_unsupported(self, config_dir):
        orch = _FakeOrch()
        orch.set_lead("proj", None, provider="kimi")
        result = notify_mod.lead_mirror_diagnosis(orch, "proj")
        assert result == {"code": "provider_unsupported", "provider": "kimi"}

    def test_claude_without_session_uuid_is_no_session_uuid(self, config_dir):
        orch = _FakeOrch()
        orch.set_lead("proj", None, provider="claude")
        result = notify_mod.lead_mirror_diagnosis(orch, "proj")
        assert result == {"code": "no_session_uuid", "provider": "claude"}

    def test_claude_uuid_set_but_no_matching_file_is_transcript_missing(self, tmp_path, config_dir):
        orch = _FakeOrch()
        orch.set_lead("proj", "ghost-uuid-1234", provider="claude")
        result = notify_mod.lead_mirror_diagnosis(orch, "proj")
        assert result == {
            "code": "transcript_missing",
            "provider": "claude",
            "session_uuid_short": "ghost-uu",
        }

    def test_claude_uuid_resolves_is_no_reason(self, tmp_path, config_dir):
        orch = _FakeOrch()
        orch.set_lead("proj", "uuid-1", provider="claude")
        _write_jsonl(tmp_path, "C--proj", "uuid-1", [])
        result = notify_mod.lead_mirror_diagnosis(orch, "proj")
        assert result == {"code": None, "provider": "claude"}

    def test_provider_without_uuid_requirement_missing_file_is_transcript_missing(self, config_dir):
        # codex/gemini pick their own file by cwd+mtime rather than an exact
        # uuid — a fresh pane or a cwd mismatch resolves to None the same as
        # claude's drift case, just without a uuid to shorten.
        orch = _FakeOrch()
        orch.set_lead("proj", None, provider="codex")
        result = notify_mod.lead_mirror_diagnosis(orch, "proj")
        assert result == {"code": "transcript_missing", "provider": "codex"}

    def test_claude_file_resolves_with_content_but_zero_records_is_unreadable(
        self, tmp_path, config_dir
    ):
        """#348: the file exists, is not empty, and resolution succeeded —
        but every line failed to parse into a message. That is the sharpest
        available signal of upstream schema drift, not "no messages yet"."""
        orch = _FakeOrch()
        orch.set_lead("proj", "uuid-1", provider="claude")
        drifted_lines = [
            json.dumps({"type": "future_record_shape", "body": "x" * 200}) for _ in range(30)
        ]
        _write_jsonl(tmp_path, "C--proj", "uuid-1", drifted_lines)
        result = notify_mod.lead_mirror_diagnosis(orch, "proj")
        assert result == {
            "code": "transcript_unreadable",
            "provider": "claude",
            "session_uuid_short": "uuid-1",
        }

    def test_claude_small_unparseable_file_is_not_flagged_as_drift(self, tmp_path, config_dir):
        """A handful of bytes that fail to parse into a message (well under
        the drift threshold) reads as "no messages yet", not a fault — this
        guards against crying wolf on a session that only just started."""
        orch = _FakeOrch()
        orch.set_lead("proj", "uuid-1", provider="claude")
        _write_jsonl(
            tmp_path, "C--proj", "uuid-1", [json.dumps({"type": "session_meta", "id": "uuid-1"})]
        )
        result = notify_mod.lead_mirror_diagnosis(orch, "proj")
        assert result == {"code": None, "provider": "claude"}


class TestTranscriptUnreadableGuard:
    """#348 centralized guard exercised directly against every registered
    scanner, without needing each provider's full spawn/resolver plumbing —
    `_transcript_unreadable` only ever calls `scanner.read_messages`, so this
    is the same code path `lead_mirror_diagnosis` drives in production."""

    @pytest.mark.parametrize("provider", ["claude", "gemini", "codex", "cursor"])
    def test_content_that_parses_to_nothing_is_flagged(self, tmp_path, provider):
        scanner = notify_mod.history_scanner(provider)
        path = tmp_path / f"{provider}.transcript"
        drifted = "\n".join(
            json.dumps({"totally": "new-shape", "pad": "x" * 200}) for _ in range(30)
        )
        path.write_text(drifted + "\n", encoding="utf-8")
        assert notify_mod._transcript_unreadable(scanner, path, "proj") is True

    @pytest.mark.parametrize("provider", ["claude", "gemini", "codex", "cursor"])
    def test_empty_file_is_not_flagged(self, tmp_path, provider):
        scanner = notify_mod.history_scanner(provider)
        path = tmp_path / f"{provider}.transcript"
        path.write_text("", encoding="utf-8")
        assert notify_mod._transcript_unreadable(scanner, path, "proj") is False

    def test_opencodes_shared_store_is_never_flagged(self, tmp_path):
        """OpenCode keeps every project's sessions in ONE sqlite db — a
        non-empty file with zero rows *for this session* is the normal shape
        of a brand-new session there, not drift (unlike the per-session
        files every other scanner resolves to)."""
        scanner = notify_mod.history_scanner("opencode")
        path = tmp_path / "opencode.db"
        path.write_text("x" * 10_000, encoding="utf-8")
        assert notify_mod._transcript_unreadable(scanner, path, "proj") is False


class TestGeminiHistoryHelpers:
    @staticmethod
    def _chat_store(home: Path, cwd: Path) -> Path:
        project_dir = home / ".gemini" / "tmp" / cwd.name
        chats = project_dir / "chats"
        chats.mkdir(parents=True, exist_ok=True)
        (project_dir / ".project_root").write_text(str(cwd), encoding="utf-8")
        return chats

    @staticmethod
    def _write_session(chats: Path, uuid: str, suffix: str, records: list[dict]) -> Path:
        path = chats / f"session-2026-08-10T10-{suffix}-{uuid[:8]}.jsonl"
        lines = [
            json.dumps(
                {
                    "sessionId": uuid,
                    "projectHash": "hash",
                    "startTime": "2026-08-10T03:00:00Z",
                    "kind": "main",
                }
            ),
            *[json.dumps(record) for record in records],
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    @pytest.fixture(autouse=True)
    def _isolate_gemini(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path, Path]:
        home = tmp_path / "home"
        cwd = tmp_path / "project"
        cwd.mkdir()
        chats = self._chat_store(home, cwd)
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setattr("agent_takkub.config.lead_cwd", lambda project=None: str(cwd))
        gemini_helper._gemini_chats_cache.clear()
        self.home, self.cwd, self.chats = home, cwd, chats
        yield home, cwd, chats
        gemini_helper._gemini_chats_cache.clear()

    def test_remote_resolver_delegates_to_provider_core(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = self.chats / "delegated.jsonl"
        seen: dict[str, str | None] = {}

        def _resolve(cwd: str, session_uuid: str | None) -> Path:
            seen.update(cwd=cwd, session_uuid=session_uuid)
            return marker

        monkeypatch.setattr(gemini_helper, "resolve_gemini_jsonl_for_cwd", _resolve)

        assert notify_mod._resolve_gemini_jsonl_for_cwd(str(self.cwd), "session-id") == marker
        assert seen == {"cwd": str(self.cwd), "session_uuid": "session-id"}

    def test_exact_resolver_confirms_full_uuid_not_only_filename_prefix(self) -> None:
        uuid_a = "12345678-aaaa-4444-8888-aaaaaaaaaaaa"
        uuid_b = "12345678-bbbb-4444-8888-bbbbbbbbbbbb"
        path_a = self._write_session(self.chats, uuid_a, "a", [])
        path_b = self._write_session(self.chats, uuid_b, "b", [])

        assert notify_mod._resolve_gemini_jsonl_for_cwd(str(self.cwd), uuid_a) == path_a
        assert notify_mod._resolve_gemini_jsonl_for_cwd(str(self.cwd), uuid_b) == path_b
        assert (
            notify_mod._resolve_gemini_jsonl_for_cwd(
                str(self.cwd), "12345678-cccc-4444-8888-cccccccccccc"
            )
            is None
        )

    def test_initial_store_miss_is_rescanned_after_agy_creates_chats(self) -> None:
        late_cwd = self.cwd.parent / "late-project"
        late_cwd.mkdir()
        assert notify_mod._find_gemini_chats_dir(str(late_cwd)) is None

        late_chats = self._chat_store(self.home, late_cwd)
        assert notify_mod._find_gemini_chats_dir(str(late_cwd)) == late_chats

    def test_positive_store_hit_is_reused_without_rescan(self) -> None:
        assert notify_mod._find_gemini_chats_dir(str(self.cwd)) == self.chats
        (self.chats.parent / ".project_root").unlink()

        assert notify_mod._find_gemini_chats_dir(str(self.cwd)) == self.chats

    def test_reads_snapshot_and_incremental_array_content(self) -> None:
        path = self._write_session(
            self.chats,
            "abcdef12-aaaa-4444-8888-aaaaaaaaaaaa",
            "messages",
            [
                {
                    "$set": {
                        "messages": [
                            {
                                "id": "context",
                                "type": "user",
                                "content": [{"text": "<session_context>hidden"}],
                            },
                            {
                                "id": "user-1",
                                "type": "user",
                                "content": [{"text": "[remote → lead] hello"}],
                            },
                        ]
                    }
                },
                {
                    "id": "gemini-1",
                    "type": "gemini",
                    "content": ["answer", {"functionCall": {"name": "tool"}}],
                },
                {
                    "id": "tool-result",
                    "type": "user",
                    "content": [{"functionResponse": {"name": "tool"}}],
                },
            ],
        )

        assert notify_mod.read_recent_lead_messages(path, provider="gemini") == [
            {"text": "hello", "kind": "me"},
            {"text": "answer", "kind": "lead"},
        ]

    def test_lists_gemini_sessions_and_filters_teammate_tasks(self) -> None:
        lead_uuid = "aaaaaaaa-aaaa-4444-8888-aaaaaaaaaaaa"
        teammate_uuid = "bbbbbbbb-bbbb-4444-8888-bbbbbbbbbbbb"
        self._write_session(
            self.chats,
            lead_uuid,
            "lead",
            [{"id": "u1", "type": "user", "content": [{"text": "real task"}]}],
        )
        self._write_session(
            self.chats,
            teammate_uuid,
            "teammate",
            [
                {
                    "id": "u2",
                    "type": "user",
                    "content": [{"text": "[ROLE: qa] smoke test"}],
                }
            ],
        )

        sessions = notify_mod.list_recent_lead_sessions("proj", provider="gemini")
        assert [item["uuid"] for item in sessions] == [lead_uuid]

    def test_read_recent_lead_messages_skips_command_wrapper_markup(self, tmp_path, config_dir):
        path = _write_jsonl(
            tmp_path,
            "C--proj",
            "uuid-1",
            [
                _command_wrapper_line(
                    "<command-name>/compact</command-name>\n"
                    "<command-message>compact</command-message>\n"
                    "<command-args></command-args>"
                ),
                _command_wrapper_line(
                    "<local-command-stdout>Compacted (ctrl+o to see full summary)"
                    "</local-command-stdout>"
                ),
                _user_string_line("real question"),
            ],
        )
        assert notify_mod.read_recent_lead_messages(path) == [
            {"text": "real question", "kind": "me"}
        ]


class TestLeadUserText:
    def test_extracts_text_from_list_content(self):
        rec = json.loads(_user_line("hello from user"))
        assert notify_mod._lead_user_text(rec) == "hello from user"

    def test_extracts_text_from_string_content(self):
        rec = json.loads(_user_string_line("plain string turn"))
        assert notify_mod._lead_user_text(rec) == "plain string turn"

    def test_skips_tool_result_blocks(self):
        rec = json.loads(_tool_result_line())
        assert notify_mod._lead_user_text(rec) is None

    def test_skips_non_user_records(self):
        rec = json.loads(_assistant_line("hi"))
        assert notify_mod._lead_user_text(rec) is None

    def test_empty_content_is_none(self):
        rec = {"type": "user", "message": {"role": "user", "content": "   "}}
        assert notify_mod._lead_user_text(rec) is None

    def test_skips_is_meta_records(self):
        rec = json.loads(_meta_user_line("Continue from where you left off."))
        assert notify_mod._lead_user_text(rec) is None

    def test_skips_is_meta_image_placeholder(self):
        rec = json.loads(
            _meta_user_line(r"[Image: source: C:\Users\alice\.claude-work\image-cache\abc\1.png]")
        )
        assert notify_mod._lead_user_text(rec) is None

    def test_skips_command_name_wrapper(self):
        rec = json.loads(
            _command_wrapper_line(
                "<command-name>/compact</command-name>\n<command-args></command-args>"
            )
        )
        assert notify_mod._lead_user_text(rec) is None

    def test_skips_local_command_stdout_wrapper(self):
        rec = json.loads(
            _command_wrapper_line(
                "<local-command-stdout>Compacted (ctrl+o to see full summary)</local-command-stdout>"
            )
        )
        assert notify_mod._lead_user_text(rec) is None

    def test_live_user_payload_marks_remote_origin_and_strips_prefix(self):
        assert notify_mod._claude_live_users(
            json.loads(_user_line("[remote → lead] hello from phone"))
        ) == [{"text": "hello from phone", "remote": True}]

    def test_live_user_payload_preserves_desktop_origin(self):
        assert notify_mod._claude_live_users(json.loads(_user_line("hello from desktop"))) == [
            {"text": "hello from desktop", "remote": False}
        ]


class TestAskQuestionPrompt:
    """`_ask_question_prompt` (W2a SHOULD-FIX): detects a real `AskUserQuestion`
    tool_use block and returns only the short question text — never the
    options list (data-min)."""

    def test_extracts_first_question_text(self):
        rec = json.loads(_ask_user_question_line("เลือกแนวทางไหนดี?"))
        assert notify_mod._ask_question_prompt(rec) == "เลือกแนวทางไหนดี?"

    def test_never_leaks_options_payload(self):
        rec = json.loads(_ask_user_question_line("q"))
        prompt = notify_mod._ask_question_prompt(rec)
        assert "description" not in prompt
        assert "label" not in prompt

    def test_truncates_long_question(self):
        long_q = "x" * 500
        rec = json.loads(_ask_user_question_line(long_q))
        prompt = notify_mod._ask_question_prompt(rec)
        assert len(prompt) == notify_mod._MAX_ASK_QUESTION_CHARS

    def test_non_ask_tool_use_is_none(self):
        rec = json.loads(_tool_use_line())
        assert notify_mod._ask_question_prompt(rec) is None

    def test_text_only_record_is_none(self):
        rec = json.loads(_assistant_line("hi"))
        assert notify_mod._ask_question_prompt(rec) is None

    def test_non_assistant_record_is_none(self):
        rec = json.loads(_user_line("hi"))
        assert notify_mod._ask_question_prompt(rec) is None


class TestAskQuestionOptions:
    """`_ask_question_options` (B2, remote AskUserQuestion fix): forwards the full picker payload
    for EVERY question in the tool call (prompt + option labels +
    multiSelect), not just the first, so the remote can render tappable
    chips and answer a multi-question call."""

    def test_extracts_prompt_options_and_multi_select(self):
        rec = json.loads(_ask_user_question_line("เลือกแนวทางไหนดี?"))
        assert notify_mod._ask_question_options(rec) == {
            "questions": [
                {
                    "prompt": "เลือกแนวทางไหนดี?",
                    "options": [{"index": 0, "label": "A"}, {"index": 1, "label": "B"}],
                    "multiSelect": False,
                }
            ]
        }

    def test_caps_option_count(self):
        rec = json.loads(_ask_user_question_line("q"))
        opts = [{"label": f"opt{i}"} for i in range(10)]
        rec["message"]["content"][0]["input"]["questions"][0]["options"] = opts
        result = notify_mod._ask_question_options(rec)
        assert len(result["questions"][0]["options"]) == notify_mod._MAX_ASK_OPTIONS

    def test_non_ask_tool_use_is_none(self):
        rec = json.loads(_tool_use_line())
        assert notify_mod._ask_question_options(rec) is None

    def test_non_assistant_record_is_none(self):
        rec = json.loads(_user_line("hi"))
        assert notify_mod._ask_question_options(rec) is None

    def test_forwards_every_question_not_just_the_first(self):
        # remote AskUserQuestion fix: the pre-fix behavior read only questions[0] -- a Lead turn
        # firing 2+ questions in one call silently lost every question after
        # the first, so the phone couldn't even show it, let alone answer.
        rec = json.loads(_ask_user_question_line("Q1?"))
        rec["message"]["content"][0]["input"]["questions"].append(
            {
                "question": "Q2?",
                "header": "second",
                "options": [{"label": "C"}, {"label": "D"}],
                "multiSelect": True,
            }
        )
        result = notify_mod._ask_question_options(rec)
        assert [q["prompt"] for q in result["questions"]] == ["Q1?", "Q2?"]
        assert result["questions"][1]["multiSelect"] is True
        assert result["questions"][1]["options"] == [
            {"index": 0, "label": "C"},
            {"index": 1, "label": "D"},
        ]

    def test_caps_question_count(self):
        rec = json.loads(_ask_user_question_line("Q0?"))
        base = rec["message"]["content"][0]["input"]["questions"][0]
        rec["message"]["content"][0]["input"]["questions"] = [
            {**base, "question": f"Q{i}?"} for i in range(10)
        ]
        result = notify_mod._ask_question_options(rec)
        assert len(result["questions"]) == notify_mod._MAX_ASK_QUESTIONS


class TestCurrentAskState:
    """`current_ask_state` (remote AskUserQuestion fix): the answer-picker endpoint's guard — a
    FRESH, uncached re-read of the pane's actual current state, independent
    of whatever `LeadNotifier`'s poll tail last pushed over SSE. Must return
    None the moment anything supersedes the picker (a real reply, or a
    different tool_use), so a stale mobile banner can never type digits
    into a live chat turn."""

    def test_active_picker_returns_all_questions(self, qapp, tmp_path, config_dir):
        orch = _FakeOrch()
        orch.set_lead("proj", "uuid-1")
        _write_jsonl(tmp_path, "C--proj", "uuid-1", [_ask_user_question_line("ไปทางไหนดี?")])
        state = notify_mod.current_ask_state(orch, "proj")
        assert state == {
            "questions": [
                {
                    "prompt": "ไปทางไหนดี?",
                    "options": [{"index": 0, "label": "A"}, {"index": 1, "label": "B"}],
                    "multiSelect": False,
                }
            ]
        }

    def test_a_real_reply_after_the_picker_supersedes_it(self, qapp, tmp_path, config_dir):
        orch = _FakeOrch()
        orch.set_lead("proj", "uuid-1")
        _write_jsonl(
            tmp_path,
            "C--proj",
            "uuid-1",
            [_ask_user_question_line("q"), _assistant_line("answered on desktop already")],
        )
        assert notify_mod.current_ask_state(orch, "proj") is None

    def test_a_different_tool_use_after_the_picker_supersedes_it(self, qapp, tmp_path, config_dir):
        # Lead moved on to something else -- the picker (if it ever showed)
        # is no longer the pane's current state.
        orch = _FakeOrch()
        orch.set_lead("proj", "uuid-1")
        _write_jsonl(
            tmp_path, "C--proj", "uuid-1", [_ask_user_question_line("q"), _tool_use_line()]
        )
        assert notify_mod.current_ask_state(orch, "proj") is None

    def test_no_jsonl_yet_is_none(self, qapp, tmp_path, config_dir):
        orch = _FakeOrch()
        orch.set_lead("proj", "uuid-1")
        assert notify_mod.current_ask_state(orch, "proj") is None

    def test_non_claude_provider_is_none(self, qapp, tmp_path, config_dir):
        # Only Claude has a live_ask scanner -- same #103 gap the fallback
        # banner already documents.
        orch = _FakeOrch()
        orch.set_lead("proj", "uuid-1", provider="gemini")
        assert notify_mod.current_ask_state(orch, "proj") is None


class TestLeadOutputTailAskQuestion:
    def test_ask_user_question_pushes_blocked_on_picker(self, qapp, tmp_path, config_dir):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        _write_jsonl(tmp_path, "C--proj", "uuid-1", [])
        path = config_dir / "projects" / "C--proj" / "uuid-1.jsonl"

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-1")
            orch.statusChanged.emit()

            with path.open("a", encoding="utf-8") as fh:
                fh.write(_ask_user_question_line("ไปทางไหนดี?") + "\n")
            notifier._poll_all()
            expected = {
                "questions": [
                    {
                        "prompt": "ไปทางไหนดี?",
                        "options": [{"index": 0, "label": "A"}, {"index": 1, "label": "B"}],
                        "multiSelect": False,
                    }
                ]
            }
            assert broadcaster.events == [("blocked_on_picker", expected, "proj")]
        finally:
            notifier.stop()

    def test_answer_text_in_same_batch_supersedes_the_picker_signal(
        self, qapp, tmp_path, config_dir
    ):
        # A picker followed immediately (same poll batch) by real reply text
        # means it was already resolved — no stuck-banner signal needed.
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        _write_jsonl(tmp_path, "C--proj", "uuid-1", [])
        path = config_dir / "projects" / "C--proj" / "uuid-1.jsonl"

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-1")
            orch.statusChanged.emit()

            with path.open("a", encoding="utf-8") as fh:
                fh.write(_ask_user_question_line("q") + "\n")
                fh.write(_assistant_line("answered already") + "\n")
            notifier._poll_all()
            assert broadcaster.events == [("lead", "answered already", "proj")]
        finally:
            notifier.stop()

    def test_later_text_in_a_subsequent_poll_does_not_retroactively_clear(
        self, qapp, tmp_path, config_dir
    ):
        # The picker event already reached the client in its own poll tick;
        # a later poll's real text is just a normal 'lead' push — the PWA
        # itself clears the banner on receiving it.
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        _write_jsonl(tmp_path, "C--proj", "uuid-1", [])
        path = config_dir / "projects" / "C--proj" / "uuid-1.jsonl"

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-1")
            orch.statusChanged.emit()

            with path.open("a", encoding="utf-8") as fh:
                fh.write(_ask_user_question_line("q") + "\n")
            notifier._poll_all()
            expected = {
                "questions": [
                    {
                        "prompt": "q",
                        "options": [{"index": 0, "label": "A"}, {"index": 1, "label": "B"}],
                        "multiSelect": False,
                    }
                ]
            }
            assert broadcaster.events == [("blocked_on_picker", expected, "proj")]

            broadcaster.events.clear()
            with path.open("a", encoding="utf-8") as fh:
                fh.write(_assistant_line("finally answered") + "\n")
            notifier._poll_all()
            assert broadcaster.events == [("lead", "finally answered", "proj")]
        finally:
            notifier.stop()


class TestStripRemotePrefix:
    def test_strips_leading_prefix(self):
        assert notify_mod._strip_remote_prefix("[remote → lead] hi") == "hi"

    def test_leaves_text_without_prefix_untouched(self):
        assert notify_mod._strip_remote_prefix("hi") == "hi"


class TestLeadOutputTail:
    def test_emits_new_desktop_user_turn_to_connected_remotes(self, qapp, tmp_path, config_dir):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        _write_jsonl(tmp_path, "C--proj", "uuid-1", [])
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-1")
            orch.statusChanged.emit()
            path = config_dir / "projects" / "C--proj" / "uuid-1.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(_user_line("typed on desktop") + "\n")

            notifier._poll_all()

            assert broadcaster.events == [
                ("user", {"text": "typed on desktop", "remote": False}, "proj")
            ]
        finally:
            notifier.stop()

    def test_jsonl_activity_followed_by_idle_always_clears_working(
        self, qapp, tmp_path, config_dir
    ):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        _write_jsonl(tmp_path, "C--proj", "uuid-1", [])
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-1")
            orch.statusChanged.emit()
            path = config_dir / "projects" / "C--proj" / "uuid-1.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(_tool_use_line() + "\n")

            # The pane is already idle by this poll. The activity record may
            # briefly raise working, but the same poll must also emit idle.
            notifier._poll_all()

            assert broadcaster.events == [
                ("working", "reading", "proj"),
                ("idle", "", "proj"),
            ]
            assert notifier._lead_working["proj"] is False
        finally:
            notifier.stop()

    def test_resyncs_to_lead_session_and_emits_assistant_text_only(
        self, qapp, tmp_path, config_dir
    ):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        _write_jsonl(tmp_path, "C--proj", "uuid-1", [])

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-1")
            orch.statusChanged.emit()  # discovers the newly-registered lead session

            path = config_dir / "projects" / "C--proj" / "uuid-1.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(_tool_use_line() + "\n")
                fh.write(_assistant_line("hello lead") + "\n")

            notifier._poll_all()
            assert broadcaster.events == [("lead", "hello lead", "proj")]
        finally:
            notifier.stop()

    def test_does_not_replay_backlog_that_predates_discovery(self, qapp, tmp_path, config_dir):
        # Offset starts at current EOF at discovery time — mirrors the old
        # bytesIn hook, which never handed a fresh subscriber history that
        # predated the connection.
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        _write_jsonl(tmp_path, "C--proj", "uuid-1", [_assistant_line("old backlog text")])

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-1")
            orch.statusChanged.emit()

            notifier._poll_all()
            assert broadcaster.events == []

            path = config_dir / "projects" / "C--proj" / "uuid-1.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(_assistant_line("new text") + "\n")
            notifier._poll_all()
            assert broadcaster.events == [("lead", "new text", "proj")]
        finally:
            notifier.stop()

    def test_partial_last_line_is_held_back_until_completed(self, qapp, tmp_path, config_dir):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        _write_jsonl(tmp_path, "C--proj", "uuid-1", [])
        path = config_dir / "projects" / "C--proj" / "uuid-1.jsonl"

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-1")
            orch.statusChanged.emit()

            full_line = _assistant_line("split across polls")
            half = len(full_line) // 2
            with path.open("a", encoding="utf-8") as fh:
                fh.write(full_line[:half])  # no trailing newline yet
            notifier._poll_all()
            assert broadcaster.events == []

            with path.open("a", encoding="utf-8") as fh:
                fh.write(full_line[half:] + "\n")
            notifier._poll_all()
            assert broadcaster.events == [("lead", "split across polls", "proj")]
        finally:
            notifier.stop()

    def test_switching_lead_session_uuid_resets_the_tail(self, qapp, tmp_path, config_dir):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        _write_jsonl(tmp_path, "C--proj", "uuid-old", [])
        _write_jsonl(tmp_path, "C--proj", "uuid-new", [])

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-old")
            orch.statusChanged.emit()

            old_path = config_dir / "projects" / "C--proj" / "uuid-old.jsonl"
            with old_path.open("a", encoding="utf-8") as fh:
                fh.write(_assistant_line("stale output") + "\n")

            # respawn — new session-id
            orch.set_lead("proj", "uuid-new")
            orch.statusChanged.emit()
            notifier._poll_all()
            assert broadcaster.events == [("session_changed", {"provider": "claude"}, "proj")], (
                "stale session's output must never surface"
            )

            new_path = config_dir / "projects" / "C--proj" / "uuid-new.jsonl"
            with new_path.open("a", encoding="utf-8") as fh:
                fh.write(_assistant_line("fresh output") + "\n")
            notifier._poll_all()
            assert broadcaster.events == [
                ("session_changed", {"provider": "claude"}, "proj"),
                ("lead", "fresh output", "proj"),
            ]
        finally:
            notifier.stop()

    def test_tails_every_open_projects_lead_session_simultaneously(
        self, qapp, tmp_path, config_dir
    ):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        _write_jsonl(tmp_path, "C--proj-a", "uuid-a", [])
        _write_jsonl(tmp_path, "C--proj-b", "uuid-b", [])

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj-a", "uuid-a")
            orch.set_lead("proj-b", "uuid-b")
            orch.statusChanged.emit()

            with (config_dir / "projects" / "C--proj-a" / "uuid-a.jsonl").open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write(_assistant_line("from proj-a") + "\n")
            with (config_dir / "projects" / "C--proj-b" / "uuid-b.jsonl").open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write(_assistant_line("from proj-b") + "\n")

            notifier._poll_all()
            assert ("lead", "from proj-a", "proj-a") in broadcaster.events
            assert ("lead", "from proj-b", "proj-b") in broadcaster.events
            for _event, text, ns in broadcaster.events:
                if ns == "proj-b":
                    assert "proj-a" not in text
                if ns == "proj-a":
                    assert "proj-b" not in text
        finally:
            notifier.stop()

    def test_resync_does_not_reset_offset_for_an_unchanged_session(
        self, qapp, tmp_path, config_dir
    ):
        # statusChanged can fire many times (e.g. another project's pane
        # spawning) without this project's Lead session ever changing —
        # a resync must never re-read from EOF and lose already-tailed ground.
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        _write_jsonl(tmp_path, "C--proj", "uuid-1", [])
        path = config_dir / "projects" / "C--proj" / "uuid-1.jsonl"

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-1")
            orch.statusChanged.emit()
            orch.statusChanged.emit()  # same session both times

            with path.open("a", encoding="utf-8") as fh:
                fh.write(_assistant_line("hello") + "\n")
            notifier._poll_all()
            orch.statusChanged.emit()  # must not rewind the offset
            notifier._poll_all()
            assert broadcaster.events == [("lead", "hello", "proj")]
        finally:
            notifier.stop()

    def test_resync_drops_tail_for_a_project_that_closed(self, qapp, tmp_path, config_dir):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        _write_jsonl(tmp_path, "C--proj-a", "uuid-a", [])
        path = config_dir / "projects" / "C--proj-a" / "uuid-a.jsonl"

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj-a", "uuid-a")
            orch.statusChanged.emit()

            # project tab closed — no longer present in the pane registry
            orch.drop_project("proj-a")
            orch.statusChanged.emit()

            with path.open("a", encoding="utf-8") as fh:
                fh.write(_assistant_line("stale after close") + "\n")
            notifier._poll_all()
            assert broadcaster.events == []
        finally:
            notifier.stop()

    def test_no_lead_pane_is_a_safe_no_op(self, qapp):
        orch = _FakeOrch()  # no "lead" key registered
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.statusChanged.emit()
            notifier._poll_all()
        finally:
            notifier.stop()
        assert broadcaster.events == []

    def test_missing_jsonl_file_is_a_safe_no_op(self, qapp, config_dir):
        # session_uuid is known but the file hasn't been created yet (e.g. a
        # spawn that just wrote --session-id but claude hasn't flushed yet).
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-does-not-exist")
            orch.statusChanged.emit()
            notifier._poll_all()
        finally:
            notifier.stop()
        assert broadcaster.events == []

    def test_retries_resolving_jsonl_that_is_created_after_first_resync(
        self, qapp, tmp_path, config_dir
    ):
        # codex HIGH: the Lead's session uuid can be known before Claude has
        # created/flushed its jsonl file. `_resync()` used to run only on
        # `statusChanged` — if the glob missed on that first pass, the
        # session was never retried unless some unrelated `statusChanged`
        # happened to fire later, so every reply for it was lost for good.
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-late")
            orch.statusChanged.emit()  # jsonl doesn't exist yet
            assert "proj" not in notifier._tails

            notifier._poll_all()  # no statusChanged fired again — must still retry
            assert "proj" not in notifier._tails

            path = _write_jsonl(tmp_path, "C--proj", "uuid-late", [])
            notifier._poll_all()  # picks the file up on its own, no signal needed
            assert "proj" in notifier._tails

            with path.open("a", encoding="utf-8") as fh:
                fh.write(_assistant_line("finally here") + "\n")
            notifier._poll_all()
            assert broadcaster.events == [("lead", "finally here", "proj")]
        finally:
            notifier.stop()

    def test_eof_mid_record_at_discovery_does_not_lose_that_record(
        self, qapp, tmp_path, config_dir
    ):
        # codex LOW: if EOF at discovery time lands mid-JSON-object (Claude
        # is still writing it, no trailing \n yet), starting the tail's
        # offset there would split the completed line in two once the
        # newline lands, permanently dropping it.
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        full_line = _assistant_line("half-written at discovery")
        half = len(full_line) // 2
        path = _write_jsonl(tmp_path, "C--proj", "uuid-1", [])
        with path.open("a", encoding="utf-8") as fh:
            fh.write(full_line[:half])  # no trailing newline — EOF is mid-record

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-1")
            orch.statusChanged.emit()  # discovers with EOF mid-line

            with path.open("a", encoding="utf-8") as fh:
                fh.write(full_line[half:] + "\n")
            notifier._poll_all()
            assert broadcaster.events == [("lead", "half-written at discovery", "proj")]
        finally:
            notifier.stop()

    def test_stop_disconnects_everything(self, qapp, tmp_path, config_dir):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        _write_jsonl(tmp_path, "C--proj", "uuid-1", [])
        path = config_dir / "projects" / "C--proj" / "uuid-1.jsonl"

        notifier = LeadNotifier(orch, broadcaster)
        orch.set_lead("proj", "uuid-1")
        orch.statusChanged.emit()
        notifier.stop()

        with path.open("a", encoding="utf-8") as fh:
            fh.write(_assistant_line("after stop") + "\n")
        notifier._poll_all()  # stopped notifier must not still be polling
        orch.agentDone.emit("proj", "backend", "note")
        assert broadcaster.events == []


class TestUuidlessProviderResyncThrottle:
    """#234 regression: #229's `project_ns in self._tails` fast path proves
    a resolved path is stable only for a session-uuid-anchored provider
    (claude) — the eviction loop above it deletes the tail the instant that
    identity drifts. A provider with `requires_session_uuid=False` (gemini,
    codex) has a permanently-empty session_uuid in its identity triple, so
    that proof never applies to it; its resolver can still legitimately
    re-point to a different file (e.g. gemini's uuid-less lookup is an
    uncached newest-mtime glob) without the identity ever changing. These
    must keep being re-resolved — just throttled, not skipped forever."""

    def test_gemini_tail_repoints_to_a_rotated_file_after_the_throttle_elapses(
        self, qapp, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "agent_takkub.config.lead_cwd", lambda project=None: str(tmp_path / "cwd")
        )
        first = tmp_path / "session-first.jsonl"
        first.write_text("", encoding="utf-8")
        second = tmp_path / "session-second.jsonl"
        second.write_text("", encoding="utf-8")

        current = [first]
        calls = {"n": 0}

        def _resolve(cwd, session_uuid):
            calls["n"] += 1
            return current[0]

        monkeypatch.setattr(gemini_helper, "resolve_gemini_jsonl_for_cwd", _resolve)

        clock = _FakeMonotonicClock()
        monkeypatch.setattr(notify_mod, "time", clock)

        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", None, provider="gemini")
            orch.statusChanged.emit()
            assert notifier._tails["proj"].path == first
            assert calls["n"] == 1

            # agy rotates to a fresh conversation file — same
            # (provider, "", spawn_ts) identity, different resolved path.
            current[0] = second

            # Still inside the throttle window: must not re-resolve or
            # re-point yet — this bounds the #229 stat-storm risk.
            notifier._poll_all()
            assert notifier._tails["proj"].path == first
            assert calls["n"] == 1

            # Throttle elapsed: must re-resolve and pick up the rotated file.
            clock.value += notify_mod._UUIDLESS_RESYNC_THROTTLE_S
            notifier._poll_all()
            assert notifier._tails["proj"].path == second
            assert calls["n"] == 2
            assert ("session_changed", {"provider": "gemini"}, "proj") in broadcaster.events
        finally:
            notifier.stop()

    def test_claude_tail_is_never_re_resolved_regardless_of_elapsed_time(
        self, qapp, tmp_path, config_dir, monkeypatch
    ):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        _write_jsonl(tmp_path, "C--proj", "uuid-1", [])

        calls = {"n": 0}
        real_resolve = notify_mod._resolve_claude_jsonl_path

        def _spy(project, uuid):
            calls["n"] += 1
            return real_resolve(project, uuid)

        monkeypatch.setattr(notify_mod, "_resolve_claude_jsonl_path", _spy)

        clock = _FakeMonotonicClock()
        monkeypatch.setattr(notify_mod, "time", clock)

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.set_lead("proj", "uuid-1", provider="claude")
            orch.statusChanged.emit()
            assert calls["n"] == 1

            # A claude identity that hasn't drifted must stay pinned no
            # matter how much wall-clock time passes — unlike the uuid-less
            # throttle above, this path has no time-based re-check at all.
            clock.value += notify_mod._UUIDLESS_RESYNC_THROTTLE_S * 10
            notifier._poll_all()
            notifier._poll_all()
            assert calls["n"] == 1
        finally:
            notifier.stop()


class TestCodexRemoteHistory:
    @pytest.fixture(autouse=True)
    def _isolate_codex(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.cwd = tmp_path / "project"
        self.cwd.mkdir()
        self.root = tmp_path / "codex-sessions"
        self.root.mkdir()
        self.archived_root = tmp_path / "codex-archived-sessions"
        monkeypatch.setattr(notify_mod, "_codex_sessions_root", lambda: self.root)
        monkeypatch.setattr(notify_mod, "_codex_archived_sessions_root", lambda: self.archived_root)
        monkeypatch.setattr("agent_takkub.config.lead_cwd", lambda project=None: str(self.cwd))
        notify_mod._CODEX_RESOLVE_CACHE.clear()
        yield
        notify_mod._CODEX_RESOLVE_CACHE.clear()

    def _write_archived(self, uuid: str) -> Path:
        self.archived_root.mkdir(parents=True, exist_ok=True)
        path = self.archived_root / f"rollout-2026-08-11T10-00-00-{uuid}.jsonl"
        meta = {
            "type": "session_meta",
            "payload": {"id": uuid, "session_id": uuid, "cwd": str(self.cwd)},
        }
        path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
        return path

    def _write(self, uuid: str, records: list[dict]) -> Path:
        path = self.root / f"rollout-2026-08-11T10-00-00-{uuid}.jsonl"
        meta = {
            "type": "session_meta",
            "payload": {"id": uuid, "session_id": uuid, "cwd": str(self.cwd)},
        }
        path.write_text(
            "\n".join(json.dumps(item) for item in [meta, *records]) + "\n",
            encoding="utf-8",
        )
        return path

    def test_reads_only_clean_user_and_agent_events(self):
        path = self._write(
            "codex-uuid",
            [
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "[remote → lead] hello"},
                },
                {
                    "type": "response_item",
                    "payload": {"type": "custom_tool_call", "input": "secret tool args"},
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "working update"},
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "final answer"},
                },
            ],
        )

        assert notify_mod.read_recent_lead_messages(path, provider="codex") == [
            {"text": "hello", "kind": "me"},
            {"text": "working update", "kind": "lead"},
            {"text": "final answer", "kind": "lead"},
        ]

    def _item_event(self, item: dict) -> dict:
        return {"type": "event_msg", "payload": {"type": "item_completed", "item": item}}

    def test_reads_item_completed_messages_from_codex_0_147(self):
        """Codex 0.147's TUI dropped the flat agent_message/user_message pair
        for `item_completed` items. `codex exec` still writes the old form, so
        every exec-based probe kept passing while Mobile went blank for
        `Lead = codex` — both schemas must parse."""
        path = self._write(
            "codex-0147",
            [
                self._item_event(
                    {
                        "type": "UserMessage",
                        "content": [
                            {"type": "local_image", "path": "/tmp/pasted.png"},
                            {"type": "text", "text": "[remote → lead] hello"},
                        ],
                    }
                ),
                self._item_event({"type": "Reasoning", "summary_text": ["hidden thinking"]}),
                self._item_event(
                    {"type": "CommandExecution", "command": ["pwsh", "-Command", "secret args"]}
                ),
                self._item_event(
                    {
                        "type": "AgentMessage",
                        "phase": "commentary",
                        "content": [{"type": "Text", "text": "working update"}],
                    }
                ),
                self._item_event(
                    {
                        "type": "AgentMessage",
                        "phase": "final_answer",
                        "content": [{"type": "Text", "text": "final answer"}],
                    }
                ),
            ],
        )

        assert notify_mod.read_recent_lead_messages(path, provider="codex") == [
            {"text": "hello", "kind": "me"},
            {"text": "working update", "kind": "lead"},
            {"text": "final answer", "kind": "lead"},
        ]

    def test_reads_item_completed_messages_from_codex_0_148_paginated(self):
        """Regression guard for #319: codex 0.148.0's `migrate-rollouts`/
        background pagination adds `ordinal` to every record and a duplicate
        `session_id` key to `session_meta` (verified by migrating a real
        on-disk rollout with `codex migrate-rollouts --apply` and diffing the
        result) — the `item_completed` shape itself is unchanged, so the
        extra fields must be harmlessly ignored, not break the parse."""
        path = self._write(
            "codex-0148",
            [
                {"ordinal": 1, "type": "turn_context", "payload": {"turn_id": "t1"}},
                {
                    "ordinal": 2,
                    **self._item_event(
                        {
                            "type": "UserMessage",
                            "id": "item-1",
                            "content": [{"type": "text", "text": "[remote → lead] hello"}],
                        }
                    ),
                },
                {
                    "ordinal": 3,
                    **self._item_event(
                        {
                            "type": "AgentMessage",
                            "id": "item-2",
                            "content": [{"type": "Text", "text": "final answer"}],
                        }
                    ),
                },
            ],
        )

        assert notify_mod.read_recent_lead_messages(path, provider="codex") == [
            {"text": "hello", "kind": "me"},
            {"text": "final answer", "kind": "lead"},
        ]

    def test_live_codex_0_147_reply_is_pushed(self, qapp):
        path = self._write("codex-live-0147", [])
        os.utime(path, (time.time(), time.time()))
        orch = _FakeOrch()
        orch.set_lead("proj", None, provider="codex")
        orch._panes_by_project["proj"]["lead"].model.spawn_ts = time.time() - 1
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            assert notifier._tails["proj"].path == path
            with path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        self._item_event(
                            {
                                "type": "AgentMessage",
                                "phase": "final_answer",
                                "content": [{"type": "Text", "text": "codex 0.147 reply"}],
                            }
                        )
                    )
                    + "\n"
                )
            notifier._poll_all()
            assert broadcaster.events == [("lead", "codex 0.147 reply", "proj")]
        finally:
            notifier.stop()

    def test_uuidless_live_codex_session_is_resolved_by_cwd_and_spawn_time(self, qapp):
        path = self._write("codex-live", [])
        os.utime(path, (time.time(), time.time()))
        orch = _FakeOrch()
        orch.set_lead("proj", None, provider="codex")
        orch._panes_by_project["proj"]["lead"].model.spawn_ts = time.time() - 1
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            assert notifier._tails["proj"].path == path
            with path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {"type": "agent_message", "message": "codex reply"},
                        }
                    )
                    + "\n"
                )
            notifier._poll_all()
            assert broadcaster.events == [("lead", "codex reply", "proj")]
        finally:
            notifier.stop()

    def test_resumed_codex_uuid_resolves_old_rollout_before_first_new_write(self):
        path = self._write("codex-resumed", [])
        old = time.time() - 86_400
        os.utime(path, (old, old))

        assert (
            notify_mod._resolve_codex_jsonl_path("proj", "codex-resumed", not_before=time.time())
            == path
        )

    def test_wrong_cwd_is_never_selected(self):
        path = self._write("other-cwd", [])
        first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        first["payload"]["cwd"] = str(self.cwd.parent / "other")
        path.write_text(json.dumps(first) + "\n", encoding="utf-8")

        assert notify_mod._resolve_codex_jsonl_path("proj", None) is None

    def test_archived_session_still_resolves_by_id(self):
        """`codex archive` (0.148+) moves the rollout OUT of `sessions/` into
        a flat `archived_sessions/` dir (verified against a real store, #319)
        — an id-based lookup must still find it there rather than going
        silently blank for a session archived mid-conversation."""
        path = self._write_archived("codex-archived")

        assert notify_mod._resolve_codex_jsonl_path("proj", "codex-archived") == path

    def test_live_session_wins_over_a_stale_archived_duplicate(self):
        live = self._write("codex-dup", [])
        self._write_archived("codex-dup")

        assert notify_mod._resolve_codex_jsonl_path("proj", "codex-dup") == live

    def test_archived_lookup_never_used_for_the_no_id_spawn_time_scan(self):
        """Only the exact id+cwd lookup falls back to `archived_sessions/` —
        a fresh session with no provider id yet can't already be archived, so
        the newest-for-cwd scan must stay scoped to the live store."""
        self._write_archived("codex-archived-only")

        assert notify_mod._resolve_codex_jsonl_path("proj", None) is None

    def test_mobile_picker_lists_lead_sessions_newest_first_and_filters_teammates(self):
        lead_old = self._write(
            "lead-old",
            [{"type": "event_msg", "payload": {"type": "user_message", "message": "งานเก่า"}}],
        )
        lead_new = self._write(
            "lead-new",
            [
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "[remote → lead] งานใหม่"},
                }
            ],
        )
        teammate = self._write(
            "qa-session",
            [
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "[ROLE: qa] run smoke"},
                }
            ],
        )
        now = time.time()
        os.utime(lead_old, (now - 200, now - 200))
        os.utime(lead_new, (now - 100, now - 100))
        os.utime(teammate, (now, now))

        sessions = notify_mod.list_recent_lead_sessions("proj", provider="codex")
        assert [item["uuid"] for item in sessions] == ["lead-new", "lead-old"]
        assert sessions[0]["preview"] == "งานใหม่"

    def test_mobile_picker_excludes_other_cwd(self):
        path = self._write(
            "other-project",
            [{"type": "event_msg", "payload": {"type": "user_message", "message": "secret"}}],
        )
        records = path.read_text(encoding="utf-8").splitlines()
        meta = json.loads(records[0])
        meta["payload"]["cwd"] = str(self.cwd.parent / "other")
        path.write_text("\n".join([json.dumps(meta), *records[1:]]) + "\n", encoding="utf-8")

        assert notify_mod.list_recent_lead_sessions("proj", provider="codex") == []

    def test_freshly_spawned_pane_meta_only_file_is_not_flagged_as_drift(self):
        """A Codex rollout file exists the moment the pane spawns (the
        `session_meta` preamble `_write` always prepends) — well before the
        user has typed anything. That legitimately parses to zero messages
        and must stay "no reason to explain", never "transcript_unreadable"
        (#348 false-positive guard)."""
        orch = _FakeOrch()
        orch.set_lead("proj", None, provider="codex")
        self._write("codex-fresh", [])
        result = notify_mod.lead_mirror_diagnosis(orch, "proj")
        assert result == {"code": None, "provider": "codex"}

    def test_real_conversation_that_fails_to_parse_at_all_is_unreadable(self):
        """#348: Codex wired into the remote mirror in production — a whole
        rollout file that yields zero parsed messages despite real content
        must surface as `transcript_unreadable`, not a silent blank chat."""
        orch = _FakeOrch()
        orch.set_lead("proj", None, provider="codex")
        drifted = [
            {"type": "turn_completed", "data": {"role": "assistant", "text": "x" * 200}}
            for _ in range(30)
        ]
        self._write("codex-drifted", drifted)
        assert (
            notify_mod.read_recent_lead_messages(
                self.root / "rollout-2026-08-11T10-00-00-codex-drifted.jsonl", provider="codex"
            )
            == []
        )
        result = notify_mod.lead_mirror_diagnosis(orch, "proj")
        assert result == {"code": "transcript_unreadable", "provider": "codex"}


class TestProviderNeutralLiveFallback:
    @pytest.mark.parametrize(
        "provider", ["claude", "codex", "gemini", "opencode", "kimi", "cursor"]
    )
    def test_every_provider_emits_visible_reply_when_structured_event_is_missing(
        self, qapp, provider
    ):
        orch = _FakeOrch()
        orch.set_lead("proj", None, provider=provider)
        pane = orch._panes_by_project["proj"]["lead"]
        pane.session = _FakeScreenSession(["old reply", "current prompt", "ctrl+p commands"])
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            pane.state = "working"
            notifier._emit_lead_working_transitions()
            broadcaster.events.clear()

            pane.session.lines = [
                "old reply",
                "current prompt",
                "provider answer",
                "ctrl+p commands",
            ]
            pane.state = "idle"
            notifier._emit_lead_working_transitions()

            assert broadcaster.events == [
                ("lead", "provider answer", "proj"),
                ("idle", "", "proj"),
            ]
        finally:
            notifier.stop()

    def test_structured_text_suppresses_duplicate_screen_fallback(self, qapp):
        orch = _FakeOrch()
        orch.set_lead("proj", "uuid", provider="claude")
        pane = orch._panes_by_project["proj"]["lead"]
        pane.session = _FakeScreenSession(["prompt"])
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            pane.state = "working"
            notifier._emit_lead_working_transitions()
            broadcaster.events.clear()
            notifier._structured_text_seen.add("proj")
            pane.session.lines = ["prompt", "same structured answer"]
            pane.state = "idle"
            notifier._emit_lead_working_transitions()
            assert broadcaster.events == [("idle", "", "proj")]
        finally:
            notifier.stop()


def test_gemini_live_parser_uses_gemini_records_not_claude_shape():
    rec = {"id": "g1", "type": "gemini", "content": ["gemini live reply"]}
    assert notify_mod._gemini_live_text_blocks(rec) == ["gemini live reply"]

    snapshot = {
        "$set": {
            "messages": [
                {"id": "g-old", "type": "gemini", "content": ["old reply"]},
                {"id": "g-new", "type": "gemini", "content": ["new reply"]},
            ]
        }
    }
    assert notify_mod._gemini_live_text_blocks(snapshot) == ["new reply"]


def test_gemini_and_codex_live_user_parsers_are_provider_native():
    gemini = {"id": "u1", "type": "user", "content": [{"text": "desktop gemini"}]}
    codex = {
        "type": "event_msg",
        "payload": {"type": "user_message", "message": "desktop codex"},
    }
    assert notify_mod._gemini_live_users(gemini) == [{"text": "desktop gemini", "remote": False}]
    assert notify_mod._codex_live_users(codex) == [{"text": "desktop codex", "remote": False}]


class TestOpenCodeHistoryIsProjectScoped:
    """OpenCode keeps every project's session in ONE shared sqlite db, so the
    session id is the only thing separating them. The first version of the
    adapter picked it with `list(_LAST_OPENCODE_SESSION_BY_PROJECT.values())[-1]`
    — the newest *insertion*, not the requested project. Re-assigning an
    existing dict key doesn't move it, so once two projects had resolved,
    every history read for the first one served the second one's transcript.
    """

    def test_read_uses_the_requested_projects_session(self, monkeypatch):
        seen: list[str | None] = []

        def _fake_read(path, sid, limit):
            seen.append(sid)
            return [{"role": "lead", "text": f"from {sid}"}]

        monkeypatch.setattr(
            "agent_takkub.opencode_helper.read_opencode_session_messages", _fake_read
        )
        monkeypatch.setattr(
            notify_mod,
            "_LAST_OPENCODE_SESSION_BY_PROJECT",
            {"project-a": "sid-a", "project-b": "sid-b"},
        )

        out = notify_mod._read_recent_opencode_messages(Path("db.sqlite"), 10, "project-a")

        assert seen == ["sid-a"]
        assert out == [{"role": "lead", "text": "from sid-a"}]

    def test_unknown_project_does_not_borrow_another_ones_session(self, monkeypatch):
        seen: list[str | None] = []

        def _fake_read(path, sid, limit):
            seen.append(sid)
            return []

        monkeypatch.setattr(
            "agent_takkub.opencode_helper.read_opencode_session_messages", _fake_read
        )
        monkeypatch.setattr(notify_mod, "_LAST_OPENCODE_SESSION_BY_PROJECT", {"project-b": "sid-b"})

        notify_mod._read_recent_opencode_messages(Path("db.sqlite"), 10, "project-a")

        assert seen == [None]

    def test_every_scanner_accepts_the_project_argument(self):
        # The project is part of the read_messages contract now — a provider
        # adapter that silently dropped it would reintroduce the same class of
        # bug the moment its store stopped being one-file-per-project.
        for provider in ("claude", "gemini", "codex", "opencode", "cursor"):
            scanner = notify_mod.history_scanner(provider)
            assert scanner is not None
            scanner.read_messages(Path("missing-on-purpose"), 1, "project-a")
