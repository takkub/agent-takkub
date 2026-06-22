from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_takkub import limit_status
from agent_takkub.limit_status import LimitStore, UsageData, _KeychainSink, _load_credentials


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def test_fetch_usage_returns_three_windows_from_config_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "other-user" / ".claude"
    config_dir.mkdir(parents=True)
    expires_at_ms = int(datetime(2099, 1, 1, tzinfo=UTC).timestamp() * 1000)
    (config_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "override-token",
                    "expiresAt": expires_at_ms,
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                }
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "five_hour": {"utilization": 12.5, "resets_at": "2026-06-09T10:00:00+00:00"},
        "seven_day": {"utilization": 34, "resets_at": "2026-06-15T10:00:00+00:00"},
        "seven_day_sonnet": {
            "utilization": 56.75,
            "resets_at": "2026-06-16T10:00:00+00:00",
        },
        "extra_usage": {"is_enabled": True},
    }
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return _FakeResponse(payload)

    monkeypatch.setattr(limit_status.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(limit_status, "_resolve_user_agent", lambda: "test-agent")

    usage = limit_status.fetch_usage(config_dir)

    assert usage is not None
    assert usage.plan == "Max 20x"
    assert usage.extra_usage_enabled is True
    assert [(window.name, window.utilization) for window in usage.windows] == [
        ("five_hour", 12.5),
        ("seven_day", 34.0),
        ("seven_day_sonnet", 56.75),
    ]
    assert [window.resets_at.isoformat() for window in usage.windows] == [
        "2026-06-09T10:00:00+00:00",
        "2026-06-15T10:00:00+00:00",
        "2026-06-16T10:00:00+00:00",
    ]
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == limit_status._USAGE_URL
    assert request.get_header("Authorization") == "Bearer override-token"
    assert timeout == limit_status._TIMEOUT_S


# ---------------------------------------------------------------------------
# LimitStore tests
# ---------------------------------------------------------------------------


def _fake_usage() -> UsageData:
    return UsageData(plan="Max", windows=[], extra_usage_enabled=False)


class TestLimitStore:
    def test_dedup_same_config_dir_two_registers_one_poll(self, tmp_path: Path) -> None:
        """Two register() calls on the same resolved config_dir trigger only 1 fetch."""
        cd = tmp_path / ".claude"
        cd.mkdir()

        fetch_calls: list = []
        fetched = threading.Event()

        def fake_fetch(cfg=None):
            fetch_calls.append(cfg)
            fetched.set()
            return _fake_usage()

        store = LimitStore(interval_s=3600)

        with patch("agent_takkub.limit_status.fetch_usage", side_effect=fake_fetch):
            store.register(cd)
            fetched.wait(timeout=2.0)
            count_after_first = len(fetch_calls)

            store.register(cd)  # same dir, second register — no new fetch
            time.sleep(0.1)

        assert count_after_first == 1
        assert len(fetch_calls) == 1, "second register on same dir must not trigger another fetch"

    def test_ref_count_unregister_to_zero_clears_cache(self, tmp_path: Path) -> None:
        """Unregistering the last ref clears the cache; get() returns None."""
        cd = tmp_path / ".claude"
        cd.mkdir()

        fetched = threading.Event()

        def fake_fetch(cfg=None):
            fetched.set()
            return _fake_usage()

        store = LimitStore(interval_s=3600)

        with patch("agent_takkub.limit_status.fetch_usage", side_effect=fake_fetch):
            store.register(cd)
            fetched.wait(timeout=2.0)

        assert store.get(cd) is not None, "cache should hold data after fetch"
        store.unregister(cd)
        assert store.get(cd) is None, "cache must be cleared when ref-count reaches 0"

    def test_register_new_fires_immediate_fetch(self, tmp_path: Path) -> None:
        """First register() of a new config_dir triggers an immediate background fetch."""
        cd = tmp_path / ".claude"
        cd.mkdir()

        fired = threading.Event()

        def fake_fetch(cfg=None):
            fired.set()
            return _fake_usage()

        store = LimitStore(interval_s=3600)

        with patch("agent_takkub.limit_status.fetch_usage", side_effect=fake_fetch):
            store.register(cd)
            result = fired.wait(timeout=2.0)

        assert result, "fetch_usage should fire immediately on first register"

    def test_get_reads_cache_only_no_fetch(self, tmp_path: Path) -> None:
        """get() never calls fetch_usage — pure cache read."""
        cd = tmp_path / ".claude"
        cd.mkdir()

        store = LimitStore(interval_s=3600)

        with patch("agent_takkub.limit_status.fetch_usage") as mock_fetch:
            result = store.get(cd)

        assert result is None
        mock_fetch.assert_not_called()

    def test_switch_does_not_fire_api(self, tmp_path: Path) -> None:
        """After the initial fetch, repeated get() calls do not call fetch_usage."""
        cd = tmp_path / ".claude"
        cd.mkdir()

        fetch_count = 0
        fetched = threading.Event()

        def fake_fetch(cfg=None):
            nonlocal fetch_count
            fetch_count += 1
            fetched.set()
            return _fake_usage()

        store = LimitStore(interval_s=3600)

        with patch("agent_takkub.limit_status.fetch_usage", side_effect=fake_fetch):
            store.register(cd)
            fetched.wait(timeout=2.0)
            count_after_register = fetch_count

            # Simulate multiple tab switches
            for _ in range(5):
                store.get(cd)

        assert fetch_count == count_after_register, "get() must not trigger any additional fetches"


# ---------------------------------------------------------------------------
# _load_credentials tests
# ---------------------------------------------------------------------------


def test_load_credentials_reads_file_when_present(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    cred = {"claudeAiOauth": {"accessToken": "tok", "expiresAt": 9_999_999_999_000}}
    (config_dir / ".credentials.json").write_text(json.dumps(cred), encoding="utf-8")

    raw, sink = _load_credentials(config_dir)

    assert raw == cred
    assert sink == config_dir / ".credentials.json"


def test_load_credentials_returns_none_on_missing_file_non_darwin(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    monkeypatch.setattr(limit_status.sys, "platform", "linux")

    raw, sink = _load_credentials(config_dir)

    assert raw is None
    assert isinstance(sink, Path)


def test_load_credentials_keychain_fallback_on_darwin(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()  # no .credentials.json inside
    monkeypatch.setattr(limit_status.sys, "platform", "darwin")
    monkeypatch.setattr(limit_status.getpass, "getuser", lambda: "testuser")

    cred = {"claudeAiOauth": {"accessToken": "kc-tok", "expiresAt": 9_999_999_999_000}}

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = json.dumps(cred) + "\n"

    with patch("agent_takkub.limit_status.subprocess.run", return_value=fake_result) as mock_run:
        raw, sink = _load_credentials(config_dir)

    assert raw == cred
    assert isinstance(sink, _KeychainSink)
    assert sink.service == limit_status._KEYCHAIN_SERVICE
    assert sink.account == "testuser"
    call_args = mock_run.call_args[0][0]
    assert "find-generic-password" in call_args
    assert limit_status._KEYCHAIN_SERVICE in call_args


def test_load_credentials_keychain_not_found_returns_none(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    monkeypatch.setattr(limit_status.sys, "platform", "darwin")
    monkeypatch.setattr(limit_status.getpass, "getuser", lambda: "testuser")

    fake_result = MagicMock()
    fake_result.returncode = 44  # security exit code when not found
    fake_result.stdout = ""

    with patch("agent_takkub.limit_status.subprocess.run", return_value=fake_result):
        raw, sink = _load_credentials(config_dir)

    assert raw is None
    assert isinstance(sink, _KeychainSink)


def test_fetch_usage_uses_keychain_on_darwin(tmp_path: Path, monkeypatch) -> None:
    """fetch_usage returns UsageData when credentials come from macOS Keychain."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()  # no credentials file
    monkeypatch.setattr(limit_status.sys, "platform", "darwin")
    monkeypatch.setattr(limit_status.getpass, "getuser", lambda: "testuser")

    expires_at_ms = int(datetime(2099, 1, 1, tzinfo=UTC).timestamp() * 1000)
    cred = {
        "claudeAiOauth": {
            "accessToken": "kc-access-tok",
            "expiresAt": expires_at_ms,
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_5x",
        }
    }
    fake_security = MagicMock()
    fake_security.returncode = 0
    fake_security.stdout = json.dumps(cred) + "\n"

    payload = {
        "five_hour": {"utilization": 10.0, "resets_at": "2099-01-01T00:00:00+00:00"},
        "extra_usage": {"is_enabled": False},
    }

    def fake_urlopen(request, timeout):
        return _FakeResponse(payload)

    with patch("agent_takkub.limit_status.subprocess.run", return_value=fake_security):
        monkeypatch.setattr(limit_status.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(limit_status, "_resolve_user_agent", lambda: "test-agent")
        usage = limit_status.fetch_usage(config_dir)

    assert usage is not None
    assert usage.plan == "Max 5x"
    assert len(usage.windows) == 1
    assert usage.windows[0].name == "five_hour"


def test_write_refreshed_token_keychain_sink(monkeypatch) -> None:
    """_write_refreshed_token calls `security add-generic-password` for keychain sink."""
    sink = _KeychainSink(service="Claude Code-credentials", account="bob")
    raw = {"claudeAiOauth": {"accessToken": "old", "expiresAt": 0}}

    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        return result

    with patch("agent_takkub.limit_status.subprocess.run", side_effect=fake_run):
        limit_status._write_refreshed_token(sink, raw, "new-tok", None, 3600)

    assert any("add-generic-password" in " ".join(c) for c in calls)
    written_json = next(
        calls[i][calls[i].index("-w") + 1]
        for i in range(len(calls))
        if "-w" in calls[i]
    )
    written = json.loads(written_json)
    assert written["claudeAiOauth"]["accessToken"] == "new-tok"


def test_write_refreshed_token_file_sink(tmp_path: Path) -> None:
    """_write_refreshed_token writes to file when sink is a Path (no regression)."""
    cred_file = tmp_path / ".credentials.json"
    raw = {"claudeAiOauth": {"accessToken": "old", "expiresAt": 0}}
    cred_file.write_text(json.dumps(raw), encoding="utf-8")

    limit_status._write_refreshed_token(cred_file, raw, "new-file-tok", "rt", 3600)

    written = json.loads(cred_file.read_text(encoding="utf-8"))
    assert written["claudeAiOauth"]["accessToken"] == "new-file-tok"
    assert written["claudeAiOauth"]["refreshToken"] == "rt"
