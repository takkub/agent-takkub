"""#103 (2026-08-31): AgentPaneModel's token-badge formatting/state for the
provider-neutral token meter — format_token_badge's provider-reported `limit`
override, format_unsupported_badge, record_token_meter_result, and
token_meter_context (the DATA-MIN summary remote/api.py's /api/activity reads).

AgentPaneModel has no QWidget/display dependency (see its own module
docstring), so these are plain unit tests — no QApplication needed.
"""

from __future__ import annotations

from agent_takkub.agent_pane_model import AgentPaneModel
from agent_takkub.roles import by_name


def _model() -> AgentPaneModel:
    m = AgentPaneModel(by_name("backend"))
    m.session = object()  # current_usage()/token_meter_context() require a live session
    return m


class TestFormatTokenBadgeLimitOverride:
    def test_provider_reported_limit_wins_over_model_table(self) -> None:
        m = _model()
        usage = {
            "model": "codex",
            "prompt": 200_000,
            "input": 200_000,
            "cache_creation": 0,
            "cache_read": 0,
            "output": 100,
            "limit": 258_400,
        }
        badge = m.format_token_badge(usage)
        # The claude-model table's _DEFAULT_LIMIT (200_000) would read this as
        # >=100% full; the real codex-reported cap must be used instead.
        assert badge["limit"] == 258_400
        assert "77%" in badge["text"] or "78%" in badge["text"]

    def test_claude_dict_with_no_limit_key_falls_back_to_table(self) -> None:
        # Pre-#103 claude usage dicts never carried a "limit" key at all —
        # this is the exact shape read_last_usage still returns.
        m = _model()
        usage = {
            "model": "claude-sonnet-5",
            "prompt": 500_000,
            "input": 500_000,
            "cache_creation": 0,
            "cache_read": 0,
            "output": 10,
        }
        badge = m.format_token_badge(usage)
        assert badge["limit"] == 1_000_000  # claude-sonnet-5's table entry

    def test_none_limit_falls_back_to_table(self) -> None:
        m = _model()
        usage = {
            "model": "claude-haiku-4-5",
            "prompt": 1000,
            "input": 1000,
            "cache_creation": 0,
            "cache_read": 0,
            "output": 5,
            "limit": None,
        }
        badge = m.format_token_badge(usage)
        assert badge["limit"] == 200_000


class TestFormatUnsupportedBadge:
    def test_text_and_reason(self) -> None:
        m = _model()
        badge = m.format_unsupported_badge(
            {"status": "unsupported", "reason": "no schema confirmed"}
        )
        assert badge["text"] == "tokens n/a"
        assert badge["tooltip"] == "no schema confirmed"

    def test_missing_reason_gets_a_generic_fallback(self) -> None:
        m = _model()
        badge = m.format_unsupported_badge({"status": "no_data"})
        assert badge["tooltip"]  # non-empty, never blank chrome


class TestRecordTokenMeterResult:
    def test_ok_status_populates_last_usage(self) -> None:
        m = _model()
        usage = {
            "status": "ok",
            "model": "codex",
            "prompt": 10,
            "output": 1,
            "input": 10,
            "cache_creation": 0,
            "cache_read": 0,
            "limit": 258_400,
        }
        m.record_token_meter_result(usage)
        assert m.current_usage() == usage
        assert m.last_usage_raw == usage

    def test_unsupported_status_does_not_populate_last_usage(self) -> None:
        m = _model()
        m.record_token_meter_result({"status": "unsupported", "model": None, "reason": "x"})
        # current_usage() (status-bar aggregation / session-cap watchdog) must
        # keep reading None — only "ok" data may ever reach it.
        assert m.current_usage() is None
        assert m.last_usage_raw == {"status": "unsupported", "model": None, "reason": "x"}

    def test_claude_dict_with_no_status_key_treated_as_ok(self) -> None:
        # Backward compat: a pre-#103 caller handing a bare claude usage dict
        # (no "status" key at all) must still count as "ok".
        m = _model()
        usage = {
            "model": "claude-sonnet-5",
            "prompt": 5,
            "output": 1,
            "input": 5,
            "cache_creation": 0,
            "cache_read": 0,
        }
        m.record_token_meter_result(usage)
        assert m.current_usage() == usage


class TestTokenMeterContext:
    def test_no_session_returns_none(self) -> None:
        m = AgentPaneModel(by_name("backend"))
        m.session = None
        m.record_token_meter_result(
            {
                "status": "ok",
                "model": "codex",
                "prompt": 1,
                "output": 1,
                "input": 1,
                "cache_creation": 0,
                "cache_read": 0,
                "limit": 100,
            }
        )
        assert m.token_meter_context() is None

    def test_no_reading_yet_returns_none(self) -> None:
        m = _model()
        assert m.token_meter_context() is None

    def test_ok_reading_reports_numbers_and_pct(self) -> None:
        m = _model()
        m.record_token_meter_result(
            {
                "status": "ok",
                "model": "codex",
                "prompt": 50_000,
                "output": 1,
                "input": 50_000,
                "cache_creation": 0,
                "cache_read": 0,
                "limit": 100_000,
            }
        )
        ctx = m.token_meter_context()
        assert ctx == {"prompt": 50_000, "limit": 100_000, "pct": 50, "status": "ok"}

    def test_unsupported_reading_reports_status_only(self) -> None:
        m = _model()
        m.record_token_meter_result({"status": "unsupported", "model": None, "reason": "no schema"})
        ctx = m.token_meter_context()
        assert ctx == {"prompt": None, "limit": None, "pct": None, "status": "unsupported"}
