"""Security checks for restricted Lead spawn preparation (#842)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_takkub import lead_enforcement
from agent_takkub.orchestrator import Orchestrator


def test_legacy_sandbox_config_refuses_restricted_spawn(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'sandbox_mode = "danger-full-access"\n', encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="legacy sandbox configuration"):
        lead_enforcement.verify_codex_boundary(
            "codex", str(tmp_path), {"CODEX_HOME": str(codex_home)}, [str(tmp_path)]
        )


def test_write_grant_cannot_point_into_project_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    source = project_root / "src"
    monkeypatch.setattr("agent_takkub.config.RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(
        "agent_takkub.config._project_dict",
        lambda _project: {"paths": {"main": str(project_root)}},
    )
    with pytest.raises(ValueError, match="overlaps a project root"):
        lead_enforcement.lead_write_roots("project", {"TAKKUB_DOCS_DIR": str(source)})
    assert not source.exists()


def test_live_unrestricted_lead_blocks_policy_tightening() -> None:
    session = SimpleNamespace(is_alive=True)
    pane = SimpleNamespace(session=session)
    fake = SimpleNamespace(
        _resolve_project=lambda project: project,
        _panes_by_project={"project": {"lead": pane}},
        _pane_state={
            "project::lead": SimpleNamespace(lead_enforcement_mode="none", session_provider="codex")
        },
    )

    reason = Orchestrator._lead_restriction_transition_block(fake, "project")

    assert "close the active unrestricted Lead" in reason
    fake._pane_state["project::lead"].lead_enforcement_mode = "codex_sandbox"
    assert Orchestrator._lead_restriction_transition_block(fake, "project") == ""


def test_live_claude_lead_may_tighten_without_restart() -> None:
    # Claude's Lead edit guard is a PreToolUse hook that reads the preset live.
    pane = SimpleNamespace(
        session=SimpleNamespace(is_alive=True), model=SimpleNamespace(provider_name="claude")
    )
    fake = SimpleNamespace(
        _resolve_project=lambda project: project,
        _panes_by_project={"project": {"lead": pane}},
        _pane_state={"project::lead": SimpleNamespace(lead_enforcement_mode="none")},
    )

    assert Orchestrator._lead_restriction_transition_block(fake, "project") == ""
    pane.model.provider_name = "gemini"
    assert "close the active unrestricted Lead" in Orchestrator._lead_restriction_transition_block(
        fake, "project"
    )


def test_boundary_probe_pass_is_cached_until_binary_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#871: the three blocking `codex` probes ran on the Qt thread on every
    restricted Lead spawn (6 s freeze). A pass is reused; a changed binary,
    config file or expired TTL probes again; a failure is never cached."""
    codex = tmp_path / "codex.exe"
    codex.write_bytes(b"v1")
    home = tmp_path / "home"
    home.mkdir()
    env = {"CODEX_HOME": str(home)}
    calls: list[str] = []
    monkeypatch.setattr(lead_enforcement, "_VERIFIED", {})
    monkeypatch.setattr(
        lead_enforcement, "_run_codex_boundary_probes", lambda *a: calls.append("probe")
    )
    verify = lambda: lead_enforcement.verify_codex_boundary(  # noqa: E731
        str(codex), str(tmp_path), env, [str(tmp_path)]
    )
    verify()
    verify()
    assert calls == ["probe"]

    codex.write_bytes(b"v2-upgraded")
    verify()
    assert calls == ["probe", "probe"]

    (home / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
    verify()
    assert len(calls) == 3

    monkeypatch.setattr(lead_enforcement, "_VERIFIED_TTL_S", 0.0)
    verify()
    assert len(calls) == 4

    def _fail(*_a):
        raise RuntimeError("Codex sandbox allowed a write outside scoped roots")

    monkeypatch.setattr(lead_enforcement, "_VERIFIED_TTL_S", 3600.0)
    monkeypatch.setattr(lead_enforcement, "_VERIFIED", {})
    monkeypatch.setattr(lead_enforcement, "_run_codex_boundary_probes", _fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="outside scoped roots"):
            verify()


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_windows_lead_sandbox_is_unelevated(monkeypatch: pytest.MonkeyPatch, platform) -> None:
    """Codex's default elevated Windows sandbox runs an admin setup (UAC
    popup) for every fresh CODEX_HOME — the cockpit's own home made that hit
    a Codex Lead's first open on a new machine. Windows must ask for the
    unelevated sandbox; other platforms must not get a Windows-only key."""
    monkeypatch.setattr(lead_enforcement.sys, "platform", platform)
    argv = lead_enforcement.codex_lead_argv(["C:/scratch"])
    has_key = 'windows.sandbox="unelevated"' in argv
    assert has_key is (platform == "win32")
    assert argv[:2] == ["--ask-for-approval", "never"]
