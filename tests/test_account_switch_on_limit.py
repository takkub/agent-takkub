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
