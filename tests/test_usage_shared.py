"""Machine-wide shared usage cache + per-instance auto-fetch switch.

Guards: identity keying (dev/prod config dirs of ONE account share a record),
one-real-fetch-per-account (lease), a 429 seen by one instance honoured by all,
lost-update safety under concurrent writers, and switch OFF => zero fetches.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_takkub import limit_status, provider_usage, usage_shared
from agent_takkub.limit_status import LimitStore, LimitWindow, UsageData


def _acct_dir(root: Path, name: str, uuid: str | None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    if uuid:
        (d / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": uuid, "emailAddress": "me@x.test"}}),
            encoding="utf-8",
        )
    return d


def _usage(plan: str = "Pro") -> UsageData:
    return UsageData(
        plan=plan,
        windows=[LimitWindow("five_hour", 10.0, datetime.now(tz=UTC) + timedelta(hours=1))],
        extra_usage_enabled=False,
        fetched_at=datetime.now(tz=UTC),
    )


@pytest.fixture
def switch_off():
    usage_shared.set_auto_fetch_enabled(False)
    yield
    usage_shared.set_auto_fetch_enabled(True)


class TestIdentity:
    def test_same_account_two_config_dirs_share_one_record(self, tmp_path: Path) -> None:
        dev = _acct_dir(tmp_path, "dev", "AAA-1")
        prod = _acct_dir(tmp_path, "prod", "aaa-1")
        other = _acct_dir(tmp_path, "other", "BBB-2")
        assert usage_shared.claude_identity(dev) == usage_shared.claude_identity(prod)
        assert usage_shared.claude_identity(dev) != usage_shared.claude_identity(other)

    def test_no_account_falls_back_to_dir_path(self, tmp_path: Path) -> None:
        a = _acct_dir(tmp_path, "a", None)
        b = _acct_dir(tmp_path, "b", None)
        assert usage_shared.claude_identity(a).startswith("dir:")
        assert usage_shared.claude_identity(a) != usage_shared.claude_identity(b)

    def test_codex_identity_uses_account_id_never_token(self, tmp_path: Path) -> None:
        h1, h2 = tmp_path / "h1", tmp_path / "h2"
        for h in (h1, h2):
            h.mkdir()
            (h / "auth.json").write_text(
                json.dumps({"tokens": {"account_id": "ACC-9", "access_token": "SECRET"}}),
                encoding="utf-8",
            )
        assert usage_shared.codex_identity(h1) == usage_shared.codex_identity(h2)
        assert "SECRET" not in usage_shared.codex_identity(h1)

    def test_cache_dir_env_override_and_neutral_default(self, monkeypatch, tmp_path: Path) -> None:
        assert usage_shared.cache_dir() == tmp_path / "_isolated_usage_cache"
        monkeypatch.delenv(usage_shared.CACHE_ENV)
        assert "agent-takkub" in usage_shared.cache_dir().parts


class TestRecord:
    def test_concurrent_writers_lose_no_updates(self) -> None:
        def bump(rec):
            rec["extra"]["n"] = rec["extra"].get("n", 0) + 1

        def work():
            for _ in range(15):
                usage_shared.update_record("claude", "acct:c", bump)

        threads = [threading.Thread(target=work) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert usage_shared.read_record("claude", "acct:c")["extra"]["n"] == 90

    def test_lease_is_exclusive_and_expires(self, monkeypatch) -> None:
        # Real disk I/O on a busy Windows runner can take longer than this
        # short lease. Control its clock while keeping locking and I/O real.
        now = [1000.0]
        monkeypatch.setattr(
            usage_shared,
            "time",
            SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic, sleep=time.sleep),
        )
        first = usage_shared.try_acquire_lease("claude", "acct:l", ttl_s=0.3)
        assert first is not None
        assert usage_shared.try_acquire_lease("claude", "acct:l") is None
        now[0] += 0.4
        assert usage_shared.try_acquire_lease("claude", "acct:l") is not None

    def test_corrupt_record_reads_as_blank(self) -> None:
        usage_shared.set_extra("claude", "acct:z", "k", 1)
        path = usage_shared._record_path("claude", "acct:z")
        path.write_text("{not json", encoding="utf-8")
        assert usage_shared.read_record("claude", "acct:z")["payload"] is None


class TestOneFetchPerAccount:
    def test_two_instances_same_account_fetch_once(self, tmp_path: Path) -> None:
        dev = _acct_dir(tmp_path, "dev", "acct-1")
        prod = _acct_dir(tmp_path, "prod", "acct-1")
        with patch("agent_takkub.limit_status.fetch_usage", return_value=_usage()) as m:
            a = limit_status.fetch_usage_shared(dev)
            b = limit_status.fetch_usage_shared(prod)
        assert m.call_count == 1
        assert a is not None and b is not None and a.plan == b.plan

    def test_429_from_one_instance_stops_the_other(self, tmp_path: Path) -> None:
        dev = _acct_dir(tmp_path, "dev", "acct-2")
        prod = _acct_dir(tmp_path, "prod", "acct-2")
        with patch(
            "agent_takkub.limit_status.fetch_usage",
            side_effect=limit_status.RateLimited(1800.0),
        ):
            limit_status.fetch_usage_shared(dev)
        with patch("agent_takkub.limit_status.fetch_usage") as m:
            limit_status.fetch_usage_shared(prod)
            store = LimitStore(interval_s=600)
            key = limit_status._resolve_config_dir(prod)
            store._refs[key] = 1
            store._do_fetch(key)
        assert m.call_count == 0

    def test_held_lease_blocks_second_fetcher(self, tmp_path: Path) -> None:
        d = _acct_dir(tmp_path, "d", "acct-3")
        ident = usage_shared.claude_identity(d)
        assert usage_shared.try_acquire_lease("claude", ident) is not None
        with patch("agent_takkub.limit_status.fetch_usage") as m:
            assert limit_status.fetch_usage_shared(d) is None
        assert m.call_count == 0

    def test_codex_shared_across_homes_of_one_account(self, tmp_path: Path) -> None:
        homes = []
        for n in ("h1", "h2"):
            h = tmp_path / n
            h.mkdir()
            (h / "auth.json").write_text(
                json.dumps({"tokens": {"account_id": "same"}}), encoding="utf-8"
            )
            homes.append(h)
        ok = provider_usage.ProviderUsage(
            provider="codex", status="active", utilization=5.0, fetched_at=datetime.now(tz=UTC)
        )
        with patch.object(provider_usage, "_dispatch_fetch", return_value=ok) as m:
            provider_usage.fetch_provider_usage("codex", homes[0])
            out = provider_usage.fetch_provider_usage("codex", homes[1])
        assert m.call_count == 1 and out.utilization == 5.0


class TestSwitchOff:
    def test_default_on_and_persisted(self) -> None:
        assert usage_shared.auto_fetch_enabled() is True
        usage_shared.set_auto_fetch_enabled(False)
        assert usage_shared.auto_fetch_enabled() is False
        usage_shared.set_auto_fetch_enabled(True)
        assert usage_shared.auto_fetch_enabled() is True

    def test_off_means_zero_fetches_but_serves_shared_cache(
        self, tmp_path: Path, switch_off
    ) -> None:
        d = _acct_dir(tmp_path, "d", "acct-4")
        limit_status.save_shared_state(d, data=_usage("Pro"))
        usage_shared.update_record(
            "claude",
            usage_shared.claude_identity(d),
            lambda r: r.__setitem__("fetched_at", time.time() - 99999),  # long stale
        )
        with patch("agent_takkub.limit_status.fetch_usage") as m:
            out = limit_status.fetch_usage_shared(d)
            emitted: list = []
            store = LimitStore(interval_s=600, on_update=lambda k, dat: emitted.append(dat))
            key = limit_status._resolve_config_dir(d)
            store._refs[key] = 1
            store._do_fetch(key)
        assert m.call_count == 0
        assert out is not None and out.plan == "Pro"
        assert emitted and emitted[-1] is not None

    def test_off_blocks_provider_probes_and_reports_no_cache(self, switch_off) -> None:
        with patch.object(provider_usage, "_dispatch_fetch") as m:
            out = provider_usage.fetch_provider_usage("codex", Path("nowhere"))
            gem = provider_usage.fetch_provider_usage("gemini")
        assert m.call_count == 0
        assert out.status == "error" and gem.status == "error"

    def test_turning_on_wakes_registered_pollers(self) -> None:
        woke = []
        usage_shared.add_wake_hook(lambda: woke.append(1))
        usage_shared.set_auto_fetch_enabled(True)
        assert woke


class TestPlan:
    def test_plan_from_server_profile(self) -> None:
        pro = {"organization": {"organization_type": "claude_pro", "rate_limit_tier": "x"}}
        assert limit_status._plan_from_profile(pro) == "Pro"
        m20 = {
            "organization": {
                "organization_type": "claude_max",
                "rate_limit_tier": "default_claude_max_20x",
            }
        }
        assert limit_status._plan_from_profile(m20) == "Max 20x"
        assert limit_status._plan_from_profile({}) is None

    def test_plan_cached_in_shared_record_once_per_day(self, tmp_path: Path) -> None:
        calls = []

        def fake_req(url, **kw):
            calls.append(url)
            return {"organization": {"organization_type": "claude_pro"}}

        with patch("agent_takkub.limit_status._request_json", fake_req):
            p1 = limit_status._resolve_plan("acct:p", "tok", "Max 20x")  # stale local blob
            p2 = limit_status._resolve_plan("acct:p", "tok", "Max 20x")
        assert (p1, p2) == ("Pro", "Pro") and len(calls) == 1

    def test_plan_lookup_failure_keeps_local_plan(self) -> None:
        with patch("agent_takkub.limit_status._request_json", side_effect=OSError("down")):
            assert limit_status._resolve_plan("acct:q", "tok", "Max") == "Max"


class TestClaudeTargets:
    def test_one_target_per_account_with_instance_independent_label(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        dev = _acct_dir(home, ".claude", "uuid-A")
        prod = _acct_dir(home / ".agent-takkub", "claude-config", "uuid-A")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(provider_usage.config, "default_claude_config_dir", lambda: dev)
        as_dev = provider_usage._discover_claude_usage_targets()
        monkeypatch.setattr(provider_usage.config, "default_claude_config_dir", lambda: prod)
        as_prod = provider_usage._discover_claude_usage_targets()
        assert len(as_dev) == len(as_prod) == 1
        assert as_dev[0].account == as_prod[0].account == "me@x.test"
        assert as_dev[0].identity == as_prod[0].identity
        assert not as_dev[0].read_only and not as_prod[0].read_only

    def test_other_instances_only_account_is_read_only(self, monkeypatch, tmp_path: Path) -> None:
        home = tmp_path / "home"
        mine = _acct_dir(home / ".agent-takkub", "claude-config", "uuid-B")
        _acct_dir(home, ".claude", "uuid-C")  # dev's account, unknown to this instance
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(provider_usage.config, "default_claude_config_dir", lambda: mine)
        targets = {t.identity: t for t in provider_usage._discover_claude_usage_targets()}
        assert len(targets) == 2
        assert targets["acct:uuid-b"].read_only is False
        assert targets["acct:uuid-c"].read_only is True
        assert targets["acct:uuid-c"].config_dir is None
