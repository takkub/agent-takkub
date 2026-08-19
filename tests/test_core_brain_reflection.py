"""core.brain.sources.reflection_source + facade.on_pane_done — the
Reflection hook `orchestrator.done()`/`subagent_done()` call into (epic
#309, Phase 7c)."""

from __future__ import annotations

import pytest

import agent_takkub.core.brain.facade as facade
from agent_takkub.core.brain.sources import reflection_source
from agent_takkub.core.brain.store import BrainStore
from agent_takkub.core.models.memory import MemoryKind, RecordStatus, Trust
from agent_takkub.digest_facts import DigestFacts

# ── reflection_source.from_done_note ────────────────────────────────────


def test_clean_done_note_becomes_a_project_candidate():
    candidate = reflection_source.from_done_note(
        "wired the /auth/login endpoint\nmore detail below",
        project="demo",
        role="backend",
        failed=False,
    )
    assert candidate is not None
    assert candidate.kind == MemoryKind.PROJECT
    assert candidate.content == "wired the /auth/login endpoint"
    assert candidate.trust == Trust.AGENT_REPORTED
    assert candidate.project_id == "demo"
    assert candidate.agent_id == "backend"


def test_failed_done_note_becomes_a_feedback_candidate():
    candidate = reflection_source.from_done_note(
        "pytest failed: fixture 'db' not found", project="demo", role="backend", failed=True
    )
    assert candidate is not None
    assert candidate.kind == MemoryKind.FEEDBACK


def test_blank_note_yields_nothing():
    assert (
        reflection_source.from_done_note("   \n  ", project="demo", role="backend", failed=False)
        is None
    )


def test_too_short_note_yields_nothing():
    assert (
        reflection_source.from_done_note("ok", project="demo", role="backend", failed=False) is None
    )


def test_task_id_is_threaded_through():
    candidate = reflection_source.from_done_note(
        "shipped the migration", project="demo", role="backend", failed=False, task_id="task-9"
    )
    assert candidate.task_id == "task-9"


# ── facade.on_pane_done ──────────────────────────────────────────────────


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import agent_takkub.config as config

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    return tmp_path


def test_on_pane_done_flag_off_touches_nothing(monkeypatch):
    monkeypatch.delenv("TAKKUB_V2_BRAIN", raising=False)

    def boom(*a, **kw):
        raise AssertionError("BrainStore must not be constructed when the flag is off")

    monkeypatch.setattr(facade, "BrainStore", boom)
    facade.on_pane_done("demo", "backend", note="did the thing", failed=False)


def test_on_pane_done_submits_note_and_digest_facts_candidates(monkeypatch, runtime):
    monkeypatch.setenv("TAKKUB_V2_BRAIN", "1")
    facts = DigestFacts(role="backend", verdict="done", branch="wt/backend-1", files_touched=3)
    facade.on_pane_done(
        "demo",
        "backend",
        note="wired the login endpoint end to end",
        digest_facts=facts,
        failed=False,
    )
    records = BrainStore("demo").load_active()
    assert len(records) == 2
    sources = {r.source for r in records}
    assert sources == {"reflection_note", "digest_facts"}
    digest_record = next(r for r in records if r.source == "digest_facts")
    assert digest_record.trust == Trust.COCKPIT_MEASURED
    assert digest_record.status == RecordStatus.ACTIVE


def test_on_pane_done_without_digest_facts_still_submits_the_note(monkeypatch, runtime):
    monkeypatch.setenv("TAKKUB_V2_BRAIN", "1")
    facade.on_pane_done(
        "demo", "backend", note="a subagent note without digest facts", failed=False
    )
    records = BrainStore("demo").load_active()
    assert len(records) == 1
    assert records[0].source == "reflection_note"


def test_on_pane_done_fails_open_on_exception(monkeypatch, runtime):
    monkeypatch.setenv("TAKKUB_V2_BRAIN", "1")

    class _BoomStore:
        def __init__(self, *a, **kw):
            raise RuntimeError("store blew up")

    monkeypatch.setattr(facade, "BrainStore", _BoomStore)
    facade.on_pane_done(
        "demo", "backend", note="anything at all here", failed=False
    )  # must not raise
