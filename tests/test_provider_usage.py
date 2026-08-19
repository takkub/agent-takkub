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
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


class TestClaudeConfigDirResolution:
    """#203: the fetch must never fall back to `limit_status`'s own
    ~/.claude default — it must resolve THIS cockpit instance's real
    profile dir, the same one panes actually run under."""

    def test_fetcher_is_called_with_resolved_cockpit_config_dir_not_none(
        self, monkeypatch, tmp_path
    ):
        resolved = tmp_path / "agent-takkub-claude-config"
        monkeypatch.setattr(pu, "_resolve_claude_config_dir", lambda: resolved)
        seen: list[Path | None] = []

        def fake_fetch_usage_shared(config_dir=None):
            seen.append(config_dir)
            return None

        monkeypatch.setattr(limit_status, "fetch_usage_shared", fake_fetch_usage_shared)
        pu.fetch_claude_usage()
        assert seen == [resolved]

    def test_resolves_via_active_project_profile_not_hardcoded_home_claude(
        self, monkeypatch, tmp_path
    ):
        from agent_takkub import config as cockpit_config
        from agent_takkub import user_profile

        profile_dir = tmp_path / "agent-takkub" / "claude-config"
        monkeypatch.setattr(cockpit_config, "active_project", lambda: ("myproject", {}))
        monkeypatch.setattr(user_profile, "config_dir_for", lambda project: profile_dir)
        result = pu._resolve_claude_config_dir()
        assert result == profile_dir
        assert result != Path.home() / ".claude"

    def test_falls_back_to_default_profile_when_no_active_project(self, monkeypatch):
        from agent_takkub import config as cockpit_config

        monkeypatch.setattr(cockpit_config, "active_project", lambda: (None, {}))
        # Must not raise even with no active project tab yet (e.g. a mobile
        # poll racing cockpit boot).
        result = pu._resolve_claude_config_dir()
        assert isinstance(result, Path)


class TestClaudeAdapter:
    def test_no_data_reports_error_not_zero(self, monkeypatch):
        monkeypatch.setattr(limit_status, "fetch_usage_shared", lambda config_dir=None: None)
        result = pu.fetch_claude_usage()
        assert result.status == "error"
        assert result.utilization is None

    def test_stale_by_age_even_when_status_ok(self, monkeypatch):
        """#203: a wrong-directory read is neither rate_limited nor an HTTP
        error — only an age check catches data that stopped updating."""
        five_hour = limit_status.LimitWindow(
            name="five_hour", utilization=7.0, resets_at=datetime(2026, 8, 14, tzinfo=UTC)
        )
        data = limit_status.UsageData(
            plan="Max 5x",
            windows=[five_hour],
            extra_usage_enabled=False,
            status="ok",
            fetched_at=datetime.now(tz=UTC) - timedelta(days=23),
        )
        monkeypatch.setattr(limit_status, "fetch_usage_shared", lambda config_dir=None: data)
        result = pu.fetch_claude_usage()
        assert result.status == "stale"

    def test_missing_fetched_at_is_stale_not_active(self, monkeypatch):
        five_hour = limit_status.LimitWindow(
            name="five_hour", utilization=7.0, resets_at=datetime(2026, 8, 14, tzinfo=UTC)
        )
        data = limit_status.UsageData(
            plan="Max 5x",
            windows=[five_hour],
            extra_usage_enabled=False,
            status="ok",
            fetched_at=None,
        )
        monkeypatch.setattr(limit_status, "fetch_usage_shared", lambda config_dir=None: data)
        result = pu.fetch_claude_usage()
        assert result.status == "stale"

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
            fetched_at=datetime.now(tz=UTC),
        )
        monkeypatch.setattr(limit_status, "fetch_usage_shared", lambda config_dir=None: data)
        result = pu.fetch_claude_usage()
        assert result.status == "active"
        assert result.utilization == 10.0
        assert result.plan == "Pro"
        assert result.raw_data["extra_usage_enabled"] is True
        assert len(result.raw_data["windows"]) == 2
        assert len(result.windows) == 2
        assert result.windows[1]["name"] == "seven_day"
        assert result.windows[1]["utilization"] == 55.0

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

    def test_parse_codex_rate_limits_surfaces_secondary_window(self):
        """#204: the weekly `secondary` window was fetched but silently
        dropped — it must show up in `windows` alongside `primary`."""
        result = pu._parse_codex_rate_limits(
            {
                "primary": {"usedPercent": 42, "resetsAt": 1800000000},
                "secondary": {"usedPercent": 18, "resetsAt": 1800600000},
                "planType": "pro",
            }
        )
        assert result.windows is not None
        names = [w["name"] for w in result.windows]
        assert names == ["primary", "secondary"]
        secondary = result.windows[1]
        assert secondary["utilization"] == 18.0

    def test_parse_codex_rate_limits_no_secondary_leaves_single_window(self):
        result = pu._parse_codex_rate_limits({"primary": {"usedPercent": 42}})
        assert result.windows is not None
        assert [w["name"] for w in result.windows] == ["primary"]

    def test_parse_codex_rate_limits_no_data_leaves_windows_none_not_empty_rows(self):
        result = pu._parse_codex_rate_limits({})
        assert result.windows is None

    def test_roundtrip_exception_is_caught_never_raises(self, monkeypatch):
        monkeypatch.setattr(pu, "_codex_executable", lambda: sys.executable)

        def _boom(proc, timeout):
            raise RuntimeError("stdin pipe exploded")

        monkeypatch.setattr(pu, "_codex_rpc_roundtrip", _boom)
        result = pu.fetch_codex_usage(timeout=2.0)
        assert result.status == "error"
        assert result.error == "app-server RPC failed"


class TestCodexAdapterConfigDir:
    """epic #309 Phase 3b: `config_dir` scopes the app-server probe to one
    account's isolated CODEX_HOME instead of whatever this process inherited."""

    def _capture_popen_env(self, monkeypatch):
        captured: dict = {}

        def fake_popen(argv, **kwargs):
            captured["env"] = kwargs.get("env")
            raise OSError("stop before actually spawning — env capture only")

        monkeypatch.setattr(pu, "_codex_executable", lambda: "/fake/codex")
        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        return captured

    def test_config_dir_sets_codex_home_in_subprocess_env(self, monkeypatch, tmp_path):
        captured = self._capture_popen_env(monkeypatch)
        result = pu.fetch_codex_usage(config_dir=tmp_path)
        assert result.status == "error"
        assert captured["env"]["CODEX_HOME"] == str(tmp_path)

    def test_no_config_dir_inherits_process_env(self, monkeypatch):
        captured = self._capture_popen_env(monkeypatch)
        pu.fetch_codex_usage()
        assert captured["env"] is None


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
        now = datetime.now(tz=UTC)
        now_ms = int(now.timestamp() * 1000)
        future_iso = (now + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        payload = {
            "email": "user@example.com",
            "updatedAt": now_ms,
            "payload": {
                "models": {
                    "gemini-3-flash": {
                        "quotaInfo": {
                            "remainingFraction": 0.9,
                            "resetTime": future_iso,
                        }
                    },
                    "gemini-3.1-pro": {
                        "quotaInfo": {
                            "remainingFraction": 0.2,
                            "resetTime": future_iso,
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
        assert result.windows is not None
        assert len(result.windows) == 2

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
        assert result.utilization is None

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

    def test_emit_swallows_on_update_exception(self):
        def _boom(name, data):
            raise RuntimeError("listener blew up")

        store = pu.ProviderUsageStore(on_update=_boom)
        # Must not raise even though the callback does.
        store._emit("claude", pu.ProviderUsage(provider="claude", status="active"))

    def test_start_spawns_daemon_thread_running_the_loop(self, monkeypatch):
        ran = threading.Event()
        store = pu.ProviderUsageStore(interval_s=999)
        monkeypatch.setattr(store, "_loop", ran.set)
        store.start()
        assert ran.wait(timeout=2.0), "start() did not invoke _loop on a background thread"
        assert store._running is True

    def test_refresh_now_fetches_off_the_calling_thread(self, monkeypatch):
        seen_thread: list[str] = []
        done = threading.Event()

        def fake_fetch_one(provider):
            seen_thread.append(threading.current_thread().name)
            done.set()

        store = pu.ProviderUsageStore()
        monkeypatch.setattr(store, "_fetch_one", fake_fetch_one)
        store.refresh_now("claude")
        assert done.wait(timeout=2.0), "refresh_now() never triggered a fetch"
        assert seen_thread == ["usage-fetch-claude"]

    # ── per-account (config_dir-scoped) cache — epic #309 Phase 3b ────────

    def test_get_with_no_config_dir_reads_provider_level_cache_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            pu,
            "fetch_provider_usage",
            lambda name, config_dir=None: pu.ProviderUsage(provider=name, status="active"),
        )
        store = pu.ProviderUsageStore()
        store._fetch_one("claude")
        assert store.get("claude") is not None
        assert store.get("claude", config_dir="/some/dir") is None

    def test_fetch_one_with_config_dir_populates_account_scoped_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            pu,
            "fetch_provider_usage",
            lambda name, config_dir=None: pu.ProviderUsage(
                provider=name, status="active", utilization=33.0
            ),
        )
        store = pu.ProviderUsageStore()
        store._fetch_one("claude", tmp_path)
        cached = store.get("claude", config_dir=tmp_path)
        assert cached is not None
        assert cached.utilization == 33.0
        # provider-level (no config_dir) cache stays untouched.
        assert store.get("claude") is None

    def test_refresh_now_with_config_dir_passes_it_through(self, tmp_path, monkeypatch):
        seen: list[tuple] = []
        done = threading.Event()

        def fake_fetch_one(provider, config_dir=None):
            seen.append((provider, config_dir))
            done.set()

        store = pu.ProviderUsageStore()
        monkeypatch.setattr(store, "_fetch_one", fake_fetch_one)
        store.refresh_now("codex", config_dir=tmp_path)
        assert done.wait(timeout=2.0), "refresh_now() never triggered a fetch"
        assert seen == [("codex", tmp_path)]


class TestFetchProviderUsageConfigDir:
    """`fetch_provider_usage(provider, config_dir=...)` dispatch — epic #309
    Phase 3b."""

    def test_claude_config_dir_forwarded_to_fetch_claude_usage(self, monkeypatch, tmp_path):
        seen = {}

        def fake_fetch_claude(config_dir=None):
            seen["config_dir"] = config_dir
            return pu.ProviderUsage(provider="claude", status="active")

        monkeypatch.setitem(pu._CONFIG_DIR_AWARE_FETCHERS, "claude", fake_fetch_claude)
        result = pu.fetch_provider_usage("claude", config_dir=tmp_path)
        assert result.status == "active"
        assert seen["config_dir"] == tmp_path

    def test_provider_with_no_isolation_knob_ignores_config_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            pu, "fetch_gemini_usage", lambda: pu.ProviderUsage(provider="gemini", status="active")
        )
        monkeypatch.setitem(pu._FETCHERS, "gemini", pu.fetch_gemini_usage)
        result = pu.fetch_provider_usage("gemini", config_dir=tmp_path)
        assert result.status == "active"


# ── ProviderUsageStore._loop ──────────────────────────────────────────────
#
# Every test here drives `_loop()` synchronously on the calling thread (no
# real background thread, no real `time.sleep`/`Event.wait` delay) by
# monkeypatching `_wake.wait` itself: the fake stands in for "an interval
# elapsed or refresh_now() woke us early" and flips `_running` off once the
# test has observed enough cycles, so the loop always terminates
# deterministically and fast.


class TestProviderUsageLoop:
    def test_initial_pass_fetches_every_provider_once_in_order(self, monkeypatch):
        store = pu.ProviderUsageStore(interval_s=999)
        calls: list[str] = []
        monkeypatch.setattr(store, "_fetch_one", lambda p: calls.append(p))

        def fake_wait(timeout=None):
            store._running = False
            return False

        monkeypatch.setattr(store._wake, "wait", fake_wait)
        store._running = True
        store._loop()
        assert calls == list(pu.PROVIDER_NAMES)

    def test_wait_blocks_for_the_configured_interval_not_a_busy_spin(self, monkeypatch):
        store = pu.ProviderUsageStore(interval_s=42)
        monkeypatch.setattr(store, "_fetch_one", lambda p: None)
        seen_timeouts: list[float] = []

        def fake_wait(timeout=None):
            seen_timeouts.append(timeout)
            store._running = False
            return False

        monkeypatch.setattr(store._wake, "wait", fake_wait)
        store._running = True
        store._loop()
        assert seen_timeouts == [42]

    def test_wake_event_is_cleared_every_cycle_so_it_cannot_fire_stale(self, monkeypatch):
        store = pu.ProviderUsageStore(interval_s=0.01)
        monkeypatch.setattr(store, "_fetch_one", lambda p: None)
        clear_calls = {"n": 0}
        orig_clear = store._wake.clear

        def spy_clear():
            clear_calls["n"] += 1
            orig_clear()

        monkeypatch.setattr(store._wake, "clear", spy_clear)
        iterations = {"n": 0}

        def fake_wait(timeout=None):
            iterations["n"] += 1
            if iterations["n"] >= 2:
                store._running = False
            return False

        monkeypatch.setattr(store._wake, "wait", fake_wait)
        store._running = True
        store._loop()
        assert clear_calls["n"] == 2

    def test_stop_before_loop_starts_exits_without_any_fetch(self, monkeypatch):
        store = pu.ProviderUsageStore(interval_s=999)
        calls: list[str] = []
        monkeypatch.setattr(store, "_fetch_one", lambda p: calls.append(p))
        store.stop()
        store._loop()
        assert calls == []

    def test_stop_mid_initial_pass_halts_remaining_fetches(self, monkeypatch):
        store = pu.ProviderUsageStore(interval_s=999)
        calls: list[str] = []

        def fake_fetch_one(provider):
            calls.append(provider)
            if provider == pu.PROVIDER_NAMES[1]:
                store._running = False

        monkeypatch.setattr(store, "_fetch_one", fake_fetch_one)
        store._running = True
        store._loop()
        assert calls == list(pu.PROVIDER_NAMES[:2])

    def test_repoll_cycle_skips_a_provider_cached_unsupported(self, monkeypatch):
        store = pu.ProviderUsageStore(interval_s=0.01)
        phase = {"value": "initial"}
        initial_round: list[str] = []
        repoll_round: list[str] = []

        def fake_fetch_one(provider):
            if phase["value"] == "initial":
                initial_round.append(provider)
                if provider == "codex":
                    store._cache["codex"] = pu.ProviderUsage(
                        provider="codex", status=pu.STATUS_UNSUPPORTED
                    )
            else:
                repoll_round.append(provider)

        monkeypatch.setattr(store, "_fetch_one", fake_fetch_one)
        waits = {"n": 0}

        def fake_wait(timeout=None):
            waits["n"] += 1
            phase["value"] = "repoll"
            if waits["n"] >= 2:
                store._running = False
            return False

        monkeypatch.setattr(store._wake, "wait", fake_wait)
        store._running = True
        store._loop()

        assert "codex" in initial_round
        assert "codex" not in repoll_round
        assert set(repoll_round) == set(pu.PROVIDER_NAMES) - {"codex"}

    def test_unsupported_provider_is_never_reprobed_across_many_cycles(self, monkeypatch):
        """Documents the class docstring's intentional contract: an
        `unsupported` verdict is treated as static per machine and is never
        automatically re-fetched, no matter how many interval cycles pass —
        only an explicit `refresh_now()` call bypasses the cache check. If
        this ever starts failing because `_loop` began re-probing, that is a
        deliberate behavior change, not a bug to silently "fix" here.
        """
        store = pu.ProviderUsageStore(interval_s=0.01)
        store._cache["codex"] = pu.ProviderUsage(provider="codex", status=pu.STATUS_UNSUPPORTED)
        calls: list[str] = []
        monkeypatch.setattr(store, "_fetch_one", lambda p: calls.append(p))
        waits = {"n": 0}

        def fake_wait(timeout=None):
            waits["n"] += 1
            if waits["n"] >= 4:
                store._running = False
            return False

        monkeypatch.setattr(store._wake, "wait", fake_wait)
        store._running = True
        store._loop()
        # Fetched exactly once: the unconditional initial pass. None of the
        # 4 repoll cycles that followed touched it again.
        assert calls.count("codex") == 1

    def test_stop_during_repoll_cycle_halts_remaining_fetches(self, monkeypatch):
        # Prove the `if not self._running: return` guard inside the repoll
        # for-loop itself (not just the outer while) actually stops a cycle
        # partway through once something flips `_running` off mid-fetch.
        store = pu.ProviderUsageStore(interval_s=0.01)
        calls: list[str] = []

        def fake_fetch_one(provider):
            calls.append(provider)
            if len(calls) == len(pu.PROVIDER_NAMES) + 2:
                store._running = False

        monkeypatch.setattr(store, "_fetch_one", fake_fetch_one)
        monkeypatch.setattr(store._wake, "wait", lambda timeout=None: False)
        store._running = True
        store._loop()
        # Initial pass (6) + 2 providers into the first repoll cycle, then
        # the inner-loop guard stopped it before the remaining 4.
        assert calls == list(pu.PROVIDER_NAMES) + list(pu.PROVIDER_NAMES[:2])
