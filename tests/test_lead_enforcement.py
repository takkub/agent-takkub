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


def test_live_claude_lead_blocks_tightening_unless_spawned_restricted() -> None:
    # Claude's live PreToolUse hook does not cover shell writes, so a Claude
    # Lead spawned under --dangerously-skip-permissions must restart too.
    pane = SimpleNamespace(
        session=SimpleNamespace(is_alive=True), model=SimpleNamespace(provider_name="claude")
    )
    fake = SimpleNamespace(
        _resolve_project=lambda project: project,
        _panes_by_project={"project": {"lead": pane}},
        _pane_state={"project::lead": SimpleNamespace(lead_enforcement_mode="none")},
    )

    assert "close the active unrestricted Lead" in Orchestrator._lead_restriction_transition_block(
        fake, "project"
    )
    fake._pane_state["project::lead"].lead_enforcement_mode = "claude_permissions"
    assert Orchestrator._lead_restriction_transition_block(fake, "project") == ""


def test_claude_rule_path_uses_root_anchored_posix_form() -> None:
    assert lead_enforcement._claude_rule_path(r"C:\Users\me\rt\docs") == "//c/Users/me/rt/docs"
    assert lead_enforcement._claude_rule_path("/Users/me/rt/docs") == "//Users/me/rt/docs"


def test_claude_lead_permissions_default_deny_shape() -> None:
    perms = lead_enforcement.claude_lead_permissions([r"C:\rt\docs", "/rt/lead-tmp/p"])

    assert perms["defaultMode"] == "dontAsk"
    assert perms["disableBypassPermissionsMode"] == "disable"
    assert perms["disableAutoMode"] == "disable"
    allow = perms["allow"]
    # Writes only under the cockpit-owned roots, via Edit rules (Claude Code
    # does not match Write(path) rules in its file checks).
    assert "Edit(//c/rt/docs/**)" in allow and "Edit(//rt/lead-tmp/p/**)" in allow
    assert not any(rule.startswith("Write(") for rule in allow)
    assert "Bash(takkub *)" in allow and "PowerShell(takkub *)" in allow
    # No interpreter, git or rtk passthrough is allowlisted.
    for rule in allow:
        assert not rule.startswith(("Bash(python", "Bash(git", "Bash(rtk", "Bash(*"))
    for cmd in ("takkub verify", "takkub worktree merge", "gh issue develop"):
        assert f"Bash({cmd})" in perms["deny"] and f"Bash({cmd} *)" in perms["deny"]


def test_claude_version_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(stdout: str, code: int = 0):
        return lambda *a, **k: SimpleNamespace(returncode=code, stdout=stdout)

    monkeypatch.setattr(lead_enforcement.subprocess, "run", fake_run("2.1.296 (Claude Code)"))
    lead_enforcement.verify_claude_version("claude", {})
    monkeypatch.setattr(lead_enforcement.subprocess, "run", fake_run("2.1.200 (Claude Code)"))
    with pytest.raises(RuntimeError, match="older than"):
        lead_enforcement.verify_claude_version("claude", {})
    monkeypatch.setattr(lead_enforcement.subprocess, "run", fake_run("", code=1))
    with pytest.raises(RuntimeError, match="cannot determine"):
        lead_enforcement.verify_claude_version("claude", {})
