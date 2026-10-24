"""core.context_sources — `trace_store` (redacted last-trace persistence) and
the `doctor_section` last-trace report."""

from __future__ import annotations

import pytest


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import agent_takkub.config as config

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_HOME", tmp_path / "data")
    monkeypatch.setenv("TAKKUB_V2_CONVERSATION", "0")
    return tmp_path


def test_save_last_trace_redacts_secret_in_rejected_examples(runtime):
    from agent_takkub.core.brain.context_builder import ContextTrace
    from agent_takkub.core.context_sources.trace_store import load_last_trace, save_last_trace

    trace = ContextTrace(
        mode="gated:medium",
        sources=(),
        total_tokens=10,
        budget_tokens=100,
        rejected_examples=("REJECTED doc reason=leaked sk-ant-abcdefgh12345678 token",),
    )
    save_last_trace(trace, project="proj", role="backend")
    saved = load_last_trace()
    assert "sk-ant-abcdefgh12345678" not in saved["rejected_examples"][0]
    assert "***REDACTED***" in saved["rejected_examples"][0]


def test_save_last_trace_redacts_secret_in_complexity_reasons(runtime):
    from types import SimpleNamespace

    from agent_takkub.core.brain.context_builder import ContextTrace
    from agent_takkub.core.context_sources.trace_store import load_last_trace, save_last_trace

    trace = ContextTrace(mode="gated:small", sources=(), total_tokens=1, budget_tokens=10)
    complexity = SimpleNamespace(
        score=1,
        confidence="high",
        reasons=("leaked credential Bearer abc123.def456-token",),
        risk_flags=(),
        estimated_files=1,
        estimated_modules=1,
    )
    save_last_trace(trace, project="proj", role="backend", complexity=complexity)
    saved = load_last_trace()
    assert "abc123.def456-token" not in saved["reasons"][0]
    assert "***REDACTED***" in saved["reasons"][0]


def test_save_last_trace_redacts_secret_in_skipped_reason(runtime):
    from agent_takkub.core.brain.context_builder import ContextTrace
    from agent_takkub.core.context_sources.trace_store import load_last_trace, save_last_trace

    trace = ContextTrace(mode="gated:small", sources=(), total_tokens=1, budget_tokens=10)
    save_last_trace(
        trace,
        project="proj",
        role="backend",
        skipped=[{"name": "resource", "reason": "api_key=sk-ant-zzzzzzzz99999999 rejected"}],
    )
    saved = load_last_trace()
    assert "sk-ant-zzzzzzzz99999999" not in saved["skipped"][0]["reason"]
    assert "***REDACTED***" in saved["skipped"][0]["reason"]


# ── doctor_section.py ─────────────────────────────────────────────────


def test_doctor_findings_skip_when_no_trace_recorded(runtime):
    from agent_takkub.core.context_sources.doctor_section import build_findings
    from agent_takkub.doctor import Status

    findings = build_findings()
    names = {f.name: f for f in findings}
    assert names["last-trace"].status == Status.SKIP


def test_doctor_findings_report_last_trace(runtime, monkeypatch):
    from agent_takkub.core.brain.context_builder import ContextTrace, SourceTrace
    from agent_takkub.core.context_sources.doctor_section import build_findings
    from agent_takkub.core.context_sources.trace_store import save_last_trace
    from agent_takkub.doctor import Status

    trace = ContextTrace(
        mode="gated:medium",
        sources=(SourceTrace("Resource", 2, "docs", 300),),
        total_tokens=900,
        budget_tokens=2000,
        dedup_count=1,
        latency_ms=12.5,
    )
    save_last_trace(trace, project="proj", role="backend")

    findings = build_findings()
    names = {f.name: f for f in findings}
    assert names["last-trace"].status == Status.INFO
    assert "gated:medium" in names["last-trace"].detail
    assert "900/2000" in names["last-trace"].detail
