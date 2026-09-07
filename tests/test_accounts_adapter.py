"""accounts_adapter (#505 stage 1) — the Accounts page's one storage layer.

Pure-logic tests, no Qt: login/plan detection from provider-written
credential files, per-project selection scan, add/remove write-through to
user_profile, gap handling straight from config (never hardcoded), and the
login-pane launch spec.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_takkub import accounts_adapter, config, user_profile


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(user_profile, "_REGISTRY_PATH", tmp_path / "user-profiles.json")
    monkeypatch.setattr(user_profile, "_DEFAULT_CONFIG_DIR", tmp_path / ".claude")
    monkeypatch.setattr(user_profile, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_HOME", tmp_path)
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    yield


def _write_creds(config_dir: Path, *, token: str = "tok", tier: str = "max_20x") -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": token,
                    "subscriptionType": "max",
                    "rateLimitTier": tier,
                }
            }
        ),
        encoding="utf-8",
    )


class TestClaudeLoginStatus:
    def test_logged_in_with_plan_from_credentials(self, tmp_path: Path) -> None:
        home = tmp_path / ".claude"
        _write_creds(home, tier="max_20x")
        status = accounts_adapter.claude_login_status(home, is_default=True)
        assert status.state == accounts_adapter.LOGGED_IN
        assert status.plan == "Max 20x"

    def test_email_read_from_claude_json(self, tmp_path: Path) -> None:
        home = tmp_path / ".claude"
        _write_creds(home, tier="pro")
        (home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "monchai500@gmail.com"}}),
            encoding="utf-8",
        )
        status = accounts_adapter.claude_login_status(home, is_default=True)
        assert status.state == accounts_adapter.LOGGED_IN
        assert status.detail == "monchai500@gmail.com"
        assert status.plan == "Pro"

    def test_tokenless_credential_file_is_logged_out(self, tmp_path: Path) -> None:
        home = tmp_path / ".claude"
        home.mkdir()
        (home / ".credentials.json").write_text("{}", encoding="utf-8")
        status = accounts_adapter.claude_login_status(home, is_default=True)
        assert status.state == accounts_adapter.LOGGED_OUT

    def test_missing_everything_never_guesses_logged_in(self, tmp_path: Path) -> None:
        status = accounts_adapter.claude_login_status(tmp_path / "nothing", is_default=True)
        if sys.platform == "win32":
            # Credential Manager isn't directly checkable — "ไม่ทราบ", not a guess.
            assert status.state == accounts_adapter.UNKNOWN
        else:
            assert status.state in (accounts_adapter.LOGGED_OUT, accounts_adapter.UNKNOWN)
        assert status.state != accounts_adapter.LOGGED_IN

    def test_named_profile_on_darwin_without_file_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A named profile must never borrow the default account's Keychain
        entry — no file on macOS means "ไม่ทราบ"."""
        monkeypatch.setattr(sys, "platform", "darwin")
        status = accounts_adapter.claude_login_status(tmp_path / "office", is_default=False)
        assert status.state == accounts_adapter.UNKNOWN
        assert "Keychain" in status.detail


class TestCodexLoginStatus:
    def test_auth_json_present(self, tmp_path: Path) -> None:
        (tmp_path / "auth.json").write_text("{}", encoding="utf-8")
        assert accounts_adapter.codex_login_status(tmp_path).state == accounts_adapter.LOGGED_IN

    def test_auth_json_missing(self, tmp_path: Path) -> None:
        assert accounts_adapter.codex_login_status(tmp_path).state == accounts_adapter.LOGGED_OUT

    def test_auth_json_unreadable(self, tmp_path: Path) -> None:
        (tmp_path / "auth.json").write_text("not json", encoding="utf-8")
        assert accounts_adapter.codex_login_status(tmp_path).state == accounts_adapter.UNKNOWN

    def test_plan_comes_only_from_a_running_usage_store(self) -> None:
        # No store started in tests → no plan, never a probe.
        assert accounts_adapter._codex_plan_cached() is None


class TestProjectsBySelection:
    def test_reads_old_and_new_selection_formats(self, tmp_path: Path) -> None:
        a = tmp_path / "projects" / "proj-a"
        b = tmp_path / "projects" / "proj-b"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        (a / "user-profile.json").write_text(json.dumps({"name": "office"}), encoding="utf-8")
        (b / "user-profile.json").write_text(
            json.dumps({"providers": {"claude": "office", "openai": "work"}}),
            encoding="utf-8",
        )
        out = accounts_adapter.projects_by_selection()
        assert out[("claude", "office")] == ["proj-a", "proj-b"]
        # legacy "openai" spelling normalizes to codex
        assert out[("codex", "work")] == ["proj-b"]


class TestProviderRows:
    def test_one_row_per_registry_provider_in_order(self) -> None:
        from agent_takkub.provider_spec import PROVIDER_REGISTRY

        rows = accounts_adapter.provider_rows()
        assert [r.provider for r in rows] == list(PROVIDER_REGISTRY)

    def test_gap_rows_come_from_config_not_a_hardcoded_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gaps = dict(config.PROVIDER_ISOLATION_GAPS)
        gaps["codex"] = "fake gap for this test"
        monkeypatch.setattr(config, "PROVIDER_ISOLATION_GAPS", gaps)
        rows = {r.provider: r for r in accounts_adapter.provider_rows()}
        assert rows["codex"].gap_reason == "fake gap for this test"
        assert rows["codex"].accounts == []
        assert rows["codex"].can_add is False

    def test_gap_providers_are_shown_not_hidden(self) -> None:
        rows = {r.provider: r for r in accounts_adapter.provider_rows()}
        for provider, reason in config.PROVIDER_ISOLATION_GAPS.items():
            assert rows[provider].gap_reason == reason

    def test_claude_row_has_default_account_and_selections(self, tmp_path: Path) -> None:
        user_profile.add_profile("office", str(tmp_path / "office-cfg"), share_sessions=False)
        proj = tmp_path / "projects" / "libsbt"
        proj.mkdir(parents=True)
        (proj / "user-profile.json").write_text(json.dumps({"name": "office"}), encoding="utf-8")

        rows = {r.provider: r for r in accounts_adapter.provider_rows()}
        claude = rows["claude"]
        assert claude.can_add is True
        by_name = {a.name: a for a in claude.accounts}
        assert by_name["default"].is_default is True
        assert by_name["office"].projects == ["libsbt"]

    def test_v2_registry_leftovers_still_visible(self) -> None:
        from agent_takkub.core.accounts.registry import AccountRegistry
        from agent_takkub.core.models.account import ProviderAccount

        AccountRegistry().upsert(ProviderAccount(id="acc-1", provider_id="codex"))
        rows = {r.provider: r for r in accounts_adapter.provider_rows()}
        v2 = [a for a in rows["codex"].accounts if a.origin == "v2"]
        assert [a.name for a in v2] == ["acc-1"]


class TestWriteSide:
    def test_default_account_home_derives_from_config_base(self) -> None:
        home = accounts_adapter.default_account_home("claude", "office")
        assert home.name == ".claude-office"

    def test_add_account_blank_dir_uses_conventional_home(self) -> None:
        home, _linked = accounts_adapter.add_account("claude", "office", share_sessions=False)
        assert home.name == ".claude-office"
        assert any(
            p["name"] == "office" and p["provider"] == "claude"
            for p in user_profile.list_profiles()
        )

    def test_add_reserved_name_raises(self) -> None:
        with pytest.raises(ValueError):
            accounts_adapter.add_account("claude", "default", share_sessions=False)

    def test_remove_profile_account(self, tmp_path: Path) -> None:
        user_profile.add_profile("office", str(tmp_path / "office-cfg"), share_sessions=False)
        account = accounts_adapter.AccountInfo(
            provider="claude",
            name="office",
            config_dir=str(tmp_path / "office-cfg"),
            is_default=False,
            login=accounts_adapter.LoginStatus(accounts_adapter.UNKNOWN),
        )
        accounts_adapter.remove_account(account)
        assert not any(p["name"] == "office" for p in user_profile.list_profiles())

    def test_remove_default_raises(self) -> None:
        account = accounts_adapter.AccountInfo(
            provider="claude",
            name="default",
            config_dir="",
            is_default=True,
            login=accounts_adapter.LoginStatus(accounts_adapter.UNKNOWN),
        )
        with pytest.raises(ValueError):
            accounts_adapter.remove_account(account)

    def test_can_add_stage1_is_claude_only(self) -> None:
        assert accounts_adapter.can_add_account("claude") == (True, "")
        ok, hint = accounts_adapter.can_add_account("opencode")
        assert ok is False and hint
        for provider in config.PROVIDER_ISOLATION_GAPS:
            ok, hint = accounts_adapter.can_add_account(provider)
            assert ok is False
            assert hint == config.PROVIDER_ISOLATION_GAPS[provider]


class TestLoginLaunch:
    def test_claude_launch_scopes_config_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "find_claude_executable", lambda: "claude-exe")
        launch = accounts_adapter.login_launch("claude", "C:/homes/office")
        assert launch is not None
        argv, env = launch
        assert argv == ["claude-exe"]
        assert env == {"CLAUDE_CONFIG_DIR": "C:/homes/office"}

    def test_claude_launch_defaults_to_default_profile_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "find_claude_executable", lambda: "claude-exe")
        launch = accounts_adapter.login_launch("claude", "")
        assert launch is not None
        _argv, env = launch
        assert env == {"CLAUDE_CONFIG_DIR": str(user_profile._DEFAULT_CONFIG_DIR)}

    def test_unverifiable_provider_has_no_launch(self) -> None:
        assert accounts_adapter.login_launch("gemini", "") is None
