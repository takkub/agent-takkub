"""#572: `takkub assign`/`spawn` must not resolve a role onto a provider
that's currently recorded quota-hit (`provider_state.set_quota_reset_at`) —
before this fix, `_assign_dispatch` (the one chokepoint the normal AND
worktree assign paths both route through, `orchestrator.py`) called
`provider_config.effective_provider_for` with no quota awareness at all, so
a codex-mapped role kept resolving straight onto codex even while codex was
still mid-window: a full pane boot (~70k tokens), immediately followed by
the #514 post-hit reroute once codex's own rate-limit banner appeared.

These tests exercise the real `Orchestrator.assign()` → `_assign_dispatch`
path (with `Orchestrator.spawn` short-circuited so no real PTY/subprocess is
ever created — the resolution + notice logic under test all runs before
`spawn()` is reached) rather than re-testing `effective_provider_for` itself
(see `test_provider_config.py::TestEffectiveProviderForQuotaSkip` /
`TestProviderQuotaSkipInfo` for that unit-level coverage).
"""

from __future__ import annotations

import pathlib
import time
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config, provider_config
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator

_PROJECT = "default"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def tmp_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    cockpit = tmp_path / "cockpit"
    cockpit.mkdir(parents=True, exist_ok=True)
    (cockpit / "CLAUDE.md").write_text("# Lead\n", encoding="utf-8")
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "find_claude_executable", lambda: "claude")
    monkeypatch.setattr(provider_config, "_provider_available", lambda p: True)
    return tmp_path


@pytest.fixture
def orch(qapp: QCoreApplication, tmp_env: pathlib.Path) -> Orchestrator:
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _assign(orch: Orchestrator, tmp_env: pathlib.Path, role: str, **kwargs):
    cwd = str(tmp_env / "workdir")
    pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
    with patch.object(Orchestrator, "spawn", return_value=(True, "spawned")):
        with patch.object(Orchestrator, "_notify_lead") as mock_notify:
            with patch.object(orch_mod, "_log_event") as mock_log:
                ok, msg = orch.assign(role, cwd, "do the thing", project=_PROJECT, **kwargs)
    return ok, msg, mock_notify, mock_log


class TestAssignSkipsQuotaHitProvider:
    def test_role_mapped_to_quota_hit_provider_logs_and_notifies_once(
        self, orch: Orchestrator, tmp_env: pathlib.Path, redirect_config_path: pathlib.Path
    ) -> None:
        from agent_takkub import provider_state

        redirect_config_path.write_text('{"reviewer": "codex"}', encoding="utf-8")
        reset_at = time.time() + 3600
        provider_state.set_quota_reset_at("codex", reset_at)

        ok, _msg, mock_notify, mock_log = _assign(orch, tmp_env, "reviewer")

        assert ok is True
        skip_events = [
            c for c in mock_log.call_args_list if c.args and c.args[0] == "provider_quota_skip"
        ]
        assert len(skip_events) == 1
        assert skip_events[0].kwargs["from_provider"] == "codex"
        assert skip_events[0].kwargs["to_provider"] == "claude"

        skip_notices = [
            c for c in mock_notify.call_args_list if c.kwargs.get("note") == "provider_quota_skip"
        ]
        assert len(skip_notices) == 1

    def test_role_on_ready_provider_never_skips(
        self, orch: Orchestrator, tmp_env: pathlib.Path, redirect_config_path: pathlib.Path
    ) -> None:
        redirect_config_path.write_text('{"reviewer": "codex"}', encoding="utf-8")
        # No quota state recorded at all → codex is ready.

        ok, _msg, _mock_notify, mock_log = _assign(orch, tmp_env, "reviewer")

        assert ok is True
        skip_events = [
            c for c in mock_log.call_args_list if c.args and c.args[0] == "provider_quota_skip"
        ]
        assert skip_events == []

    def test_forced_identity_role_is_never_skipped(
        self, orch: Orchestrator, tmp_env: pathlib.Path
    ) -> None:
        """The "codex" role's whole identity IS codex — #572 must not
        reroute it away; it still spawns onto codex and the existing
        park-on-hit behaviour (#514, unchanged) takes over once it's
        actually running."""
        from agent_takkub import provider_state

        provider_state.set_quota_reset_at("codex", time.time() + 3600)

        ok, _msg, mock_notify, mock_log = _assign(orch, tmp_env, "codex")

        assert ok is True
        skip_events = [
            c for c in mock_log.call_args_list if c.args and c.args[0] == "provider_quota_skip"
        ]
        assert skip_events == []
        skip_notices = [
            c for c in mock_notify.call_args_list if c.kwargs.get("note") == "provider_quota_skip"
        ]
        assert skip_notices == []

    def test_explicit_provider_override_is_never_skipped(
        self, orch: Orchestrator, tmp_env: pathlib.Path, redirect_config_path: pathlib.Path
    ) -> None:
        """An explicit --provider (#270) is always honoured — #572's
        automatic skip logic must not touch this branch (it gets its own
        separate, non-blocking warning — see
        `test_provider_config.py::TestAssignProviderOverrideWarning`)."""
        from agent_takkub import provider_state

        redirect_config_path.write_text('{"reviewer": "claude"}', encoding="utf-8")
        provider_state.set_quota_reset_at("codex", time.time() + 3600)

        ok, _msg, mock_notify, mock_log = _assign(orch, tmp_env, "reviewer", provider="codex")

        assert ok is True
        skip_events = [
            c for c in mock_log.call_args_list if c.args and c.args[0] == "provider_quota_skip"
        ]
        assert skip_events == []
        skip_notices = [
            c for c in mock_notify.call_args_list if c.kwargs.get("note") == "provider_quota_skip"
        ]
        assert skip_notices == []


@pytest.fixture
def redirect_config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Same shape as `test_provider_config.py`'s fixture of the same name —
    duplicated locally rather than shared across test files (no conftest
    cross-import convention in this suite for fixtures this narrow)."""
    fake = tmp_path / "role-providers.json"
    monkeypatch.setattr(provider_config, "_BASE_DIR", tmp_path)
    provider_config.reset_provider_available_cache()
    return fake
