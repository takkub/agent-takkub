"""core.brain.task_complexity — Classifier v2 (`docs/plans/v2-hardening-
2026-08-24/02_CLASSIFIER_V2.md`): structural/risk-scored Stage 2 layered in
front of `context_gate.classify_task_size`'s Stage 1 heuristic. Pure/stdlib-
only, no I/O — no fixtures needed beyond `monkeypatch` for the no-network
guard below.
"""

from __future__ import annotations

import socket

from agent_takkub.core.brain import task_complexity
from agent_takkub.core.brain.task_complexity import TaskComplexity, classify_task_complexity

# ── score buckets — 0-4 small, 5-9 medium, 10+ large ───────────────────────


def test_small_bucket_no_structural_signals():
    result = classify_task_complexity("fix spacing")
    assert result.size == "small"
    assert result.score <= 4
    assert result.risk_flags == ()


def test_medium_bucket_from_structural_signals():
    text = (
        "Add a new endpoint and update the database schema; touches "
        "utils.py and handlers.py across 2 modules"
    )
    result = classify_task_complexity(text)
    assert result.size == "medium"
    assert 5 <= result.score <= 9
    assert result.estimated_files == 2
    assert result.estimated_modules == 2
    assert result.risk_flags == ()


def test_large_bucket_from_many_structural_signals():
    text = (
        "Migrate the payment and auth database schema, add new API endpoints, "
        "touches billing.py, auth.py, api.py, and models.py across 4 modules; "
        "deploy to production with a rollback plan, coordinate with frontend "
        "and backend, check history and design mockups for the new screens"
    )
    result = classify_task_complexity(text)
    assert result.size == "large"
    assert result.score >= 10
    assert result.risk_flags  # auth/payment/migration/prod all present


def test_large_bucket_via_stage1_thai_keyword_dominance():
    # No structural signals score high here — Stage 1's own TH "large"
    # keyword table drives it, and Stage 2 must not downgrade below that.
    result = classify_task_complexity("ออกแบบระบบใหม่ทั้งหมด")
    assert result.size == "large"


# ── hard override: risk domain present => never SMALL ──────────────────────


def test_risk_domain_overrides_small_signal_word():
    # "แก้สี" (change color) is a Stage 1 SMALL-signal word, but "login"/
    # "auth" match the auth risk domain — must never classify small.
    result = classify_task_complexity("แก้สี button หน้า login auth")
    assert result.size != "small"
    assert "auth" in result.risk_flags


def test_risk_domain_english_payment_never_small():
    result = classify_task_complexity("update the payment button label")
    assert result.size != "small"
    assert "payment" in result.risk_flags


# ── confidence: lower when Stage 1 and the structural score disagree ───────


def test_confidence_lower_when_stage1_disagrees_with_score():
    # Stage 1's keyword table flags "refactor" as medium; Stage 2's own
    # structural signals find nothing to score (no file/api/risk hits) —
    # the two disagree, so confidence must reflect that.
    disagreeing = classify_task_complexity("refactor the whole feature")
    agreeing = classify_task_complexity("fix spacing")
    assert disagreeing.size == "medium"  # Stage 1's floor still wins
    assert disagreeing.confidence < agreeing.confidence
    assert any("disagreed" in r for r in disagreeing.reasons)


# ── Stage 1 fallback when task text is empty ────────────────────────────────


def test_empty_text_falls_back_to_stage1():
    result = classify_task_complexity("")
    assert result.size == "small"
    assert result.score == 0
    assert result.estimated_files == 0
    assert result.estimated_modules == 0


def test_whitespace_only_text_falls_back_to_stage1():
    result = classify_task_complexity("   ")
    assert result.size == "small"


# ── explicit override via flags={"context": ...} still wins outright ───────


def test_explicit_override_short_circuits_scoring():
    result = classify_task_complexity("fix spacing", flags={"context": "large"})
    assert result.size == "large"
    assert result.confidence == 1.0


# ── output shape ────────────────────────────────────────────────────────


def test_returns_task_complexity_dataclass():
    result = classify_task_complexity("update the login auth flow")
    assert isinstance(result, TaskComplexity)
    assert isinstance(result.reasons, tuple)
    assert isinstance(result.risk_flags, tuple)
    assert 0.1 <= result.confidence <= 1.0


# ── no network / LLM call — purely local text scoring ───────────────────────


def test_no_network_call(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("classify_task_complexity must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    classify_task_complexity(
        "migrate the auth database schema across 3 files in 2 modules, deploy to production"
    )


def test_module_has_no_network_or_llm_imports():
    import inspect

    source = inspect.getsource(task_complexity)
    for forbidden in ("import requests", "import httpx", "import socket", "import urllib"):
        assert forbidden not in source
