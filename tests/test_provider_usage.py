"""Tests for `agent_takkub.provider_usage` — the cross-provider usage/quota
abstraction (#103 follow-up, Wave 2).

No test hits a real network endpoint or a real provider CLI. The codex
JSON-RPC round-trip is exercised against a small fake `codex app-server`
stand-in (a throwaway python subprocess speaking the same stdio JSON-RPC
framing) instead of mocking away the actual reader/writer/timeout logic —
that logic is exactly what needs coverage, not just the parser.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

from agent_takkub import limit_status
from agent_takkub import provider_usage as pu

# ── ProviderUsage / serialization ────────────────────────────────────────


def test_provider_usage_rejects_invalid_status():
    with pytest.raises(ValueError):
        pu.ProviderUsage(provider="claude", status="bogus")


def test_usage_to_dict_never_fabricates_zero_for_missing_fields():
    data = pu.ProviderUsage(provider="kimi", status="unsupported", error="no channel")
    out = pu.usage_to_dict(data)
    assert out["utilization"] is None
    assert out["plan"] is None
    assert out["resets_at"] is None
    assert out["fetched_at"] is None
    assert out["error"] == "no channel"


def test_usage_to_dict_serializes_datetimes_as_isoformat():
    now = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
    data = pu.ProviderUsage(
        provider="claude", status="active", utilization=7.0, resets_at=now, fetched_at=now
    )
    out = pu.usage_to_dict(data)
    assert out["resets_at"] == now.isoformat()
    assert out["fetched_at"] == now.isoformat()
    assert out["utilization"] == 7.0


# ── claude adapter ────────────────────────────────────────────────────────


class TestClaudeAdapter:
    def test_no_data_reports_error_not_zero(self, monkeypatch):
        monkeypatch.setattr(limit_status, "fetch_usage_shared", lambda config_dir=None: None)
        result = pu.fetch_claude_usage()
        assert result.status == "error"
        assert result.utilization is None

    def test_rate_limited_data_reports_stale(self, monkeypatch):
        window = limit_status.LimitWindow(
            name="five_hour", utilization=42.0, resets_at=datetime(2026, 8, 14, tzinfo=UTC)
        )
        data = limit_status.UsageData(
            plan="Max 5x",
            windows=[window],
            extra_usage_enabled=False,
            status="rate_limited",
            fetched_at=datetime(2026, 8, 13, tzinfo=UTC),
        )
        monkeypatch.setattr(limit_status, "fetch_usage_shared", lambda config_dir=None: data)
        result = pu.fetch_claude_usage()
        assert result.status == "stale"
        assert result.utilization == 42.0
        assert result.plan == "Max 5x"

    def test_active_data_maps_five_hour_window(self, monkeypatch):
        five_hour = limit_status.LimitWindow(
            name="five_hour", utilization=10.0, resets_at=datetime(2026, 8, 14, tzinfo=UTC)
        )
        seven_day = limit_status.LimitWindow(
            name="seven_day", utilization=55.0, resets_at=datetime(2026, 8, 20, tzinfo=UTC)
        )
        data = limit_status.UsageData(
            plan="Pro",
            windows=[five_hour, seven_day],
            extra_usage_enabled=True,
            fetched_at=datetime(2026, 8, 13, tzinfo=UTC),
        )
        monkeypatch.setattr(limit_status, "fetch_usage_shared", lambda config_dir=None: data)
        result = pu.fetch_claude_usage()
        assert result.status == "active"
        assert result.utilization == 10.0
        assert result.plan == "Pro"
        assert result.raw_data["extra_usage_enabled"] is True
        assert len(result.raw_data["windows"]) == 2

    def test_missing_five_hour_window_reports_none_not_zero(self, monkeypatch):
        seven_day = limit_status.LimitWindow(
            name="seven_day", utilization=55.0, resets_at=datetime(2026, 8, 20, tzinfo=UTC)
        )
        data = limit_status.UsageData(
            plan="Pro", windows=[seven_day], extra_usage_enabled=False, fetched_at=None
        )
        monkeypatch.setattr(limit_status, "fetch_usage_shared", lambda config_dir=None: data)
        result = pu.fetch_claude_usage()
        assert result.utilization is None
        assert result.resets_at is None

    def test_exception_is_caught_never_raises(self, monkeypatch):
        def _boom(config_dir=None):
            raise RuntimeError("network exploded")

        monkeypatch.setattr(limit_status, "fetch_usage_shared", _boom)
        result = pu.fetch_claude_usage()
        assert result.status == "error"


# ── codex adapter ─────────────────────────────────────────────────────────


def _spawn_fake_codex_server(script: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


_FAKE_CODEX_HAPPY_PATH = r"""
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        print(json.dumps({"id": msg["id"], "result": {"ok": True}}))
        sys.stdout.flush()
    elif method == "account/rateLimits/read":
        resp = {
            "id": msg["id"],
            "result": {
                "rateLimits": {
                    "primary": {"usedPercent": 42, "resetsAt": 1800000000, "windowDurationMins": 300},
                    "planType": "pro",
                }
            },
        }
        print(json.dumps(resp))
        sys.stdout.flush()
"""

_FAKE_CODEX_INIT_ERROR = r"""
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"id": msg["id"], "error": {"code": -1, "message": "boom"}}))
        sys.stdout.flush()
"""

_FAKE_CODEX_SILENT = r"""
import sys
for line in sys.stdin:
    pass
"""


class TestCodexAdapter:
    def test_no_binary_is_unsupported(self, monkeypatch):
        monkeypatch.setattr(pu, "_codex_executable", lambda: None)
        result = pu.fetch_codex_usage()
        assert result.status == "unsupported"

    def test_spawn_failure_is_error_not_raise(self, monkeypatch):
        monkeypatch.setattr(pu, "_codex_executable", lambda: "/definitely/not/a/real/binary")
        result = pu.fetch_codex_usage(timeout=2.0)
        assert result.status == "error"

    def test_rpc_roundtrip_parses_rate_limits(self):
        proc = _spawn_fake_codex_server(_FAKE_CODEX_HAPPY_PATH)
        try:
            result = pu._codex_rpc_roundtrip(proc, timeout=10.0)
        finally:
            pu._terminate_quietly(proc)
        assert result.status == "active"
        assert result.utilization == 42.0
        assert result.plan == "pro"
        assert result.resets_at == datetime.fromtimestamp(1800000000, tz=UTC)
        assert result.raw_data["planType"] == "pro"

    def test_rpc_roundtrip_surfaces_initialize_error(self):
        proc = _spawn_fake_codex_server(_FAKE_CODEX_INIT_ERROR)
        try:
            result = pu._codex_rpc_roundtrip(proc, timeout=10.0)
        finally:
            pu._terminate_quietly(proc)
        assert result.status == "error"
        assert "initialize error" in result.error

    def test_rpc_roundtrip_times_out_without_raising(self):
        proc = _spawn_fake_codex_server(_FAKE_CODEX_SILENT)
        try:
            result = pu._codex_rpc_roundtrip(proc, timeout=1.0)
        finally:
            pu._terminate_quietly(proc)
        assert result.status == "error"
        assert "did not respond" in result.error

    def test_parse_codex_rate_limits_handles_missing_fields(self):
        result = pu._parse_codex_rate_limits({})
        assert result.status == "active"
        assert result.utilization is None
        assert result.resets_at is None
        assert result.plan is None

    def test_parse_codex_rate_limits_ignores_malformed_used_percent(self):
        result = pu._parse_codex_rate_limits({"primary": {"usedPercent": "not-a-number"}})
        assert result.utilization is None


# ── gemini/agy adapter ────────────────────────────────────────────────────


class TestGeminiAdapter:
    def test_missing_cache_dir_is_unsupported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pu, "_antigravity_authorized_cache_dir", lambda: tmp_path / "nope")
        result = pu.fetch_gemini_usage()
        assert result.status == "unsupported"

    def test_empty_cache_dir_is_unsupported(self, monkeypatch, tmp_path):
        cache_dir = tmp_path / "authorized"
        cache_dir.mkdir()
        monkeypatch.setattr(pu, "_antigravity_authorized_cache_dir", lambda: cache_dir)
        result = pu.fetch_gemini_usage()
        assert result.status == "unsupported"

    def test_malformed_json_is_error(self, monkeypatch, tmp_path):
        cache_dir = tmp_path / "authorized"
        cache_dir.mkdir()
        (cache_dir / "acct.json").write_text("not json", encoding="utf-8")
        monkeypatch.setattr(pu, "_antigravity_authorized_cache_dir", lambda: cache_dir)
        result = pu.fetch_gemini_usage()
        assert result.status == "error"

    def test_fresh_data_is_active_with_worst_case_model(self, monkeypatch, tmp_path):
        cache_dir = tmp_path / "authorized"
        cache_dir.mkdir()
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        payload = {
            "email": "user@example.com",
            "updatedAt": now_ms,
            "payload": {
                "models": {
                    "gemini-3-flash": {
                        "quotaInfo": {
                            "remainingFraction": 0.9,
                            "resetTime": "2026-08-14T00:00:00Z",
                        }
                    },
                    "gemini-3.1-pro": {
                        "quotaInfo": {
                            "remainingFraction": 0.2,
                            "resetTime": "2026-08-15T00:00:00Z",
                        }
                    },
                }
            },
        }
        (cache_dir / "acct.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(pu, "_antigravity_authorized_cache_dir", lambda: cache_dir)
        result = pu.fetch_gemini_usage()
        assert result.status == "active"
        # worst-case model (lowest remaining fraction 0.2) drives the meter
        assert result.utilization == pytest.approx(80.0)
        assert result.raw_data["model_count"] == 2

    def test_stale_data_is_flagged_stale_not_active(self, monkeypatch, tmp_path):
        cache_dir = tmp_path / "authorized"
        cache_dir.mkdir()
        old_ms = int((datetime.now(tz=UTC) - timedelta(days=180)).timestamp() * 1000)
        payload = {
            "email": "user@example.com",
            "updatedAt": old_ms,
            "payload": {
                "models": {
                    "gemini-3-flash": {"quotaInfo": {"remainingFraction": 1.0}},
                }
            },
        }
        (cache_dir / "acct.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(pu, "_antigravity_authorized_cache_dir", lambda: cache_dir)
        result = pu.fetch_gemini_usage()
        assert result.status == "stale"

    def test_picks_newest_file_when_multiple_accounts_cached(self, monkeypatch, tmp_path):
        cache_dir = tmp_path / "authorized"
        cache_dir.mkdir()
        now = datetime.now(tz=UTC)
        old_payload = {"email": "old@example.com", "updatedAt": int(now.timestamp() * 1000)}
        new_payload = {"email": "new@example.com", "updatedAt": int(now.timestamp() * 1000)}
        old_file = cache_dir / "old.json"
        new_file = cache_dir / "new.json"
        old_file.write_text(json.dumps(old_payload), encoding="utf-8")
        time.sleep(0.02)
        new_file.write_text(json.dumps(new_payload), encoding="utf-8")
        monkeypatch.setattr(pu, "_antigravity_authorized_cache_dir", lambda: cache_dir)
        result = pu.fetch_gemini_usage()
        assert result.raw_data["email"] == "new@example.com"


# ── opencode adapter ──────────────────────────────────────────────────────


class TestOpencodeAdapter:
    def test_no_binary_is_unsupported(self, monkeypatch):
        monkeypatch.setattr(pu, "_opencode_executable", lambda: None)
        result = pu.fetch_opencode_usage()
        assert result.status == "unsupported"

    def test_never_populates_utilization(self, monkeypatch):
        """opencode's self-tallied spend must never masquerade as a quota %."""
        monkeypatch.setattr(pu, "_opencode_executable", lambda: "opencode")
        row = [
            {
                "cost": 1.23,
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 10,
                "cache_write_tokens": 0,
                "message_count": 5,
            }
        ]

        def _fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(row), stderr="")

        monkeypatch.setattr(pu.subprocess, "run", _fake_run)
        result = pu.fetch_opencode_usage()
        assert result.status == "active"
        assert result.utilization is None
        assert result.plan is None
        assert result.spend["cost_usd"] == 1.23
        assert result.spend["message_count"] == 5

    def test_nonzero_returncode_is_error(self, monkeypatch):
        monkeypatch.setattr(pu, "_opencode_executable", lambda: "opencode")

        def _fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="db locked")

        monkeypatch.setattr(pu.subprocess, "run", _fake_run)
        result = pu.fetch_opencode_usage()
        assert result.status == "error"

    def test_unparsable_output_is_error_not_raise(self, monkeypatch):
        monkeypatch.setattr(pu, "_opencode_executable", lambda: "opencode")

        def _fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="not json", stderr="")

        monkeypatch.setattr(pu.subprocess, "run", _fake_run)
        result = pu.fetch_opencode_usage()
        assert result.status == "error"

    def test_subprocess_timeout_is_error_not_raise(self, monkeypatch):
        monkeypatch.setattr(pu, "_opencode_executable", lambda: "opencode")

        def _fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="opencode", timeout=10)

        monkeypatch.setattr(pu.subprocess, "run", _fake_run)
        result = pu.fetch_opencode_usage()
        assert result.status == "error"


# ── kimi / cursor (statically unsupported) ────────────────────────────────


def test_kimi_is_always_unsupported():
    result = pu.fetch_kimi_usage()
    assert result.status == "unsupported"
    assert result.utilization is None


class TestCursorAdapter:
    def test_not_installed_reports_not_installed_reason(self, monkeypatch):
        monkeypatch.setattr(pu.shutil, "which", lambda name: None)
        result = pu.fetch_cursor_usage()
        assert result.status == "unsupported"
        assert "not installed" in result.error

    def test_installed_but_unverified_reports_distinct_reason(self, monkeypatch):
        monkeypatch.setattr(
            pu.shutil,
            "which",
            lambda name: "/usr/local/bin/cursor-agent" if name == "cursor-agent" else None,
        )
        result = pu.fetch_cursor_usage()
        assert result.status == "unsupported"
        assert "not yet verified" in result.error


# ── dispatch ──────────────────────────────────────────────────────────────


class TestFetchProviderUsage:
    def test_unknown_provider_is_unsupported(self):
        result = pu.fetch_provider_usage("nonexistent")
        assert result.status == "unsupported"

    def test_fetcher_exception_becomes_error_not_raise(self, monkeypatch):
        def _boom():
            raise RuntimeError("adapter exploded")

        monkeypatch.setitem(pu._FETCHERS, "claude", _boom)
        result = pu.fetch_provider_usage("claude")
        assert result.status == "error"

    def test_all_registered_providers_are_dispatchable(self):
        for name in pu.PROVIDER_NAMES:
            assert name in pu._FETCHERS


# ── ProviderUsageStore ────────────────────────────────────────────────────


class TestProviderUsageStore:
    def test_fetch_one_populates_cache(self, monkeypatch):
        monkeypatch.setattr(
            pu,
            "fetch_provider_usage",
            lambda name: pu.ProviderUsage(provider=name, status="active"),
        )
        store = pu.ProviderUsageStore()
        store._fetch_one("claude")
        cached = store.get("claude")
        assert cached is not None
        assert cached.status == "active"

    def test_fetch_one_emits_loading_then_final_for_first_fetch(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(
            pu,
            "fetch_provider_usage",
            lambda name: pu.ProviderUsage(provider=name, status="active"),
        )
        store = pu.ProviderUsageStore(on_update=lambda name, data: seen.append(data.status))
        store._fetch_one("codex")
        assert seen == ["loading", "active"]

    def test_get_all_returns_a_copy(self, monkeypatch):
        monkeypatch.setattr(
            pu,
            "fetch_provider_usage",
            lambda name: pu.ProviderUsage(provider=name, status="active"),
        )
        store = pu.ProviderUsageStore()
        store._fetch_one("claude")
        snapshot = store.get_all()
        snapshot["claude"] = None
        assert store.get("claude") is not None

    def test_get_store_returns_singleton(self, monkeypatch):
        monkeypatch.setattr(pu, "_store", None)
        monkeypatch.setattr(pu.ProviderUsageStore, "start", lambda self: None)
        first = pu.get_store()
        second = pu.get_store()
        assert first is second
        monkeypatch.setattr(pu, "_store", None)
