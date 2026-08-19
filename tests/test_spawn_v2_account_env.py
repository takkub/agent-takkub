"""epic #309 Phase 3b: `TAKKUB_V2_ROUTER` flag on/off parity for the
account-driven env override wired into spawn_engine.py's generic (codex)
and claude branches.

Flag OFF (default): `_apply_v2_account_env_override` is never called at
all — same harness/assertions as `test_spawn_codex_argv.py`, proving the
env this phase touches is untouched.
Flag ON with NO V2 pool registered: `core.accounts.resolve_account_for`
falls back to the legacy profile — the env stays exactly what
`inject_provider_home_env`/`inject_user_profile_env` already set.
Flag ON with a V2 pool registered: the selected account's `config_dir`
lands in `CODEX_HOME`/`CLAUDE_CONFIG_DIR`, proving the wiring is real.
"""

from __future__ import annotations

import pathlib
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.core.accounts.registry import AccountPoolRegistry, AccountRegistry
from agent_takkub.core.models.account import AccountPool, ProviderAccount, SelectionStrategy
from agent_takkub.core.storage.jsonl_store import JsonlStore
from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "v2accountenvtest"
FAKE_CLAUDE_CWD = "/tmp/takkub-test-v2-account-env-cwd"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_orchestrator(qapp, monkeypatch):
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda p: p or TEST_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _make_codex_pane(role: str = "codex"):
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role
    pane.session = None
    pane.state = "empty"
    pane._transcript_path = None
    return pane


def _spawn_codex_and_capture_env(qapp, monkeypatch, tmp_path):
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub import shared_dev_tools as sdt
    from agent_takkub.provider_config import CODEX

    orch = _make_orchestrator(qapp, monkeypatch)
    pane = _make_codex_pane("codex")
    orch._panes_by_project[TEST_PROJECT] = {"codex": pane}

    monkeypatch.setattr(sdt, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")

    pty_spawn_calls = []

    with (
        patch.object(orch, "_is_spawn_blocked", return_value=False),
        patch.object(orch, "_final_gate_clear", return_value=True),
        patch("agent_takkub.orchestrator.PtySession") as mock_pty_cls,
        patch("agent_takkub.orchestrator.QTimer.singleShot"),
        patch("agent_takkub.orchestrator._build_pane_env", return_value={}),
        patch("agent_takkub.spawn_engine.sys.platform", "linux"),
        patch(
            "agent_takkub.provider_config.effective_provider_for",
            return_value=CODEX,
        ),
        patch(
            "agent_takkub.codex_helper.find_codex_executable",
            return_value="codex",
        ),
        patch("agent_takkub.codex_agents_md.ensure_agents_md"),
        patch("agent_takkub.orchestrator.inject_user_profile_env"),
        patch("agent_takkub.mcp_bridge._codex_resolved_mcp_names", return_value=[]),
    ):
        mock_pty = MagicMock()
        mock_pty.spawn.side_effect = lambda **kw: pty_spawn_calls.append(kw)
        mock_pty_cls.return_value = mock_pty
        pane.attach_session = MagicMock()

        ok, msg = orch.spawn("codex", project=TEST_PROJECT)

    assert ok is True, msg
    assert pty_spawn_calls, "PtySession.spawn was not called"
    return pty_spawn_calls[0]["env"]


# Claude-branch harness (mirrors test_orchestrator_claude_env_leak.py).
_CLAUDE_COMMON_PATCHES: list[tuple[str, object]] = [
    ("agent_takkub.orchestrator.find_claude_executable", "fake-claude"),
    ("agent_takkub.orchestrator._build_transcript_path", pathlib.Path("/tmp/t.log")),
    ("agent_takkub.orchestrator._default_plugin_dirs", []),
    ("agent_takkub.orchestrator.render_lead_settings", pathlib.Path("/tmp/lead.json")),
    ("agent_takkub.orchestrator._render_lead_context", "/tmp/lead-ctx.md"),
    ("agent_takkub.orchestrator.apply_claude_auth_overrides", None),
    ("agent_takkub.orchestrator.agent_role_dir", pathlib.Path("/tmp/nonexistent-staging-xyz")),
    ("agent_takkub.orchestrator.default_cwd_for_role", FAKE_CLAUDE_CWD),
]


def _spawn_claude_and_capture_env(qapp, monkeypatch, role_name: str = "backend") -> dict[str, str]:
    # "default" project is exempt from the cwd-within-project check (same
    # reason test_orchestrator_claude_env_leak.py uses it) — this harness
    # only cares about env, not project cwd validation.
    project = "default"
    orch = _make_orchestrator(qapp, monkeypatch)
    captured: dict[str, str] = {}

    mock_session = MagicMock()
    mock_session.processExited = MagicMock()
    mock_session.is_alive = True

    def _capture_spawn(argv, cwd, env, transcript_path=None):
        captured.update(env)

    mock_session.spawn = _capture_spawn

    pane = MagicMock()
    pane.session = None
    orch._panes_by_project.setdefault(project, {})[role_name] = pane

    with ExitStack() as stack:
        for target, val in _CLAUDE_COMMON_PATCHES:
            stack.enter_context(patch(target, return_value=val))
        stack.enter_context(
            patch("agent_takkub.orchestrator.PtySession", return_value=mock_session)
        )
        stack.enter_context(patch.object(orch, "_auto_trust"))
        ok, msg = orch.spawn(role_name, cwd=FAKE_CLAUDE_CWD, project=project)

    assert ok, f"spawn({role_name!r}) unexpectedly failed: {msg}"
    return captured


def _empty_pool_registries(tmp_path):
    pools = AccountPoolRegistry(JsonlStore(tmp_path / "pools.jsonl"))
    accounts = AccountRegistry(JsonlStore(tmp_path / "accounts.jsonl"))
    return pools, accounts


def _registered_codex_pool_registries(tmp_path, high_dir, low_dir):
    pools, accounts = _empty_pool_registries(tmp_path)
    accounts.upsert(
        ProviderAccount(id="codex-hi", provider_id="codex", priority=10, config_dir=str(high_dir))
    )
    accounts.upsert(
        ProviderAccount(id="codex-lo", provider_id="codex", priority=1, config_dir=str(low_dir))
    )
    pools.upsert(
        AccountPool(
            id="codex-pool",
            name="codex-pool",
            provider_id="codex",
            account_ids=("codex-hi", "codex-lo"),
            strategy=SelectionStrategy.PRIORITY,
        )
    )
    return pools, accounts


class TestV2AccountEnvFlagOff:
    def test_flag_off_no_codex_home_override(self, qapp, monkeypatch, tmp_path):
        monkeypatch.delenv("TAKKUB_V2_ROUTER", raising=False)
        pools, accounts = _registered_codex_pool_registries(
            tmp_path, tmp_path / "hi", tmp_path / "lo"
        )
        monkeypatch.setattr("agent_takkub.core.accounts.facade.AccountPoolRegistry", lambda: pools)
        monkeypatch.setattr("agent_takkub.core.accounts.facade.AccountRegistry", lambda: accounts)
        env = _spawn_codex_and_capture_env(qapp, monkeypatch, tmp_path)
        # Flag off: _apply_v2_account_env_override is never called, even
        # though a matching pool exists — proves the flag gate, not just
        # the facade's own fallback behavior.
        assert "CODEX_HOME" not in env


class TestV2AccountEnvFlagOnNoPool:
    def test_flag_on_no_pool_leaves_env_unchanged(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setenv("TAKKUB_V2_ROUTER", "1")
        pools, accounts = _empty_pool_registries(tmp_path)
        monkeypatch.setattr("agent_takkub.core.accounts.facade.AccountPoolRegistry", lambda: pools)
        monkeypatch.setattr("agent_takkub.core.accounts.facade.AccountRegistry", lambda: accounts)
        env = _spawn_codex_and_capture_env(qapp, monkeypatch, tmp_path)
        # No pool -> legacy fallback -> a dev-checkout legacy codex account
        # carries no config_dir (provider_home_env is empty for DATA_HOME ==
        # REPO_ROOT) -> account_env_overrides is a no-op -> unchanged.
        assert "CODEX_HOME" not in env


class TestV2AccountEnvFlagOnWithPool:
    def test_flag_on_with_pool_applies_selected_account_config_dir(
        self, qapp, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("TAKKUB_V2_ROUTER", "1")
        hi_dir = tmp_path / "codex-hi-home"
        lo_dir = tmp_path / "codex-lo-home"
        pools, accounts = _registered_codex_pool_registries(tmp_path, hi_dir, lo_dir)
        monkeypatch.setattr("agent_takkub.core.accounts.facade.AccountPoolRegistry", lambda: pools)
        monkeypatch.setattr("agent_takkub.core.accounts.facade.AccountRegistry", lambda: accounts)
        env = _spawn_codex_and_capture_env(qapp, monkeypatch, tmp_path)
        assert env["CODEX_HOME"] == str(hi_dir)


class TestV2AccountEnvClaudeBranch:
    """Same flag on/off proof, for the claude branch's env-override hook."""

    def _claude_pool_registries(self, tmp_path, hi_dir, lo_dir):
        pools = AccountPoolRegistry(JsonlStore(tmp_path / "claude-pools.jsonl"))
        accounts = AccountRegistry(JsonlStore(tmp_path / "claude-accounts.jsonl"))
        accounts.upsert(
            ProviderAccount(
                id="claude-hi", provider_id="claude", priority=10, config_dir=str(hi_dir)
            )
        )
        accounts.upsert(
            ProviderAccount(
                id="claude-lo", provider_id="claude", priority=1, config_dir=str(lo_dir)
            )
        )
        pools.upsert(
            AccountPool(
                id="claude-pool",
                name="claude-pool",
                provider_id="claude",
                account_ids=("claude-hi", "claude-lo"),
                strategy=SelectionStrategy.PRIORITY,
            )
        )
        return pools, accounts

    def test_flag_off_no_claude_config_dir_override(self, qapp, monkeypatch, tmp_path):
        monkeypatch.delenv("TAKKUB_V2_ROUTER", raising=False)
        pools, accounts = self._claude_pool_registries(tmp_path, tmp_path / "hi", tmp_path / "lo")
        monkeypatch.setattr("agent_takkub.core.accounts.facade.AccountPoolRegistry", lambda: pools)
        monkeypatch.setattr("agent_takkub.core.accounts.facade.AccountRegistry", lambda: accounts)
        env = _spawn_claude_and_capture_env(qapp, monkeypatch)
        assert "CLAUDE_CONFIG_DIR" not in env

    def test_flag_on_applies_selected_account_config_dir(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setenv("TAKKUB_V2_ROUTER", "1")
        hi_dir = tmp_path / "claude-hi-home"
        lo_dir = tmp_path / "claude-lo-home"
        pools, accounts = self._claude_pool_registries(tmp_path, hi_dir, lo_dir)
        monkeypatch.setattr("agent_takkub.core.accounts.facade.AccountPoolRegistry", lambda: pools)
        monkeypatch.setattr("agent_takkub.core.accounts.facade.AccountRegistry", lambda: accounts)
        env = _spawn_claude_and_capture_env(qapp, monkeypatch)
        assert env["CLAUDE_CONFIG_DIR"] == str(hi_dir)
