"""core.brain.context_builder — Context Builder (epic #309, Phase 7c):
budget sizing, empty/blank input, bounded assembly from RetrievalEngine +
the Conversation rolling summary."""

from __future__ import annotations

import pytest

from agent_takkub.core.brain import context_builder
from agent_takkub.core.brain.store import BrainStore
from agent_takkub.core.models.memory import MemoryKind, MemoryRecord, Scope


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import agent_takkub.config as config

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    # The Context Gate (closeout #C) writes a trace on every gated
    # `build_context_for_assign` call — DATA_HOME must be redirected too or
    # these tests write a real `context/last_context_trace.json` next to
    # this dev checkout (`config.DATA_HOME == REPO_ROOT` on a dev checkout).
    monkeypatch.setattr(config, "DATA_HOME", tmp_path / "data")
    # Explicitly OFF, not merely unset: the shipped default is ON since
    # 1.0.84, so "no env var" no longer means "feature disabled".
    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "0")
    # v2-hardening C: pin Context Strategy so these tests never depend on a
    # real dev machine's persisted core-v2-settings.json (see the identical
    # note in test_core_brain_context_gate_facade.py's own `runtime` fixture).
    monkeypatch.setenv("TAKKUB_CONTEXT_STRATEGY", "automatic")
    return tmp_path


def _rec(**kwargs) -> MemoryRecord:
    defaults = dict(id=kwargs.pop("id", "r"), kind=MemoryKind.PROJECT, content="x")
    defaults.update(kwargs)
    return MemoryRecord(**defaults)


# ── budget_tokens_for ────────────────────────────────────────────────────


def test_budget_is_12pct_of_context_window_clamped():
    assert context_builder.budget_tokens_for(10_000) == max(
        context_builder._BUDGET_FLOOR, int(10_000 * 0.12)
    )


def test_budget_clamps_to_the_ceiling_for_a_huge_context_window():
    assert context_builder.budget_tokens_for(1_000_000) == context_builder._BUDGET_CEILING


def test_budget_clamps_to_the_floor_for_a_tiny_context_window():
    assert context_builder.budget_tokens_for(100) == context_builder._BUDGET_FLOOR


def test_budget_falls_back_to_default_window_when_unknown():
    assert context_builder.budget_tokens_for(None) == context_builder.budget_tokens_for(
        context_builder._DEFAULT_CONTEXT_WINDOW
    )


def test_budget_is_cut_down_when_file_read_unsupported():
    with_read = context_builder.budget_tokens_for(50_000, file_read_supported=True)
    without_read = context_builder.budget_tokens_for(50_000, file_read_supported=False)
    assert without_read < with_read
    assert without_read >= context_builder._NO_FILE_READ_FLOOR


# ── build_context: empty / bounded input ────────────────────────────────


def test_zero_budget_returns_empty_string(runtime):
    assert context_builder.build_context("proj", "backend", "do the thing", 0) == ""


def test_blank_task_text_returns_empty_string(runtime):
    assert context_builder.build_context("proj", "backend", "   ", 2000) == ""


def test_empty_store_returns_empty_string(runtime):
    assert context_builder.build_context("proj", "backend", "anything relevant", 2000) == ""


# ── build_context: assembly from RetrievalEngine ────────────────────────


def test_relevant_project_memory_is_included(runtime):
    store = BrainStore("proj")
    store.append_event(
        _rec(id="a", content="backend uses codex as its default provider for this project")
    )
    out = context_builder.build_context("proj", "backend", "codex provider default", 2000)
    assert "## Context (Takkub brain)" in out
    assert "backend uses codex as its default provider" in out


def test_global_scope_record_is_included_alongside_project_ones(runtime):
    BrainStore("proj").append_event(_rec(id="p1", content="project scoped fact about deploy"))
    BrainStore(None).append_event(
        _rec(id="g1", content="global fact about deploy conventions", scope=Scope.GLOBAL)
    )
    out = context_builder.build_context("proj", "backend", "deploy conventions", 2000)
    assert "project scoped fact about deploy" in out
    assert "global fact about deploy conventions" in out


def test_other_roles_agent_scoped_record_is_excluded(runtime):
    store = BrainStore("proj")
    store.append_event(
        _rec(
            id="mine",
            content="backend private note about the retry policy",
            scope=Scope.AGENT,
            agent_id="backend",
        )
    )
    store.append_event(
        _rec(
            id="theirs",
            content="frontend private note about the retry policy",
            scope=Scope.AGENT,
            agent_id="frontend",
        )
    )
    out = context_builder.build_context("proj", "backend", "retry policy", 2000)
    assert "backend private note" in out
    assert "frontend private note" not in out


def test_other_roles_project_scoped_record_is_kept(runtime):
    """The role filter must key on SCOPE, not on `agent_id`: a PROJECT-scoped
    digest carries the reporting role in `agent_id` too, so filtering on
    `agent_id in (None, role)` threw away every other role's cockpit-measured
    findings — the highest-trust records in the store."""
    store = BrainStore("proj")
    store.append_event(
        _rec(
            id="qa",
            content="qa done — verify the retry policy holds under load",
            scope=Scope.PROJECT,
            agent_id="qa",
        )
    )
    out = context_builder.build_context("proj", "backend", "verify retry policy load", 2000)
    assert "qa done" in out


def test_shard_instance_reads_its_base_roles_agent_memory(runtime):
    store = BrainStore("proj")
    store.append_event(
        _rec(
            id="mine",
            content="backend private note about the retry policy",
            scope=Scope.AGENT,
            agent_id="backend",
        )
    )
    out = context_builder.build_context("proj", "backend#2", "retry policy", 2000)
    assert "backend private note" in out


def test_the_two_records_one_done_writes_are_collapsed(runtime):
    """`facade.on_pane_done` submits the agent's headline AND the digest that
    embeds that same headline; they differ in scope so `pipeline`'s dedup can
    never see them as one (#332)."""
    headline = "pruned 21 redundant theme layouts down to 10 verified keepers"
    store = BrainStore("proj")
    store.append_event(_rec(id="note", content=headline, scope=Scope.AGENT, agent_id="frontend"))
    store.append_event(
        _rec(
            id="digest",
            content=f"frontend done branch=master files_touched=101 — {headline}",
            scope=Scope.PROJECT,
            agent_id="frontend",
        )
    )
    out = context_builder.build_context("proj", "frontend", "theme layouts pruned", 2000)
    assert out.count("pruned 21 redundant theme layouts") == 1
    # The survivor is the one carrying MORE information, not just the first.
    assert "files_touched=101" in out


def test_distinct_events_sharing_vocabulary_are_both_kept(runtime):
    """The collapse threshold sits above `pipeline._SEMANTIC_MATCH_THRESHOLD`
    precisely so two separate runs of the same job stay two records."""
    store = BrainStore("proj")
    store.append_event(
        _rec(id="one", content="rebuild and restart admin and frontend, both healthy again")
    )
    store.append_event(
        _rec(id="two", content="rebuild and restart the api container, health check green")
    )
    out = context_builder.build_context("proj", "devops", "rebuild restart", 2000)
    assert "both healthy again" in out
    assert "health check green" in out


def test_bounded_by_record_count_cap(runtime):
    store = BrainStore("proj")
    for i in range(20):
        store.append_event(_rec(id=f"r{i}", content=f"fact number {i} about the shared project"))
    out = context_builder.build_context("proj", "backend", "fact about the shared project", 100_000)
    bullet_lines = [ln for ln in out.splitlines() if ln.startswith("- (")]
    assert len(bullet_lines) <= context_builder._RECALL_LIMIT_SCOPED


def test_tiny_budget_still_keeps_the_top_hit(runtime):
    BrainStore("proj").append_event(
        _rec(id="a", content="matching keyword " + ("filler word " * 200))
    )
    out = context_builder.build_context("proj", "backend", "matching keyword", 1)
    assert "matching keyword" in out


# ── build_context: conversation rolling summary ─────────────────────────


def test_recent_summary_included_when_conversation_flag_on(runtime, monkeypatch):
    from agent_takkub.core.conversation.store import ConversationStore, conversation_id_for
    from agent_takkub.core.conversation.summary import RollingSummary, save_summary

    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "1")
    conv_store = ConversationStore()
    conv_id = conversation_id_for("proj", "backend")
    conv_dir = conv_store.conversation_dir("proj", conv_id)
    save_summary(conv_dir, RollingSummary(current_state="[backend] wired the login endpoint"))

    out = context_builder.build_context("proj", "backend", "anything", 2000)
    assert "### Recent summary" in out
    assert "wired the login endpoint" in out


def test_recent_summary_omitted_when_conversation_flag_off(runtime, monkeypatch):
    from agent_takkub.core.conversation.store import ConversationStore, conversation_id_for
    from agent_takkub.core.conversation.summary import RollingSummary, save_summary

    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "0")
    conv_store = ConversationStore()
    conv_id = conversation_id_for("proj", "backend")
    conv_dir = conv_store.conversation_dir("proj", conv_id)
    save_summary(conv_dir, RollingSummary(current_state="[backend] wired the login endpoint"))

    out = context_builder.build_context("proj", "backend", "anything", 2000)
    assert "wired the login endpoint" not in out


# ── facade.build_context_for_assign ─────────────────────────────────────


def test_facade_build_context_flag_off_returns_empty_without_touching_store(monkeypatch):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.delenv("TAKKUB_V2_CONTEXT", raising=False)

    def boom(*a, **kw):
        raise AssertionError("context_builder.build_context must not run when the flag is off")

    monkeypatch.setattr(facade.context_builder, "build_context", boom)
    assert facade.build_context_for_assign("proj", "backend", "do the thing") == ""


def test_facade_build_context_flag_on_finds_a_relevant_record(monkeypatch, runtime):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_V2_CONTEXT", "1")
    BrainStore("proj").append_event(_rec(id="a", content="the deploy pipeline uses github actions"))
    out = facade.build_context_for_assign("proj", "backend", "github actions deploy pipeline")
    assert "the deploy pipeline uses github actions" in out


def test_facade_build_context_is_independent_of_the_brain_write_flag(monkeypatch, runtime):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_V2_CONTEXT", "1")
    monkeypatch.delenv("TAKKUB_V2_BRAIN", raising=False)
    BrainStore("proj").append_event(_rec(id="a", content="the deploy pipeline uses github actions"))
    out = facade.build_context_for_assign("proj", "backend", "github actions deploy pipeline")
    assert "the deploy pipeline uses github actions" in out


def test_facade_build_context_fails_open_on_exception(monkeypatch, runtime):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_V2_CONTEXT", "1")

    def boom(*a, **kw):
        raise RuntimeError("context builder blew up")

    monkeypatch.setattr(facade.context_builder, "build_context", boom)
    assert facade.build_context_for_assign("proj", "backend", "do the thing") == ""


def test_facade_build_context_cuts_budget_when_file_read_unsupported(monkeypatch, runtime):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_V2_CONTEXT", "1")
    seen: dict = {}

    def spy(project, role, task_text, budget_tokens):
        seen["budget"] = budget_tokens
        return ""

    monkeypatch.setattr(facade.context_builder, "build_context", spy)
    facade.build_context_for_assign(
        "proj", "backend", "task", context_window=50_000, file_read_supported=False
    )
    assert seen["budget"] == context_builder.budget_tokens_for(50_000, file_read_supported=False)
