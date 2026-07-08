"""Tests for `agent_takkub.remote.notify.LeadNotifier` (§6.5, X-check 2.1):
hooks `orch.agentDone` -> SSE `done` events, and tails each open project's
Lead pane **structured session JSONL** (not raw PTY bytes) -> SSE `lead`
events (mobile junk-elimination rewrite).
"""

from __future__ import annotations

import json

import pytest
from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal

from agent_takkub.remote import notify as notify_mod
from agent_takkub.remote.notify import LeadNotifier, _lead_text_blocks


class _PaneState:
    def __init__(self, session_uuid: str | None) -> None:
        self.session_uuid = session_uuid


class _FakePane:
    """Placeholder — presence under panes_by_project[project]["lead"] is all
    the notifier checks; the actual session object lives in `_pane_state`."""


class _FakeOrch(QObject):
    agentDone = pyqtSignal(str, str, str)
    statusChanged = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._panes_by_project: dict = {}
        self._pane_state: dict = {}

    def set_lead(self, project: str, session_uuid: str | None) -> None:
        self._panes_by_project.setdefault(project, {})["lead"] = _FakePane()
        self._pane_state[f"{project}::lead"] = _PaneState(session_uuid)

    def drop_project(self, project: str) -> None:
        self._panes_by_project.pop(project, None)
        self._pane_state.pop(f"{project}::lead", None)


class _FakeBroadcaster:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str | None]] = []

    def push(self, event: str, data: str, project_ns: str | None = None) -> None:
        self.events.append((event, data, project_ns))


@pytest.fixture
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


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
    def test_agent_done_pushes_to_broadcaster(self, qapp):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.agentDone.emit("proj", "backend", "added /auth/login")
            assert broadcaster.events == [("done", "backend: added /auth/login", "proj")]
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
            orch.agentDone.emit("other-proj", "backend", "did a thing")
            assert broadcaster.events == [("done", "backend: did a thing", "other-proj")]
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
                    r"[Image: source: C:\Users\monch\.claude-work\image-cache\abc\1.png]"
                ),
                _meta_user_line("Continue from where you left off."),
                _user_line("real question"),
            ],
        )
        assert notify_mod.read_recent_lead_messages(path) == [
            {"text": "real question", "kind": "me"}
        ]

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
            _meta_user_line(r"[Image: source: C:\Users\monch\.claude-work\image-cache\abc\1.png]")
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


class TestStripRemotePrefix:
    def test_strips_leading_prefix(self):
        assert notify_mod._strip_remote_prefix("[remote → lead] hi") == "hi"

    def test_leaves_text_without_prefix_untouched(self):
        assert notify_mod._strip_remote_prefix("hi") == "hi"


class TestLeadOutputTail:
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
            assert broadcaster.events == [], "stale session's output must never surface"

            new_path = config_dir / "projects" / "C--proj" / "uuid-new.jsonl"
            with new_path.open("a", encoding="utf-8") as fh:
                fh.write(_assistant_line("fresh output") + "\n")
            notifier._poll_all()
            assert broadcaster.events == [("lead", "fresh output", "proj")]
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
