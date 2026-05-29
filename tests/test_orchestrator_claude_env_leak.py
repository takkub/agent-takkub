"""Spawn-level regression: env passed to PtySession.spawn must exclude secrets
for Claude teammate panes; Lead uses an allowlist (not full env copy).

Coverage:
  1. spawn("backend")  → env excludes the four common secret vars
  2. spawn("lead")     → ANTHROPIC_API_KEY filtered; GH_TOKEN / git identity pass
  3. Both spawn paths  → TAKKUB_ROLE and TAKKUB_PROJECT always present
  4. TAKKUB_LEAD_TOKEN → never inherited from parent env by teammate panes;
     Lead pane receives the orchestrator-generated token (not any parent leak)
  5. _build_lead_env() unit tests: allowlist semantics + TAKKUB_LEAD_ENV_ALLOW
"""

from __future__ import annotations

import pathlib
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "default"
FAKE_CWD = "/tmp/takkub-test-spawn-cwd"

# Module-level patch targets to mock out I/O that would fail outside the cockpit.
_COMMON_PATCHES: list[tuple[str, object]] = [
    ("agent_takkub.orchestrator.find_claude_executable", "fake-claude"),
    ("agent_takkub.orchestrator._build_transcript_path", pathlib.Path("/tmp/t.log")),
    ("agent_takkub.orchestrator._default_plugin_dirs", []),
    ("agent_takkub.orchestrator.render_lead_settings", pathlib.Path("/tmp/lead.json")),
    ("agent_takkub.orchestrator._render_lead_context", "/tmp/lead-ctx.md"),
    ("agent_takkub.orchestrator.apply_claude_auth_overrides", None),
    # agent_role_dir returns a non-existent path so CLAUDE.md check → False
    ("agent_takkub.orchestrator.agent_role_dir", pathlib.Path("/tmp/nonexistent-staging-xyz")),
    ("agent_takkub.orchestrator.default_cwd_for_role", FAKE_CWD),
]


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def _spawn_capture_env(orch: Orchestrator, role_name: str) -> dict[str, str]:
    """Spawn *role_name* with blocking I/O mocked; return the env dict passed to PtySession.spawn."""
    captured: dict[str, str] = {}

    mock_session = MagicMock()
    mock_session.processExited = MagicMock()
    mock_session.is_alive = True

    def _capture_spawn(argv, cwd, env, transcript_path=None):
        captured.update(env)

    mock_session.spawn = _capture_spawn

    pane = MagicMock()
    pane.session = None
    orch._panes_by_project.setdefault(TEST_PROJECT, {})[role_name] = pane

    with ExitStack() as stack:
        for target, val in _COMMON_PATCHES:
            stack.enter_context(patch(target, return_value=val))
        stack.enter_context(
            patch("agent_takkub.orchestrator.PtySession", return_value=mock_session)
        )
        stack.enter_context(patch.object(orch, "_auto_trust"))
        ok, msg = orch.spawn(role_name, cwd=FAKE_CWD, project=TEST_PROJECT)

    assert ok, f"spawn({role_name!r}) unexpectedly failed: {msg}"
    return captured


class TestClaudeTeammateEnvLeak:
    """Non-lead Claude panes must not receive secret env vars from the cockpit."""

    def test_teammate_excludes_anthropic_api_key(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthro")
        env = _spawn_capture_env(orch, "backend")
        assert "ANTHROPIC_API_KEY" not in env

    def test_teammate_excludes_openai_api_key(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "fake-openai")
        env = _spawn_capture_env(orch, "frontend")
        assert "OPENAI_API_KEY" not in env

    def test_teammate_excludes_gh_token(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "fake-gh")
        env = _spawn_capture_env(orch, "qa")
        assert "GH_TOKEN" not in env

    def test_teammate_excludes_aws_access_key_id(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake-aws")
        env = _spawn_capture_env(orch, "mobile")
        assert "AWS_ACCESS_KEY_ID" not in env


class TestClaudeLeadEnvFiltered:
    """Lead pane uses _build_lead_env() allowlist — secrets not in the list are filtered."""

    def test_lead_excludes_anthropic_api_key(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthro")
        env = _spawn_capture_env(orch, "lead")
        assert "ANTHROPIC_API_KEY" not in env

    def test_lead_excludes_openai_api_key(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "fake-openai")
        env = _spawn_capture_env(orch, "lead")
        assert "OPENAI_API_KEY" not in env

    def test_lead_excludes_aws_access_key_id(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake-aws")
        env = _spawn_capture_env(orch, "lead")
        assert "AWS_ACCESS_KEY_ID" not in env

    def test_lead_retains_gh_token(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GH_TOKEN is in _LEAD_ENV_EXTRA_ALLOWLIST — Lead needs it for gh CLI."""
        monkeypatch.setenv("GH_TOKEN", "fake-gh")
        env = _spawn_capture_env(orch, "lead")
        assert "GH_TOKEN" in env
        assert env["GH_TOKEN"] == "fake-gh"

    def test_lead_retains_github_token(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "fake-github")
        env = _spawn_capture_env(orch, "lead")
        assert "GITHUB_TOKEN" in env
        assert env["GITHUB_TOKEN"] == "fake-github"

    def test_lead_retains_base_allowlist_keys(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lead env must include PATH and other base allowlist keys."""
        env = _spawn_capture_env(orch, "lead")
        assert "PATH" in env


class TestClaudeSpawnEnvRegressions:
    """TAKKUB_ROLE and TAKKUB_PROJECT must always be present regardless of role."""

    def test_teammate_has_takkub_role(self, orch: Orchestrator) -> None:
        env = _spawn_capture_env(orch, "backend")
        assert "TAKKUB_ROLE" in env
        assert env["TAKKUB_ROLE"] == "backend"

    def test_lead_has_takkub_role(self, orch: Orchestrator) -> None:
        env = _spawn_capture_env(orch, "lead")
        assert "TAKKUB_ROLE" in env
        assert env["TAKKUB_ROLE"] == "lead"

    def test_teammate_has_takkub_project(self, orch: Orchestrator) -> None:
        env = _spawn_capture_env(orch, "backend")
        assert "TAKKUB_PROJECT" in env
        assert env["TAKKUB_PROJECT"] == TEST_PROJECT

    def test_lead_has_takkub_project(self, orch: Orchestrator) -> None:
        env = _spawn_capture_env(orch, "lead")
        assert "TAKKUB_PROJECT" in env
        assert env["TAKKUB_PROJECT"] == TEST_PROJECT


class TestBuildLeadEnvUnit:
    """Unit tests for _build_lead_env() — allowlist semantics, no spawn needed."""

    def test_filters_anthropic_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub.orchestrator import _build_lead_env

        monkeypatch.setenv("ANTHROPIC_API_KEY", "should-be-filtered")
        env = _build_lead_env()
        assert "ANTHROPIC_API_KEY" not in env

    def test_filters_openai_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub.orchestrator import _build_lead_env

        monkeypatch.setenv("OPENAI_API_KEY", "should-be-filtered")
        env = _build_lead_env()
        assert "OPENAI_API_KEY" not in env

    def test_retains_gh_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub.orchestrator import _build_lead_env

        monkeypatch.setenv("GH_TOKEN", "my-gh-token")
        env = _build_lead_env()
        assert "GH_TOKEN" in env
        assert env["GH_TOKEN"] == "my-gh-token"

    def test_retains_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub.orchestrator import _build_lead_env

        env = _build_lead_env()
        assert "PATH" in env

    def test_custom_key_via_takkub_lead_env_allow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub.orchestrator import _build_lead_env

        monkeypatch.setenv("MY_CUSTOM_KEY", "custom-value")
        monkeypatch.setenv("TAKKUB_LEAD_ENV_ALLOW", "MY_CUSTOM_KEY")
        env = _build_lead_env()
        assert "MY_CUSTOM_KEY" in env
        assert env["MY_CUSTOM_KEY"] == "custom-value"

    def test_custom_key_not_present_without_allow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_takkub.orchestrator import _build_lead_env

        monkeypatch.setenv("MY_CUSTOM_KEY", "custom-value")
        monkeypatch.delenv("TAKKUB_LEAD_ENV_ALLOW", raising=False)
        env = _build_lead_env()
        assert "MY_CUSTOM_KEY" not in env

    def test_gh_token_not_in_build_pane_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GH_TOKEN is Lead-only — must not leak into teammate env."""
        from agent_takkub.orchestrator import _build_pane_env

        monkeypatch.setenv("GH_TOKEN", "fake-gh")
        env = _build_pane_env()
        assert "GH_TOKEN" not in env


class TestTakkubLeadTokenIsolation:
    """TAKKUB_LEAD_TOKEN must never be inherited by teammate panes from the parent
    shell env; Lead pane must always receive the orchestrator-generated token."""

    def test_build_pane_env_drops_parent_takkub_lead_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_build_pane_env() must not include TAKKUB_LEAD_TOKEN even when the
        parent process has it set (e.g. nested cockpit or stale shell)."""

        from agent_takkub.orchestrator import _build_pane_env

        monkeypatch.setenv("TAKKUB_LEAD_TOKEN", "parent-leak-token")
        env = _build_pane_env()
        assert "TAKKUB_LEAD_TOKEN" not in env

    def test_lead_spawn_injects_orch_token(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lead spawn must inject the orchestrator's own token, not any parent leak."""
        monkeypatch.setenv("TAKKUB_LEAD_TOKEN", "parent-leak-token")
        env = _spawn_capture_env(orch, "lead")
        assert "TAKKUB_LEAD_TOKEN" in env
        assert env["TAKKUB_LEAD_TOKEN"] == orch._lead_token
        assert env["TAKKUB_LEAD_TOKEN"] != "parent-leak-token"

    def test_teammate_spawn_excludes_takkub_lead_token(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Teammate panes must not receive TAKKUB_LEAD_TOKEN even if parent env has it."""
        monkeypatch.setenv("TAKKUB_LEAD_TOKEN", "parent-leak-token")
        for role in ("backend", "qa", "frontend"):
            env = _spawn_capture_env(orch, role)
            assert "TAKKUB_LEAD_TOKEN" not in env, (
                f"role={role!r} leaked TAKKUB_LEAD_TOKEN into spawn env"
            )
