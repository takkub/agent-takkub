"""Same-provider account switch on a usage limit (2026-10-08).

A codex teammate on prod sat on "■ You’ve hit your usage limit … try again at
12:39 PM." with the reroute policy on and nothing happened: the banner was
never detected (■ bullet, typographic apostrophe, wrapped reset clause). Once
detected, the task now moves to another logged-in account of the same
provider before falling back to another provider.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_takkub import limit_autoresume, pty_session

CODEX_LIMIT_SCREEN = [
    "  + 347 lines (ctrl+t to expand)",
    "",
    "■ You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
    "visit https://chatgpt.com/codex/settings/usage to purchase more credits or",
    "try again at 12:39 PM.",
    "",
    "› Ask Codex to do anything",
    "  GPT-6.1-Sol high · 5h 0% left · weekly 47% left · 91K used · Fast off",
]


def test_codex_limit_banner_is_detected_with_its_clock_reset() -> None:
    text = pty_session._quota_banner_text(CODEX_LIMIT_SCREEN)
    reset = pty_session._parse_rate_limit_reset(
        text, time.time(), pty_session._resolve_quota_markers("codex")
    )
    assert reset is not None
    assert time.strftime("%H:%M", time.localtime(reset)) == "12:39"


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    """Three codex profiles: default (the hit one), "b" logged in, "c" not."""
    homes = {name: tmp_path / name for name in ("default", "b", "c")}
    for name, home in homes.items():
        home.mkdir()
        if name != "c":
            (home / "auth.json").write_text("{}", encoding="utf-8")
    from agent_takkub import user_profile

    monkeypatch.setattr(
        user_profile,
        "profiles_for_provider",
        lambda provider: [{"name": n, "provider": provider} for n in homes],
    )
    monkeypatch.setattr(user_profile, "profile_home", lambda provider, name: homes.get(name))
    monkeypatch.setattr(user_profile, "profile_for", lambda project, provider="claude": "default")
    monkeypatch.setattr(limit_autoresume, "_account_identity", lambda provider, home: home.name)
    monkeypatch.setattr(limit_autoresume, "_account_exhausted", lambda provider, home: False)
    monkeypatch.setattr(limit_autoresume, "_ACCOUNT_LIMIT_UNTIL", {})
    return homes


def test_pick_moves_to_another_logged_in_account(accounts) -> None:
    reset = time.time() + 3600
    assert limit_autoresume.pick_reroute_account("p", "codex", None, reset) == "b"
    assert limit_autoresume.account_limited("codex", "default")


def test_pick_never_bounces_back_and_skips_logged_out(accounts) -> None:
    reset = time.time() + 3600
    limit_autoresume.pick_reroute_account("p", "codex", None, reset)  # default spent
    # now "b" hits too: default is still limited and "c" has no login
    assert limit_autoresume.pick_reroute_account("p", "codex", "b", reset) is None


def test_pick_skips_spent_account_by_telemetry(accounts, monkeypatch) -> None:
    monkeypatch.setattr(
        limit_autoresume, "_account_exhausted", lambda provider, home: home.name == "b"
    )
    assert limit_autoresume.pick_reroute_account("p", "codex", None, time.time() + 60) is None


def test_next_task_spawns_on_the_free_account_while_project_one_is_spent(accounts) -> None:
    # done() closes the switched pane; the next task's spawn must not boot
    # straight back onto the spent project account.
    assert limit_autoresume.account_for_spawn("p", "codex") is None
    limit_autoresume.pick_reroute_account("p", "codex", None, time.time() + 3600)
    assert limit_autoresume.account_for_spawn("p", "codex") == "b"


# ── usage meter = primary signal ───────────────────────────────────────────
def _meter(monkeypatch, rows: dict):
    """Fake usage store: {home_name: ProviderUsage}."""
    from agent_takkub import provider_usage

    class _Store:
        def get_account_usage(self, provider, home):
            return rows.get(Path(home).name)

    monkeypatch.setattr(provider_usage, "get_store", lambda: _Store())


def _usage(windows, *, age_s=60.0):
    from datetime import UTC, datetime, timedelta

    from agent_takkub.provider_usage import ProviderUsage

    now = datetime.now(tz=UTC)
    return ProviderUsage(
        provider="codex",
        status="active",
        fetched_at=now - timedelta(seconds=age_s),
        windows=[
            {
                "name": n,
                "utilization": pct,
                "resets_at": (now + timedelta(hours=h)).isoformat(),
            }
            for n, pct, h in windows
        ],
    )


def test_meter_reset_comes_from_the_spent_window(monkeypatch, tmp_path) -> None:
    _meter(monkeypatch, {"a": _usage([("primary", 100, 2), ("secondary", 47, 100)])})
    reset = limit_autoresume.meter_reset_at("codex", tmp_path / "a", 100.0)
    assert reset is not None and 1.9 * 3600 < reset - time.time() < 2.1 * 3600


def test_meter_ignores_stale_snapshots_passed_resets_and_model_windows(
    monkeypatch, tmp_path
) -> None:
    _meter(
        monkeypatch,
        {
            "stale": _usage([("primary", 100, 2)], age_s=3600),
            "reset_passed": _usage([("primary", 100, -1)]),
            "sonnet_only": _usage([("five_hour", 10, 2), ("seven_day_sonnet", 100, 50)]),
        },
    )
    for name in ("stale", "reset_passed", "sonnet_only"):
        assert limit_autoresume.meter_reset_at("codex", tmp_path / name, 100.0) is None, name


def test_spawn_avoids_an_account_the_meter_shows_nearly_spent(accounts, monkeypatch) -> None:
    # No hit recorded yet — the meter alone says the project account is at
    # 96%, so new work starts on "b" instead of walking into the limit.
    _meter(monkeypatch, {"default": _usage([("primary", 96, 1)]), "b": _usage([("primary", 5, 4)])})
    assert limit_autoresume.account_for_spawn("p", "codex") == "b"


def test_spawn_skips_a_candidate_the_meter_shows_spent(accounts, monkeypatch) -> None:
    _meter(
        monkeypatch,
        {"default": _usage([("primary", 100, 1)]), "b": _usage([("secondary", 99, 30)])},
    )
    assert limit_autoresume.account_for_spawn("p", "codex") is None


def test_watchdog_detects_limit_from_meter_with_no_banner_on_screen() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agent_takkub.orchestrator import Orchestrator
    from agent_takkub.spawn_engine import PaneState

    reset = time.time() + 7200
    states: dict = {}
    session = MagicMock(is_alive=True)
    session.rate_limit_reset_at.return_value = None  # banner never matched
    session.quota_stall_marker.return_value = None
    session.is_at_limit_choice_modal.return_value = False
    pane = SimpleNamespace(session=session, model=SimpleNamespace(provider_name="codex"))
    fake = SimpleNamespace(
        _pane_state=states,
        _ps=lambda key: states.setdefault(key, PaneState()),
        _meter_limit_reset_at=lambda project, role, provider, now: reset,
        _schedule_rate_limit_notice=MagicMock(),
        _notify_quota_hit=MagicMock(),
    )
    assert Orchestrator._rate_limit_suppressed(fake, "p", "backend", pane, time.time()) is True
    ps = states["p::backend"]
    assert ps.rate_limited_until == reset
    assert ps.quota_provider == "codex"
    assert ps.quota_marker == "usage-meter"


def test_running_account_comes_from_the_live_pane_not_rebuilt_state() -> None:
    """prod 2026-10-08: PaneState was rebuilt after the switch, the watchdog
    read the project's (spent) account again and re-switched every tick."""
    from types import SimpleNamespace

    from agent_takkub.orchestrator import Orchestrator
    from agent_takkub.spawn_engine import PaneState

    pane = SimpleNamespace(_spawn_account="default")
    fake = SimpleNamespace(
        _project_panes=lambda project: {"lead": pane},
        _pane_state={"p::lead": PaneState()},  # override lost
    )
    assert Orchestrator._pane_running_account(fake, "p", "lead") == "default"
    pane._spawn_account = None  # spawned on the project's own account
    assert Orchestrator._pane_running_account(fake, "p", "lead") is None


def test_v2_router_never_overrides_a_switched_account() -> None:
    """prod runs with the V2 router on: it resolved the PROJECT's account and
    wrote CODEX_HOME back over the switched one."""
    import inspect

    from agent_takkub import spawn_engine

    src = inspect.getsource(spawn_engine)
    assert "if v2_router_enabled() and not _account:" in src
    assert "if v2_router_enabled() and not _claude_account:" in src
    assert src.count("_apply_v2_account_env_override(env,") == 2


def test_provider_counts_usable_only_with_fresh_headroom_on_some_account(
    accounts, monkeypatch
) -> None:
    """The provider-level reprobe used to read the default account alone and
    announce "codex quota reset" while the spent account was still spent."""
    _meter(monkeypatch, {"default": _usage([("primary", 100, 2)])})  # b: no snapshot
    assert limit_autoresume.account_with_fresh_headroom("codex") is None
    _meter(monkeypatch, {"b": _usage([("primary", 100, 2)], age_s=3600)})  # stale
    assert limit_autoresume.account_with_fresh_headroom("codex") is None
    _meter(monkeypatch, {"b": _usage([("primary", 20, 2)])})
    assert limit_autoresume.account_with_fresh_headroom("codex") == "b"


def test_account_switch_does_not_mark_the_whole_provider_spent(accounts, monkeypatch) -> None:
    from types import SimpleNamespace

    from agent_takkub import provider_state
    from agent_takkub.spawn_engine import PaneState

    recorded: list = []
    monkeypatch.setattr(provider_state, "set_quota_reset_at", lambda p, t: recorded.append(p))
    moved: list = []
    host = SimpleNamespace(
        _running_account=lambda project, role, ps: None,
        _reroute_pane_to_provider=lambda *a, **k: moved.append(k.get("account")),
        _schedule_provider_quota_reset_notice=lambda *a: None,
    )
    ps = PaneState()
    ps.quota_provider = "codex"
    ps.rate_limited_until = time.time() + 3600
    limit_autoresume.AutoResumeMixin._reroute_or_park(host, "p", "backend", ps)
    assert moved == ["b"]
    assert recorded == []


def test_providers_without_account_knob_never_switch(accounts) -> None:
    for provider in ("gemini", "opencode", "cursor"):
        assert limit_autoresume.pick_reroute_account("p", provider, None, time.time()) is None


def test_switched_pane_gets_that_accounts_codex_home(tmp_path, monkeypatch) -> None:
    from agent_takkub import pane_env, user_profile

    home_b = tmp_path / "b"
    monkeypatch.setattr(
        user_profile, "profile_home", lambda provider, name: home_b if name == "b" else None
    )
    monkeypatch.setattr(user_profile, "provider_config_dir_for", lambda project, provider: None)
    env: dict[str, str] = {}
    pane_env.inject_provider_home_env(env, "codex", "p", account="b")
    assert env["CODEX_HOME"] == str(home_b)


def test_claude_curated_dir_is_per_account(monkeypatch) -> None:
    from agent_takkub import user_profile

    monkeypatch.setattr(user_profile, "profile_for", lambda project, provider="claude": "default")
    base = user_profile.curated_config_dir_for("proj")
    other = user_profile.curated_config_dir_for("proj", "work")
    assert base != other
    assert Path(other).name.startswith("work-")
