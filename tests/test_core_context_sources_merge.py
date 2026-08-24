"""core.context_sources sources + core.brain.context_builder's OpenViking
hybrid merge (issue #372): each ContextSource's retrieve(), the merge/
budget/trace step, resource indexing bookkeeping, and the doctor section —
disabled must stay a true no-op (byte-for-byte identical `build_context`
output), per the task's own "behaviour เดิมเมื่อ disabled ต้องเหมือนเดิม"
requirement.
"""

from __future__ import annotations

import pytest

from agent_takkub.core.brain import context_builder
from agent_takkub.core.brain.store import BrainStore
from agent_takkub.core.context_sources.brain_source import BrainSource
from agent_takkub.core.context_sources.conversation_source import ConversationSource
from agent_takkub.core.context_sources.openviking_source import OpenVikingSource
from agent_takkub.core.context_sources.resource_source import ResourceSource
from agent_takkub.core.models.memory import MemoryKind, MemoryRecord


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import agent_takkub.config as config

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_HOME", tmp_path / "data")
    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "0")
    monkeypatch.delenv("TAKKUB_OPENVIKING_ENABLED", raising=False)
    monkeypatch.delenv("TAKKUB_OPENVIKING_MODE", raising=False)
    return tmp_path


def _rec(**kwargs) -> MemoryRecord:
    defaults = dict(id=kwargs.pop("id", "r"), kind=MemoryKind.PROJECT, content="x")
    defaults.update(kwargs)
    return MemoryRecord(**defaults)


# ── BrainSource / ConversationSource: thin wrappers over context_builder ──


def test_brain_source_matches_context_builder_recall(runtime):
    BrainStore("proj").append_event(
        _rec(id="a", content="backend uses codex as its default provider")
    )
    items = BrainSource().retrieve(
        "codex provider default", project="proj", role="backend", budget_tokens=2000
    )
    assert len(items) == 1
    assert "codex as its default provider" in items[0].text
    assert items[0].source == "brain"
    assert items[0].provenance == "memory:a"


def test_brain_source_empty_query_returns_nothing(runtime):
    assert BrainSource().retrieve("   ", project="proj", role="backend", budget_tokens=2000) == []


def test_conversation_source_reads_rolling_summary(runtime, monkeypatch):
    from agent_takkub.core.conversation.store import ConversationStore, conversation_id_for
    from agent_takkub.core.conversation.summary import RollingSummary, save_summary

    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "1")
    store = ConversationStore()
    conv_id = conversation_id_for("proj", "backend")
    save_summary(
        store.conversation_dir("proj", conv_id),
        RollingSummary(current_state="wired the login endpoint"),
    )

    items = ConversationSource().retrieve(
        "anything", project="proj", role="backend", budget_tokens=2000
    )
    assert any("wired the login endpoint" in i.text for i in items)
    assert all(i.source == "conversation" for i in items)


def test_conversation_source_flag_off_returns_nothing(runtime):
    items = ConversationSource().retrieve(
        "anything", project="proj", role="backend", budget_tokens=2000
    )
    assert items == []


# ── ResourceSource: local BM25 over the allowlisted vault ────────────────


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "01-Projects" / "demo").mkdir(parents=True)
    (root / "99-Logs").mkdir(parents=True)
    monkeypatch.setenv("TAKKUB_VAULT_DIR", str(root))
    return root


def test_resource_source_finds_allowlisted_doc(vault):
    (vault / "01-Projects" / "demo" / "auth.md").write_text(
        "the login endpoint uses JWT tokens for session auth", encoding="utf-8"
    )
    items = ResourceSource().retrieve(
        "JWT session auth", project="demo", role="backend", budget_tokens=2000
    )
    assert len(items) == 1
    assert items[0].source == "resource"
    assert items[0].trust == "curated"
    assert items[0].provenance == "01-Projects/demo/auth.md"


def test_resource_source_excludes_denylisted_logs(vault):
    (vault / "99-Logs" / "raw.md").write_text(
        "JWT session auth secret token dump", encoding="utf-8"
    )
    items = ResourceSource().retrieve(
        "JWT session auth", project="demo", role="backend", budget_tokens=2000
    )
    assert items == []


def test_resource_source_no_vault_configured_returns_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKKUB_VAULT_DIR", str(tmp_path / "does-not-exist"))
    assert (
        ResourceSource().retrieve("anything", project=None, role="backend", budget_tokens=2000)
        == []
    )


def test_resource_source_zero_budget_returns_nothing(vault):
    (vault / "01-Projects" / "demo" / "a.md").write_text(
        "some content about deploys", encoding="utf-8"
    )
    assert (
        ResourceSource().retrieve("deploys", project="demo", role="backend", budget_tokens=0) == []
    )


# ── OpenVikingSource: gated on adapter.enabled() ──────────────────────────


def test_openviking_source_disabled_returns_nothing_without_network(monkeypatch):
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "0")
    from agent_takkub.core.context_sources import openviking_adapter

    def boom(*a, **kw):
        raise AssertionError("must not touch the network when disabled")

    monkeypatch.setattr(openviking_adapter.urllib.request, "urlopen", boom)
    items = OpenVikingSource().retrieve("q", project=None, role="backend", budget_tokens=2000)
    assert items == []


def test_openviking_source_maps_hits_to_context_items(monkeypatch):
    from agent_takkub.core.context_sources import openviking_adapter

    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    monkeypatch.setattr(
        openviking_adapter,
        "search_resources",
        lambda query, limit=8: [
            openviking_adapter.SearchHit(
                uri="viking://a", text="curated fact", score=0.8, category="resource"
            )
        ],
    )
    items = OpenVikingSource().retrieve("q", project=None, role="backend", budget_tokens=2000)
    assert len(items) == 1
    assert items[0].source == "openviking"
    assert items[0].trust == "external"
    assert items[0].provenance == "viking://a"


# ── context_builder.merge_openviking_traced: the actual hybrid merge ─────


def test_merge_disabled_is_byte_identical_noop(runtime):
    text, trace = context_builder.merge_openviking_traced(
        "## Context (Takkub brain)\n- (project, medium) some fact",
        project="proj",
        role="backend",
        task_text="anything",
        budget_tokens=2000,
    )
    assert text == "## Context (Takkub brain)\n- (project, medium) some fact"
    assert trace is None


def test_merge_shadow_mode_traces_but_never_injects(runtime, monkeypatch):
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    monkeypatch.setenv("TAKKUB_OPENVIKING_MODE", "shadow")
    from agent_takkub.core.context_sources.openviking_source import OpenVikingSource as _OVS

    monkeypatch.setattr(
        _OVS,
        "retrieve",
        lambda self, query, *, project, role, budget_tokens: [
            _fake_item("shadow-only knowledge that must not appear")
        ],
    )
    base = "## Context (Takkub brain)\n- (project, medium) some fact"
    text, trace = context_builder.merge_openviking_traced(
        base, project="proj", role="backend", task_text="q", budget_tokens=2000
    )
    assert text == base
    assert "shadow-only knowledge" not in text
    assert trace is not None
    assert trace.mode == "shadow"
    assert trace.sources[0].count == 1


def test_merge_read_mode_appends_openviking_section(runtime, monkeypatch):
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    monkeypatch.setenv("TAKKUB_OPENVIKING_MODE", "read")
    from agent_takkub.core.context_sources.openviking_source import OpenVikingSource as _OVS

    monkeypatch.setattr(
        _OVS,
        "retrieve",
        lambda self, query, *, project, role, budget_tokens: [
            _fake_item("openviking resource content about deploys")
        ],
    )
    text, trace = context_builder.merge_openviking_traced(
        "", project="proj", role="backend", task_text="q", budget_tokens=2000
    )
    assert "### Knowledge (OpenViking)" in text
    assert "openviking resource content about deploys" in text
    assert trace.mode == "read"


def test_merge_read_mode_never_calls_local_resource_source(runtime, monkeypatch):
    """read = OpenViking resources only, per `14_OPENVIKING_INTEGRATION.md`
    rollout stage D — local `resource_source` is a hybrid-only addition."""
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    monkeypatch.setenv("TAKKUB_OPENVIKING_MODE", "read")
    from agent_takkub.core.context_sources.resource_source import ResourceSource as _RS

    def boom(self, *a, **kw):
        raise AssertionError("read mode must not query the local resource source")

    monkeypatch.setattr(_RS, "retrieve", boom)
    context_builder.merge_openviking_traced(
        "", project="proj", role="backend", task_text="q", budget_tokens=2000
    )


def test_merge_hybrid_mode_includes_both_sources(runtime, monkeypatch):
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    monkeypatch.setenv("TAKKUB_OPENVIKING_MODE", "hybrid")
    from agent_takkub.core.context_sources.openviking_source import OpenVikingSource as _OVS
    from agent_takkub.core.context_sources.resource_source import ResourceSource as _RS

    monkeypatch.setattr(
        _OVS,
        "retrieve",
        lambda self, query, *, project, role, budget_tokens: [
            _fake_item("openviking hit about deploy timing")
        ],
    )
    monkeypatch.setattr(
        _RS,
        "retrieve",
        lambda self, query, *, project, role, budget_tokens: [
            _fake_item("local curated doc about deploy checklist", trust="curated")
        ],
    )
    text, trace = context_builder.merge_openviking_traced(
        "", project="proj", role="backend", task_text="q", budget_tokens=2000
    )
    assert "openviking hit about deploy timing" in text
    assert "local curated doc about deploy checklist" in text
    assert len(trace.sources) == 2


def test_merge_budget_trims_lowest_priority_first(runtime, monkeypatch):
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    monkeypatch.setenv("TAKKUB_OPENVIKING_MODE", "hybrid")
    from agent_takkub.core.context_sources.openviking_source import OpenVikingSource as _OVS
    from agent_takkub.core.context_sources.resource_source import ResourceSource as _RS

    big = "filler word " * 400  # ~4800 chars, way over a tiny budget
    monkeypatch.setattr(
        _OVS,
        "retrieve",
        lambda self, query, *, project, role, budget_tokens: [
            _fake_item(f"external {big}", trust="external")
        ],
    )
    monkeypatch.setattr(
        _RS,
        "retrieve",
        lambda self, query, *, project, role, budget_tokens: [
            _fake_item("short curated note", trust="curated")
        ],
    )
    text, _trace = context_builder.merge_openviking_traced(
        "", project="proj", role="backend", task_text="q", budget_tokens=20
    )
    assert "short curated note" in text
    assert "filler word" not in text


def test_merge_openviking_fail_open_on_exception(runtime, monkeypatch):
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    from agent_takkub.core.context_sources.openviking_source import OpenVikingSource as _OVS

    def boom(self, *a, **kw):
        raise RuntimeError("sidecar blew up")

    monkeypatch.setattr(_OVS, "retrieve", boom)
    with pytest.raises(RuntimeError):
        context_builder.merge_openviking_traced(
            "base", project="proj", role="backend", task_text="q", budget_tokens=2000
        )
    # merge_openviking() itself is NOT the fail-open boundary (facade is) —
    # confirms facade.build_context_for_assign is what must catch this.


def _fake_item(text: str, *, trust: str = "external"):
    from agent_takkub.core.context_sources.base import ContextItem

    return ContextItem(
        text=text,
        tokens=max(1, len(text) // 4),
        source="openviking",
        provenance="p",
        trust=trust,
        score=0.5,
    )


# ── facade wiring: disabled must reproduce the exact pre-#372 behavior ───


def test_facade_build_context_unaffected_when_openviking_disabled(runtime, monkeypatch):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_V2_CONTEXT", "1")
    BrainStore("proj").append_event(_rec(id="a", content="the deploy pipeline uses github actions"))
    out = facade.build_context_for_assign("proj", "backend", "github actions deploy pipeline")
    assert "the deploy pipeline uses github actions" in out
    assert "OpenViking" not in out


def test_facade_openviking_merge_failure_falls_back_to_base_text(runtime, monkeypatch):
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setenv("TAKKUB_V2_CONTEXT", "1")
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    BrainStore("proj").append_event(_rec(id="a", content="the deploy pipeline uses github actions"))

    def boom(*a, **kw):
        raise RuntimeError("merge blew up")

    monkeypatch.setattr(facade.context_builder, "merge_openviking_traced", boom)
    out = facade.build_context_for_assign("proj", "backend", "github actions deploy pipeline")
    assert "the deploy pipeline uses github actions" in out


# ── indexing.py ────────────────────────────────────────────────────────


def test_index_vault_disabled_returns_reason(runtime):
    from agent_takkub.core.context_sources import indexing

    result = indexing.index_vault("proj")
    assert result.ok is False
    assert "ENABLED" in result.reason


def test_index_vault_no_vault_configured(runtime, monkeypatch):
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    monkeypatch.setenv("TAKKUB_VAULT_DIR", str(runtime / "no-such-vault"))
    from agent_takkub.core.context_sources import indexing

    result = indexing.index_vault("proj")
    assert result.ok is False
    assert "vault" in result.reason


def test_index_vault_incremental_skip_on_unchanged_hash(runtime, monkeypatch, vault):
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    from agent_takkub.core.context_sources import indexing, openviking_adapter

    monkeypatch.setattr(openviking_adapter, "add_resource", lambda path, **kw: True)
    (vault / "01-Projects" / "demo" / "a.md").write_text("hello world", encoding="utf-8")

    first = indexing.index_vault("demo")
    assert first.ok is True
    assert first.added == 1
    assert first.skipped == 0

    second = indexing.index_vault("demo")
    assert second.added == 0
    assert second.skipped == 1


def test_index_vault_changed_content_reindexes(runtime, monkeypatch, vault):
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    from agent_takkub.core.context_sources import indexing, openviking_adapter

    monkeypatch.setattr(openviking_adapter, "add_resource", lambda path, **kw: True)
    doc = vault / "01-Projects" / "demo" / "a.md"
    doc.write_text("hello world", encoding="utf-8")
    indexing.index_vault("demo")

    doc.write_text("hello world, edited", encoding="utf-8")
    result = indexing.index_vault("demo")
    assert result.added == 1
    assert result.skipped == 0


def test_index_vault_add_resource_failure_is_counted_not_raised(runtime, monkeypatch, vault):
    monkeypatch.setenv("TAKKUB_OPENVIKING_ENABLED", "1")
    from agent_takkub.core.context_sources import indexing, openviking_adapter

    monkeypatch.setattr(openviking_adapter, "add_resource", lambda path, **kw: False)
    (vault / "01-Projects" / "demo" / "a.md").write_text("hello world", encoding="utf-8")
    result = indexing.index_vault("demo")
    assert result.ok is True
    assert result.failed == 1
    assert result.added == 0


def test_index_status_reports_disabled(runtime):
    from agent_takkub.core.context_sources import indexing

    status = indexing.index_status("proj")
    assert status["enabled"] is False
    assert status["indexed_count"] == 0


# ── doctor_section.py ─────────────────────────────────────────────────


def test_doctor_findings_skip_when_disabled(runtime):
    from agent_takkub.core.context_sources.doctor_section import build_findings
    from agent_takkub.doctor import Status

    findings = build_findings()
    names = {f.name: f for f in findings}
    assert names["openviking"].status == Status.SKIP
    assert names["last-trace"].status == Status.SKIP


def test_doctor_findings_report_last_trace(runtime, monkeypatch):
    from agent_takkub.core.brain.context_builder import ContextTrace, SourceTrace
    from agent_takkub.core.context_sources.doctor_section import build_findings
    from agent_takkub.core.context_sources.trace_store import save_last_trace
    from agent_takkub.doctor import Status

    trace = ContextTrace(
        mode="hybrid",
        sources=(SourceTrace("OpenViking", 2, "resources", 300),),
        total_tokens=900,
        budget_tokens=2000,
        dedup_count=1,
        latency_ms=12.5,
    )
    save_last_trace(trace, project="proj", role="backend")

    findings = build_findings()
    names = {f.name: f for f in findings}
    assert names["last-trace"].status == Status.INFO
    assert "hybrid" in names["last-trace"].detail
    assert "900/2000" in names["last-trace"].detail
