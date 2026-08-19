"""orchestrator._inject_v2_context — the Context-Injection hook
`_assign_dispatch` calls into (epic #309, Phase 7c): flag on/off parity
(off = task text byte-identical) and the 300ms timeout path."""

from __future__ import annotations

import time

from agent_takkub.orchestrator import _inject_v2_context

_TASK = "original task text, unchanged unless the hook injects something"


def test_flag_off_returns_task_unchanged(monkeypatch):
    monkeypatch.delenv("TAKKUB_V2_CONTEXT", raising=False)
    assert _inject_v2_context(_TASK, "proj", "backend", "backend", "claude") == _TASK


def test_flag_off_never_imports_the_facade(monkeypatch):
    monkeypatch.delenv("TAKKUB_V2_CONTEXT", raising=False)
    import agent_takkub.core.brain.facade as facade

    def boom(*a, **kw):
        raise AssertionError("facade.build_context_for_assign must not run when the flag is off")

    monkeypatch.setattr(facade, "build_context_for_assign", boom)
    assert _inject_v2_context(_TASK, "proj", "backend", "backend", "claude") == _TASK


def test_flag_on_appends_the_returned_context_block(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_CONTEXT", "1")
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setattr(
        facade, "build_context_for_assign", lambda *a, **kw: "## Context (Takkub brain)\n- a fact"
    )
    out = _inject_v2_context(_TASK, "proj", "backend", "backend", "claude")
    assert out.startswith(_TASK)
    assert "## Context (Takkub brain)" in out


def test_flag_on_empty_context_leaves_task_unchanged(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_CONTEXT", "1")
    import agent_takkub.core.brain.facade as facade

    monkeypatch.setattr(facade, "build_context_for_assign", lambda *a, **kw: "")
    assert _inject_v2_context(_TASK, "proj", "backend", "backend", "claude") == _TASK


def test_timeout_path_leaves_task_unchanged_and_does_not_block_the_caller(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_CONTEXT", "1")
    import agent_takkub.core.brain.facade as facade

    def slow(*a, **kw):
        time.sleep(2)
        return "## Context (Takkub brain)\n- too slow to matter"

    monkeypatch.setattr(facade, "build_context_for_assign", slow)
    started = time.monotonic()
    out = _inject_v2_context(_TASK, "proj", "backend", "backend", "claude")
    elapsed = time.monotonic() - started
    assert out == _TASK
    assert elapsed < 1.0  # bounded by the hook's own 300ms timeout, not the 2s worker


def test_exception_in_facade_call_fails_open(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_CONTEXT", "1")
    import agent_takkub.core.brain.facade as facade

    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(facade, "build_context_for_assign", boom)
    assert _inject_v2_context(_TASK, "proj", "backend", "backend", "claude") == _TASK


def test_flag_on_passes_provider_file_read_support_through(monkeypatch):
    monkeypatch.setenv("TAKKUB_V2_CONTEXT", "1")
    import agent_takkub.core.brain.facade as facade
    from agent_takkub.provider_spec import PROVIDER_REGISTRY

    seen: dict = {}

    def spy(project, role, task_text, *, file_read_supported=True, **kw):
        seen["file_read_supported"] = file_read_supported
        return ""

    monkeypatch.setattr(facade, "build_context_for_assign", spy)
    _inject_v2_context(_TASK, "proj", "backend", "backend", "codex")
    assert seen["file_read_supported"] == PROVIDER_REGISTRY["codex"].supports_agent_file_read
