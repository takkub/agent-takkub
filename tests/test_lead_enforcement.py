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
        _pane_state={"project::lead": SimpleNamespace(lead_enforcement_mode="none")},
    )

    reason = Orchestrator._lead_restriction_transition_block(fake, "project")

    assert "close the active unrestricted Lead" in reason
    fake._pane_state["project::lead"].lead_enforcement_mode = "codex_sandbox"
    assert Orchestrator._lead_restriction_transition_block(fake, "project") == ""
