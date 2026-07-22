from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_takkub import limit_status
from agent_takkub.limit_status import LimitStore, UsageData


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
# credential loading: file (Windows/Linux) vs macOS Keychain fallback
# ---------------------------------------------------------------------------


def test_load_raw_credentials_prefers_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"access_token": "from-file"}), encoding="utf-8")
    # Keychain must not even be consulted when the file is present.
    monkeypatch.setattr(
        limit_status, "_read_keychain_credentials", lambda: pytest.fail("should not read")
    )
    raw, source = limit_status._load_raw_credentials(path)
    assert source == "file"
    assert raw == {"access_token": "from-file"}


def test_load_raw_credentials_keychain_fallback(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "nope" / ".credentials.json"  # does not exist (the Mac case)
    monkeypatch.setattr(
        limit_status,
        "_read_keychain_credentials",
        lambda: json.dumps({"claudeAiOauth": {"accessToken": "from-keychain"}}),
    )
    raw, source = limit_status._load_raw_credentials(missing)
    assert source == "keychain"
    assert raw["claudeAiOauth"]["accessToken"] == "from-keychain"


def test_tokenless_file_falls_through_to_keychain(tmp_path: Path, monkeypatch) -> None:
    """The remaining Mac gap: a `.credentials.json` stub exists but holds no
    access token (migration residue). It must NOT shadow the Keychain — the
    meter should still read the real token from the Keychain."""
    stub = tmp_path / ".credentials.json"
    stub.write_text(json.dumps({"some": "other-setting"}), encoding="utf-8")
    monkeypatch.setattr(
        limit_status,
        "_read_keychain_credentials",
        lambda: json.dumps({"claudeAiOauth": {"accessToken": "from-keychain"}}),
    )
    raw, source = limit_status._load_raw_credentials(stub)
    assert source == "keychain"
    assert raw["claudeAiOauth"]["accessToken"] == "from-keychain"


def test_tokenless_file_no_keychain_returns_file(tmp_path: Path, monkeypatch) -> None:
    """Tokenless file + no Keychain entry → return the file blob (source
    'file') so the caller logs a consistent 'no access token', not 'no creds'."""
    stub = tmp_path / ".credentials.json"
    stub.write_text(json.dumps({"some": "other-setting"}), encoding="utf-8")
    monkeypatch.setattr(limit_status, "_read_keychain_credentials", lambda: None)
    raw, source = limit_status._load_raw_credentials(stub)
    assert source == "file"
    assert raw == {"some": "other-setting"}


def test_load_raw_credentials_none(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "nope" / ".credentials.json"
    monkeypatch.setattr(limit_status, "_read_keychain_credentials", lambda: None)
    raw, source = limit_status._load_raw_credentials(missing)
    assert raw is None
    assert source == "none"


def test_read_keychain_is_noop_off_mac(monkeypatch) -> None:
    monkeypatch.setattr(limit_status.sys, "platform", "win32")
    assert limit_status._read_keychain_credentials() is None


def test_fetch_usage_reads_keychain_when_file_absent(tmp_path: Path, monkeypatch) -> None:
    """The actual Mac fix: no credentials file, token lives in the Keychain →
    usage still fetches instead of showing '—'."""
    config_dir = tmp_path / ".claude"  # note: NOT created → no .credentials.json
    expires_at_ms = int(datetime(2099, 1, 1, tzinfo=UTC).timestamp() * 1000)
    monkeypatch.setattr(
        limit_status,
        "_read_keychain_credentials",
        lambda: json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "keychain-token",
                    "expiresAt": expires_at_ms,
                    "subscriptionType": "max",
                }
            }
        ),
    )
    payload = {"five_hour": {"utilization": 9.0, "resets_at": "2026-06-09T10:00:00+00:00"}}
    captured = []

    def fake_urlopen(request, timeout):
        captured.append(request)
        return _FakeResponse(payload)

    monkeypatch.setattr(limit_status.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(limit_status, "_resolve_user_agent", lambda: "test-agent")

    usage = limit_status.fetch_usage(config_dir)
    assert usage is not None
    assert [w.name for w in usage.windows] == ["five_hour"]
    assert captured[0].get_header("Authorization") == "Bearer keychain-token"


# ---------------------------------------------------------------------------
# LimitStore tests
# ---------------------------------------------------------------------------


def _fake_usage() -> UsageData:
    return UsageData(plan="Max", windows=[], extra_usage_enabled=False)


def _age_shared_state(config_dir: Path, by_s: float) -> None:
    """Rewind the shared state file's timestamps so a subsequent _do_fetch
    behaves like a poll tick arriving that much later."""
    path = limit_status._state_path(config_dir)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("fetched_at"):
        raw["fetched_at"] -= by_s
    if raw.get("backoff_until"):
        raw["backoff_until"] -= by_s
    path.write_text(json.dumps(raw), encoding="utf-8")


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

    def test_rate_limited_sets_backoff_and_emits_stale(self, tmp_path: Path) -> None:
        """A 429 records a per-key backoff deadline and re-emits the last known
        data marked ``rate_limited`` instead of blanking. Regression: the old
        code swallowed RateLimited, never backed off, and kept hammering."""
        cd = tmp_path / ".claude"
        cd.mkdir()
        key = limit_status._resolve_config_dir(cd)

        emitted: list = []
        store = LimitStore(interval_s=3600, on_update=lambda k, d: emitted.append(d))
        store._refs[key] = 1  # register without spawning the immediate-fetch thread

        # Seed the cache with a good fetch, then trip a 429.
        with patch("agent_takkub.limit_status.fetch_usage", return_value=_fake_usage()):
            store._do_fetch(key)
        assert key not in store._backoff_until

        # A real second poll happens interval_s later, by which point the
        # shared-state file is no longer "fresh" — age it so the direct
        # _do_fetch call below reaches the network path instead of the
        # cross-process reuse fast-path.
        _age_shared_state(key, by_s=7200)

        with patch(
            "agent_takkub.limit_status.fetch_usage",
            side_effect=limit_status.RateLimited(3600.0),
        ):
            store._do_fetch(key)

        assert key in store._backoff_until
        assert store._backoff_until[key] > time.monotonic() + 3000
        assert emitted[-1] is not None, "stale cached data should still be emitted"
        assert emitted[-1].status == "rate_limited"

    def test_rate_limited_backoff_honours_min_floor(self, tmp_path: Path) -> None:
        """A tiny/absent Retry-After is floored to _min_backoff_s so a burst of
        429s can never collapse the backoff to near-zero."""
        cd = tmp_path / ".claude"
        cd.mkdir()
        key = limit_status._resolve_config_dir(cd)

        store = LimitStore(interval_s=3600)
        store._refs[key] = 1

        with patch(
            "agent_takkub.limit_status.fetch_usage",
            side_effect=limit_status.RateLimited(None),
        ):
            store._do_fetch(key)

        assert store._backoff_until[key] >= time.monotonic() + store._min_backoff_s - 1

    def test_loop_skips_key_while_backed_off(self, tmp_path: Path) -> None:
        """_loop must NOT fetch a key whose backoff deadline is in the future —
        this is what stops the 429 penalty from re-arming itself."""
        cd = tmp_path / ".claude"
        cd.mkdir()
        key = limit_status._resolve_config_dir(cd)

        calls: list = []
        store = LimitStore(interval_s=0)  # wake immediately, spin the loop hard
        store._refs[key] = 1
        store._backoff_until[key] = time.monotonic() + 3600  # long backoff

        with patch(
            "agent_takkub.limit_status.fetch_usage",
            side_effect=lambda cfg=None: calls.append(1) or _fake_usage(),
        ):
            store.start()
            time.sleep(0.25)
            store.stop()

        assert calls == [], "loop must skip a key that is still backing off"

    def test_loop_resumes_after_backoff_expires(self, tmp_path: Path) -> None:
        """Once the backoff deadline passes, _loop fetches the key again and a
        successful fetch clears the deadline."""
        cd = tmp_path / ".claude"
        cd.mkdir()
        key = limit_status._resolve_config_dir(cd)

        fired = threading.Event()
        store = LimitStore(interval_s=0)
        store._refs[key] = 1
        store._backoff_until[key] = time.monotonic() - 1  # already expired

        def fake_fetch(cfg=None):
            fired.set()
            return _fake_usage()

        with patch("agent_takkub.limit_status.fetch_usage", side_effect=fake_fetch):
            store.start()
            ok = fired.wait(timeout=2.0)
            store.stop()

        assert ok, "loop must fetch again once the backoff deadline has passed"
        assert key not in store._backoff_until, "a successful fetch must clear backoff"


# ---------------------------------------------------------------------------
# missing-utilization honesty + cross-process shared state
# ---------------------------------------------------------------------------


class TestParseWindowUtilization:
    def test_missing_utilization_is_none_not_zero(self) -> None:
        """A window whose payload has resets_at but NO utilization field must
        parse as utilization=None (unknown), never 0.0 — a schema change once
        rendered as a confident '0%' on the prod chip."""
        w = limit_status._parse_window("five_hour", {"resets_at": "2026-06-09T10:00:00+00:00"})
        assert w is not None
        assert w.utilization is None

    def test_null_utilization_is_none(self) -> None:
        w = limit_status._parse_window(
            "five_hour",
            {"utilization": None, "resets_at": "2026-06-09T10:00:00+00:00"},
        )
        assert w is not None
        assert w.utilization is None

    def test_real_zero_stays_zero(self) -> None:
        """An explicit utilization: 0 from the API is a genuine 0%, kept as 0.0."""
        w = limit_status._parse_window(
            "five_hour",
            {"utilization": 0, "resets_at": "2026-06-09T10:00:00+00:00"},
        )
        assert w is not None
        assert w.utilization == 0.0


class TestSharedState:
    def test_roundtrip(self, tmp_path: Path) -> None:
        cd = tmp_path / ".claude"
        cd.mkdir()
        data = UsageData(
            plan="Max",
            windows=[
                limit_status.LimitWindow(
                    name="five_hour",
                    utilization=42.5,
                    resets_at=datetime(2026, 6, 9, 10, tzinfo=UTC),
                ),
                limit_status.LimitWindow(
                    name="seven_day",
                    utilization=None,
                    resets_at=datetime(2026, 6, 15, 10, tzinfo=UTC),
                ),
            ],
            extra_usage_enabled=True,
            fetched_at=datetime(2026, 6, 9, 9, tzinfo=UTC),
        )
        limit_status.save_shared_state(cd, data=data)
        state = limit_status.load_shared_state(cd)
        assert state["backoff_until"] == 0.0
        assert state["fetched_at"] > 0
        loaded = state["data"]
        assert loaded is not None
        assert loaded.plan == "Max"
        assert loaded.windows[0].utilization == 42.5
        assert loaded.windows[1].utilization is None
        assert loaded.windows[1].resets_at == datetime(2026, 6, 15, 10, tzinfo=UTC)

    def test_missing_file_is_empty_state(self, tmp_path: Path) -> None:
        state = limit_status.load_shared_state(tmp_path / "nowhere")
        assert state == {"backoff_until": 0.0, "fetched_at": 0.0, "data": None}

    def test_second_store_honours_persisted_backoff(self, tmp_path: Path) -> None:
        """A 429 recorded by one store (process) must stop a FRESH store from
        fetching the same account — the multi-instance hammering fix."""
        cd = tmp_path / ".claude"
        cd.mkdir()
        key = limit_status._resolve_config_dir(cd)

        first = LimitStore(interval_s=3600)
        first._refs[key] = 1
        with patch(
            "agent_takkub.limit_status.fetch_usage",
            side_effect=limit_status.RateLimited(3600.0),
        ):
            first._do_fetch(key)

        calls: list = []
        second = LimitStore(interval_s=3600)  # simulates another cockpit instance
        second._refs[key] = 1
        with patch(
            "agent_takkub.limit_status.fetch_usage",
            side_effect=lambda cfg=None: calls.append(1) or _fake_usage(),
        ):
            second._do_fetch(key)

        assert calls == [], "second instance must honour the persisted backoff"
        assert key in second._backoff_until

    def test_second_store_reuses_fresh_shared_data(self, tmp_path: Path) -> None:
        """A fresh result persisted by one store is reused by another instead
        of a duplicate network fetch."""
        cd = tmp_path / ".claude"
        cd.mkdir()
        key = limit_status._resolve_config_dir(cd)

        first = LimitStore(interval_s=3600)
        first._refs[key] = 1
        with patch("agent_takkub.limit_status.fetch_usage", return_value=_fake_usage()):
            first._do_fetch(key)

        calls: list = []
        emitted: list = []
        second = LimitStore(interval_s=3600, on_update=lambda k, d: emitted.append(d))
        second._refs[key] = 1
        with patch(
            "agent_takkub.limit_status.fetch_usage",
            side_effect=lambda cfg=None: calls.append(1) or _fake_usage(),
        ):
            second._do_fetch(key)

        assert calls == [], "fresh shared data must be reused, not re-fetched"
        assert second.get(cd) is not None
        assert emitted and emitted[-1] is not None


class TestFetchUsageShared:
    def test_429_persists_backoff_and_returns_stale(self, tmp_path: Path) -> None:
        cd = tmp_path / ".claude"
        cd.mkdir()
        limit_status.save_shared_state(cd, data=_fake_usage())
        _age_shared_state(cd, by_s=7200)

        with patch(
            "agent_takkub.limit_status.fetch_usage",
            side_effect=limit_status.RateLimited(1800.0),
        ):
            out = limit_status.fetch_usage_shared(cd, max_age_s=300.0)

        assert out is not None, "stale data should be returned while penalised"
        state = limit_status.load_shared_state(cd)
        assert state["backoff_until"] > time.time() + 1200

    def test_backoff_blocks_network(self, tmp_path: Path) -> None:
        cd = tmp_path / ".claude"
        cd.mkdir()
        limit_status.save_shared_state(cd, backoff_until=time.time() + 3600)

        with patch("agent_takkub.limit_status.fetch_usage") as mock_fetch:
            out = limit_status.fetch_usage_shared(cd)

        mock_fetch.assert_not_called()
        assert out is None  # no prior data — caller sees "unknown", not a hit

    def test_fresh_data_short_circuits(self, tmp_path: Path) -> None:
        cd = tmp_path / ".claude"
        cd.mkdir()
        limit_status.save_shared_state(cd, data=_fake_usage())

        with patch("agent_takkub.limit_status.fetch_usage") as mock_fetch:
            out = limit_status.fetch_usage_shared(cd, max_age_s=300.0)

        mock_fetch.assert_not_called()
        assert out is not None
