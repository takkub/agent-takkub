"""Core V2 Conversation + Checkpoint (docs/v2/V2_IMPLEMENTATION_PLAN.md,
epic #309 Phase 6): `ConversationStore`, `RollingSummary`, `CheckpointManager`,
native-resume-fallback context building, and the fail-open façade hooks
`orchestrator.done()`/`subagent_done()`/`CooldownFailoverSelector` call into.
"""

from __future__ import annotations

import pytest

from agent_takkub.core.conversation.checkpoint import CheckpointManager
from agent_takkub.core.conversation.flag import v2_conversation_enabled
from agent_takkub.core.conversation.resume import build_resume_context, render_resume_prompt
from agent_takkub.core.conversation.store import ConversationStore, conversation_id_for
from agent_takkub.core.conversation.summary import RollingSummary, apply_done_note
from agent_takkub.core.models.conversation import MessageRole

# ── flag ──────────────────────────────────────────────────────────────────


def test_flag_on_by_default(monkeypatch):
    """Default flipped ON in 1.0.84 (epic #309) — env unset now means the
    shipped default, which is enabled."""
    monkeypatch.delenv("TAKKUB_V2_CONVERSATION", raising=False)
    assert v2_conversation_enabled() is True


def test_flag_on_when_set_to_1(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "1")
    assert v2_conversation_enabled() is True


# ── ConversationStore ────────────────────────────────────────────────────


def test_conversation_id_for_is_deterministic():
    assert conversation_id_for("proj-a", "backend") == conversation_id_for("proj-a", "backend")
    assert conversation_id_for("proj-a", "backend") != conversation_id_for("proj-a", "qa")
    assert conversation_id_for("proj-a", "backend") != conversation_id_for("proj-b", "backend")


def test_get_or_create_is_idempotent(tmp_path):
    store = ConversationStore(root=tmp_path)
    conv_id = conversation_id_for("proj-a", "backend")
    first = store.get_or_create(conv_id, project_id="proj-a", task_id="task-1")
    second = store.get_or_create(conv_id, project_id="proj-a", task_id="task-2")
    assert first == second
    assert second.task_id == "task-1"  # first write wins, never silently overwritten


def test_append_and_read_messages_round_trip(tmp_path):
    store = ConversationStore(root=tmp_path)
    conv_id = conversation_id_for("proj-a", "backend")
    store.get_or_create(conv_id, project_id="proj-a")
    store.append_message("proj-a", conv_id, role=MessageRole.USER, text="hello")
    store.append_message("proj-a", conv_id, role=MessageRole.ASSISTANT, text="hi back")

    messages = store.read_messages("proj-a", conv_id)
    assert [(m.role, m.text) for m in messages] == [
        (MessageRole.USER, "hello"),
        (MessageRole.ASSISTANT, "hi back"),
    ]


def test_append_message_true_append_never_rewrites(tmp_path, monkeypatch):
    store = ConversationStore(root=tmp_path)
    conv_id = conversation_id_for("proj-a", "backend")
    store.append_message("proj-a", conv_id, role=MessageRole.USER, text="a")

    replace_calls = []
    monkeypatch.setattr(
        "agent_takkub.core.storage.jsonl_store.os.replace",
        lambda *a: replace_calls.append(a),
    )
    store.append_message("proj-a", conv_id, role=MessageRole.USER, text="b")
    assert replace_calls == []


def test_messages_rotate_past_size_cap(tmp_path, monkeypatch):
    store = ConversationStore(root=tmp_path)
    conv_id = conversation_id_for("proj-a", "backend")
    monkeypatch.setattr("agent_takkub.core.conversation.store._ROTATE_AT_BYTES", 10)
    store.append_message("proj-a", conv_id, role=MessageRole.USER, text="first message")
    store.append_message("proj-a", conv_id, role=MessageRole.USER, text="second message")

    directory = store.conversation_dir("proj-a", conv_id)
    assert (directory / "messages.1.jsonl").exists()
    # rotation must never lose a message — read_messages sees both parts
    messages = store.read_messages("proj-a", conv_id)
    assert [m.text for m in messages] == ["first message", "second message"]


def test_provider_session_binding_round_trip(tmp_path):
    store = ConversationStore(root=tmp_path)
    conv_id = conversation_id_for("proj-a", "backend")
    store.bind_provider_session(
        "proj-a",
        conv_id,
        provider_id="claude",
        session_id="uuid-1",
        cwd="/repo",
        account_id="acc-1",
    )
    bindings = store.provider_bindings("proj-a", conv_id)
    assert len(bindings) == 1
    assert bindings[0].provider_id == "claude"
    assert bindings[0].session_id == "uuid-1"
    assert bindings[0].cwd == "/repo"
    assert bindings[0].account_id == "acc-1"


def test_bind_provider_session_upserts_identical_binding(tmp_path):
    store = ConversationStore(root=tmp_path)
    conv_id = conversation_id_for("proj-a", "backend")
    store.bind_provider_session("proj-a", conv_id, provider_id="claude", session_id="uuid-1")
    store.bind_provider_session("proj-a", conv_id, provider_id="claude", session_id="uuid-1")
    assert len(store.provider_bindings("proj-a", conv_id)) == 1  # no duplicate row

    store.bind_provider_session("proj-a", conv_id, provider_id="claude", session_id="uuid-2")
    bindings = store.provider_bindings("proj-a", conv_id)
    assert len(bindings) == 2  # a real change still appends
    assert bindings[-1].session_id == "uuid-2"


def test_get_conversation_missing_is_none(tmp_path):
    store = ConversationStore(root=tmp_path)
    assert store.get_conversation("proj-a", "nope") is None


def test_append_message_redacts_secret_before_writing_to_disk(tmp_path):
    """PR #311 review gap #2: a note that leaks a credential must never land
    in messages.jsonl as plaintext."""
    store = ConversationStore(root=tmp_path)
    conv_id = conversation_id_for("proj-a", "backend")
    secret = "sk-ant-api03-abcdefghijklmnop1234567890"  # gitleaks:allow (fake test fixture)
    saved = store.append_message(
        "proj-a", conv_id, role=MessageRole.USER, text=f"env dump: token={secret}"
    )

    assert secret not in saved.text
    assert "***REDACTED***" in saved.text

    messages_path = store.conversation_dir("proj-a", conv_id) / "messages.jsonl"
    on_disk = messages_path.read_text(encoding="utf-8")
    assert secret not in on_disk

    reloaded = store.read_messages("proj-a", conv_id)
    assert secret not in reloaded[0].text


# ── RollingSummary ────────────────────────────────────────────────────────


def test_apply_done_note_clean_promotes_to_completed():
    summary = RollingSummary(in_progress=["[backend] add /login"], pending=["[backend] add /login"])
    updated = apply_done_note(summary, role="backend", note="add /login\nmore detail", failed=False)
    assert updated.completed == ["[backend] add /login"]
    assert updated.in_progress == []
    assert updated.pending == []
    assert updated.current_state == "[backend] add /login"


def test_apply_done_note_failed_records_warning_and_pending():
    summary = RollingSummary()
    updated = apply_done_note(summary, role="backend", note="tests failing", failed=True)
    assert updated.pending == ["[backend] tests failing"]
    assert updated.warnings == ["[backend] tests failing"]
    assert updated.next_action == "fix: [backend] tests failing"


def test_apply_done_note_blank_note_is_noop():
    summary = RollingSummary(objective="ship v2")
    updated = apply_done_note(summary, role="backend", note="   \n  ", failed=False)
    assert updated == summary


def test_apply_done_note_extracts_decision_prefixed_line():
    summary = RollingSummary()
    updated = apply_done_note(
        summary,
        role="backend",
        note="DECISION: ใช้ jsonl สำหรับ conversation store เพราะ append-only",
        failed=False,
    )
    assert updated.decisions == ["[backend] ใช้ jsonl สำหรับ conversation store เพราะ append-only"]
    # still lands in completed/current_state too — extraction is additive.
    assert updated.completed == [
        "[backend] DECISION: ใช้ jsonl สำหรับ conversation store เพราะ append-only"
    ]


def test_apply_done_note_extracts_decision_and_lesson_from_any_line():
    summary = RollingSummary()
    note = "shipped the migration\ndecided: keep the old table for a release\nLESSON: back up first"
    updated = apply_done_note(summary, role="backend", note=note, failed=False)
    assert updated.decisions == ["[backend] keep the old table for a release"]
    assert updated.lessons == ["[backend] back up first"]


def test_apply_done_note_extracts_decision_and_lesson_from_single_line():
    summary = RollingSummary()
    note = "E2E ok. DECISION: checkpoint ต้องมี binding. LESSON: hook ที่ไม่ถูกเรียกคือ bug เงียบ"
    updated = apply_done_note(summary, role="backend", note=note, failed=False)
    assert updated.decisions == ["[backend] checkpoint ต้องมี binding"]
    assert updated.lessons == ["[backend] hook ที่ไม่ถูกเรียกคือ bug เงียบ"]


def test_apply_done_note_ignores_junk_segment_from_meta_mention():
    note = "extractor จับ prefix กลางบรรทัด (mid-line DECISION:/LESSON: segmentation) เสร็จ"
    updated = apply_done_note(RollingSummary(), role="backend", note=note, failed=False)
    assert updated.decisions == []
    assert updated.lessons == ["[backend] segmentation) เสร็จ"]


def test_apply_done_note_no_prefix_leaves_decisions_and_lessons_empty():
    summary = RollingSummary()
    updated = apply_done_note(summary, role="backend", note="just a plain note", failed=False)
    assert updated.decisions == []
    assert updated.lessons == []


def test_apply_done_note_bounds_list_length():
    summary = RollingSummary()
    for i in range(30):
        summary = apply_done_note(summary, role="backend", note=f"item {i}", failed=False)
    assert len(summary.completed) == 20


# ── CheckpointManager ─────────────────────────────────────────────────────


def test_checkpoint_create_writes_six_files(tmp_path):
    store = ConversationStore(root=tmp_path)
    conv_id = conversation_id_for("proj-a", "backend")
    store.get_or_create(conv_id, project_id="proj-a")
    store.append_message("proj-a", conv_id, role=MessageRole.USER, text="do the thing")

    mgr = CheckpointManager(store)
    checkpoint = mgr.create("proj-a", conv_id, tasks=[{"id": "t1"}], git_state={"sha": "abc"})
    assert checkpoint.id == "checkpoint-00001"

    target = store.conversation_dir("proj-a", conv_id) / "checkpoints" / "checkpoint-00001"
    for name in (
        "summary",
        "working-context",
        "tasks",
        "git-state",
        "provider-binding",
        "runtime-state",
    ):
        assert (target / f"{name}.json").exists()


def test_checkpoint_ids_increment(tmp_path):
    store = ConversationStore(root=tmp_path)
    conv_id = conversation_id_for("proj-a", "backend")
    mgr = CheckpointManager(store)
    first = mgr.create("proj-a", conv_id)
    second = mgr.create("proj-a", conv_id)
    assert first.id == "checkpoint-00001"
    assert second.id == "checkpoint-00002"
    assert mgr.latest_checkpoint("proj-a", conv_id) == "checkpoint-00002"


def test_checkpoint_load_defaults_to_latest(tmp_path):
    store = ConversationStore(root=tmp_path)
    conv_id = conversation_id_for("proj-a", "backend")
    mgr = CheckpointManager(store)
    mgr.create("proj-a", conv_id, git_state={"sha": "first"})
    mgr.create("proj-a", conv_id, git_state={"sha": "second"})
    loaded = mgr.load("proj-a", conv_id)
    assert loaded["git-state"] == {"sha": "second"}
    assert loaded["id"] == "checkpoint-00002"


def test_checkpoint_load_missing_conversation_is_none(tmp_path):
    store = ConversationStore(root=tmp_path)
    mgr = CheckpointManager(store)
    assert mgr.load("proj-a", "no-such-conversation") is None


def test_checkpoint_working_context_captures_recent_messages(tmp_path):
    store = ConversationStore(root=tmp_path)
    conv_id = conversation_id_for("proj-a", "backend")
    store.append_message("proj-a", conv_id, role=MessageRole.USER, text="hello")
    store.append_message("proj-a", conv_id, role=MessageRole.ASSISTANT, text="hi")
    mgr = CheckpointManager(store)
    checkpoint = mgr.create("proj-a", conv_id)
    loaded = mgr.load("proj-a", conv_id, checkpoint.id)
    texts = [m["text"] for m in loaded["working-context"]["messages"]]
    assert texts == ["hello", "hi"]


# ── native resume fallback ────────────────────────────────────────────────


def test_build_resume_context_none_without_checkpoint(tmp_path):
    store = ConversationStore(root=tmp_path)
    assert build_resume_context("proj-a", "no-checkpoint", manager=CheckpointManager(store)) is None


def test_build_resume_context_reads_checkpoint(tmp_path):
    store = ConversationStore(root=tmp_path)
    conv_id = conversation_id_for("proj-a", "backend")
    store.append_message("proj-a", conv_id, role=MessageRole.USER, text="hello")
    summary = apply_done_note(
        RollingSummary(objective="ship v2"), role="backend", note="did X", failed=False
    )
    from agent_takkub.core.conversation.summary import save_summary

    save_summary(store.conversation_dir("proj-a", conv_id), summary)
    mgr = CheckpointManager(store)
    mgr.create("proj-a", conv_id)

    ctx = build_resume_context("proj-a", conv_id, manager=mgr)
    assert ctx is not None
    assert ctx.objective == "ship v2"
    assert ctx.completed == ["[backend] did X"]
    assert ctx.recent_messages[0]["text"] == "hello"


def test_render_resume_prompt_includes_key_sections():
    from agent_takkub.core.conversation.resume import ResumeContext

    ctx = ResumeContext(
        conversation_id="c1",
        checkpoint_id="checkpoint-00001",
        objective="ship v2",
        current_state="[backend] did X",
        next_action="review PR",
        completed=["[backend] did X"],
        recent_messages=[{"role": "user", "text": "hello"}],
    )
    rendered = render_resume_prompt(ctx)
    assert "ship v2" in rendered
    assert "did X" in rendered
    assert "review PR" in rendered
    assert "hello" in rendered


# ── facade: flag off = pure no-op ───────────────────────────────────────


def test_facade_on_pane_done_flag_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "0")
    from agent_takkub.core.conversation.facade import on_pane_done

    conv_root = tmp_path / "conversations"
    store = ConversationStore(root=conv_root)
    on_pane_done("proj-a", "backend", note="did the thing", failed=False, store=store)
    assert not conv_root.exists()


def test_facade_on_pane_done_flag_on_creates_conversation_and_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "1")
    from agent_takkub.core.conversation.facade import on_pane_done

    store = ConversationStore(root=tmp_path)
    on_pane_done("proj-a", "backend", note="did the thing", failed=False, task_id="t1", store=store)

    conv_id = conversation_id_for("proj-a", "backend")
    conv = store.get_conversation("proj-a", conv_id)
    assert conv is not None
    assert conv.task_id == "t1"
    messages = store.read_messages("proj-a", conv_id)
    assert any(m.text == "did the thing" for m in messages)
    mgr = CheckpointManager(store)
    assert mgr.latest_checkpoint("proj-a", conv_id) == "checkpoint-00001"


def test_facade_on_pane_done_binds_provider_session_and_checkpoint_carries_it(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "1")
    monkeypatch.setattr("agent_takkub.core.storage.paths.core_home", lambda: tmp_path / "_core")
    from agent_takkub.core.conversation.facade import on_pane_done

    store = ConversationStore(root=tmp_path)
    on_pane_done(
        "proj-a",
        "backend",
        note="did the thing",
        failed=False,
        provider_id="claude",
        cwd=str(tmp_path),
        session_id="uuid-1",
        store=store,
    )

    conv_id = conversation_id_for("proj-a", "backend")
    bindings = store.provider_bindings("proj-a", conv_id)
    assert len(bindings) == 1
    assert bindings[0].provider_id == "claude"
    assert bindings[0].session_id == "uuid-1"

    mgr = CheckpointManager(store)
    loaded = mgr.load("proj-a", conv_id)
    assert loaded["provider-binding"]["bindings"][0]["session_id"] == "uuid-1"


def test_facade_on_pane_done_no_session_id_skips_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "1")
    from agent_takkub.core.conversation.facade import on_pane_done

    store = ConversationStore(root=tmp_path)
    on_pane_done(
        "proj-a",
        "backend",
        note="subagent done",
        failed=False,
        provider_id="claude",
        cwd=str(tmp_path),
        session_id=None,
        store=store,
    )

    conv_id = conversation_id_for("proj-a", "backend")
    assert store.provider_bindings("proj-a", conv_id) == []


def test_facade_on_pane_done_flag_on_never_raises_on_internal_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "1")
    from agent_takkub.core.conversation import facade

    def boom(*a, **kw):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(facade, "_on_pane_done_impl", boom)
    facade.on_pane_done("proj-a", "backend", note="x", failed=False)  # must not raise


def test_facade_on_account_switch_flag_off_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("TAKKUB_V2_CONVERSATION", raising=False)
    from agent_takkub.core.conversation.facade import on_account_switch

    conv_root = tmp_path / "conversations"
    store = ConversationStore(root=conv_root)
    on_account_switch("pool-1", "claude", "acc-a", "acc-b", reason="cooldown_failover", store=store)
    assert not conv_root.exists()


def test_facade_on_account_switch_without_conversation_id_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "1")
    from agent_takkub.core.conversation.facade import on_account_switch

    conv_root = tmp_path / "conversations"
    store = ConversationStore(root=conv_root)
    on_account_switch("pool-1", "claude", "acc-a", "acc-b", reason="cooldown_failover", store=store)
    assert not conv_root.exists()


def test_facade_on_account_switch_with_conversation_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "1")
    from agent_takkub.core.conversation.facade import on_account_switch

    store = ConversationStore(root=tmp_path)
    conv_id = conversation_id_for("proj-a", "backend")
    store.get_or_create(conv_id, project_id="proj-a")
    on_account_switch(
        "pool-1",
        "claude",
        "acc-a",
        "acc-b",
        reason="cooldown_failover",
        conversation_id=conv_id,
        project_id="proj-a",
        store=store,
    )
    mgr = CheckpointManager(store)
    assert mgr.latest_checkpoint("proj-a", conv_id) == "checkpoint-00001"


# ── accounts.selector wiring ──────────────────────────────────────────────


def test_cooldown_failover_selector_calls_on_account_switch(monkeypatch):
    from agent_takkub.core.accounts.selector import CooldownFailoverSelector
    from agent_takkub.core.models.account import AccountPool, AccountStatus, ProviderAccount

    calls = []
    monkeypatch.setattr(
        "agent_takkub.core.conversation.facade.on_account_switch",
        lambda *a, **kw: calls.append((a, kw)),
    )
    pool = AccountPool(id="pool-1", name="pool-1", provider_id="claude", account_ids=("a", "b"))
    accounts = [
        ProviderAccount(
            id="a", provider_id="claude", label="a", priority=5, status=AccountStatus.ACTIVE
        ),
        ProviderAccount(
            id="b", provider_id="claude", label="b", priority=1, status=AccountStatus.ACTIVE
        ),
    ]
    selector = CooldownFailoverSelector()
    picked_first = selector.select(pool, accounts)  # initial pick — must not log a switch
    assert picked_first.id == "a"
    accounts[0] = ProviderAccount(
        id="a", provider_id="claude", label="a", priority=5, status=AccountStatus.ERROR
    )
    picked_second = selector.select(pool, accounts)  # failover a -> b — must log a switch
    assert picked_second.id == "b"
    assert len(calls) == 1


@pytest.mark.parametrize("flag_value", ["0", "1"])
def test_cooldown_failover_selector_never_raises_regardless_of_flag(
    monkeypatch, flag_value, tmp_path
):
    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", flag_value)
    monkeypatch.setattr("agent_takkub.core.storage.paths.core_home", lambda: tmp_path)
    from agent_takkub.core.accounts.selector import CooldownFailoverSelector
    from agent_takkub.core.models.account import AccountPool, AccountStatus, ProviderAccount

    pool = AccountPool(id="pool-1", name="pool-1", provider_id="claude", account_ids=("a", "b"))
    accounts = [
        ProviderAccount(id="a", provider_id="claude", label="a", status=AccountStatus.ACTIVE),
        ProviderAccount(id="b", provider_id="claude", label="b", status=AccountStatus.ACTIVE),
    ]
    selector = CooldownFailoverSelector()
    selector.select(pool, accounts)
    accounts[0] = ProviderAccount(
        id="a", provider_id="claude", label="a", status=AccountStatus.ERROR
    )
    selector.select(pool, accounts)  # must not raise even with the flag on
