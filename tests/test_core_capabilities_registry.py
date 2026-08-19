"""core.capabilities.registry.CapabilityRegistry — WRAPs skill_policy +
pane_tools_policy + mcp_bridge (Phase 5b, epic #309); asserts delegation,
not the wrapped modules' own logic (that's covered by their own tests)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import mcp_bridge, pane_tools_policy, skill_policy
from agent_takkub.core.capabilities.registry import CapabilityRegistry
from agent_takkub.core.models.capability import CapabilityScope, Skill


@pytest.fixture
def registry() -> CapabilityRegistry:
    return CapabilityRegistry()


def test_resolve_skills_returns_empty_when_role_has_no_assignment(
    monkeypatch: pytest.MonkeyPatch, registry: CapabilityRegistry
) -> None:
    monkeypatch.setattr(skill_policy, "effective_skills", lambda role: [])

    assert registry.resolve_skills("backend", [Path("/nowhere")]) == ()


def test_resolve_skills_filters_to_assigned_and_existing(
    monkeypatch: pytest.MonkeyPatch, registry: CapabilityRegistry, tmp_path: Path
) -> None:
    skills_dir = tmp_path / ".claude" / "skills" / "debug-mantra"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: debug-mantra\ndescription: four mantras\n---\nbody", encoding="utf-8"
    )
    monkeypatch.setattr(
        skill_policy, "effective_skills", lambda role: ["debug-mantra", "not-installed"]
    )

    result = registry.resolve_skills("backend", [tmp_path])

    assert result == (
        Skill(
            id="debug-mantra",
            name="debug-mantra",
            description="four mantras",
            scope=CapabilityScope.PROJECT,
        ),
    )


def test_resolve_mcp_server_names_delegates_to_pane_tools_policy(
    monkeypatch: pytest.MonkeyPatch, registry: CapabilityRegistry
) -> None:
    monkeypatch.setattr(
        pane_tools_policy, "effective_mcps", lambda role, default=None: frozenset({"graft"})
    )

    assert registry.resolve_mcp_server_names("backend") == frozenset({"graft"})


def test_resolve_mcp_server_names_passthrough_none(
    monkeypatch: pytest.MonkeyPatch, registry: CapabilityRegistry
) -> None:
    monkeypatch.setattr(pane_tools_policy, "effective_mcps", lambda role, default=None: None)

    assert registry.resolve_mcp_server_names("backend") is None


def test_resolve_plugin_names_delegates_to_pane_tools_policy(
    monkeypatch: pytest.MonkeyPatch, registry: CapabilityRegistry
) -> None:
    monkeypatch.setattr(
        pane_tools_policy,
        "effective_plugins",
        lambda role, default=None: frozenset({"superpowers"}),
    )

    assert registry.resolve_plugin_names("backend") == frozenset({"superpowers"})


def test_mcp_argv_for_provider_delegates_to_mcp_bridge(
    monkeypatch: pytest.MonkeyPatch, registry: CapabilityRegistry
) -> None:
    calls: list[tuple] = []

    def fake_mcp_argv_for_provider(provider_name, base_role, shard_idx, project_ns, **kw):
        calls.append((provider_name, base_role, shard_idx, project_ns, kw))
        return ["--mcp-config", "/tmp/x.json"]

    monkeypatch.setattr(mcp_bridge, "mcp_argv_for_provider", fake_mcp_argv_for_provider)

    result = registry.mcp_argv_for_provider("claude", "backend", None, "proj", cwd="/x")

    assert result == ["--mcp-config", "/tmp/x.json"]
    assert calls == [("claude", "backend", None, "proj", {"cwd": "/x"})]


def test_snapshot_bundles_skills_mcps_and_plugins(
    monkeypatch: pytest.MonkeyPatch, registry: CapabilityRegistry, tmp_path: Path
) -> None:
    monkeypatch.setattr(skill_policy, "effective_skills", lambda role: [])
    monkeypatch.setattr(
        pane_tools_policy, "effective_mcps", lambda role, default=None: frozenset({"b", "a"})
    )
    monkeypatch.setattr(
        pane_tools_policy, "effective_plugins", lambda role, default=None: frozenset()
    )

    snap = registry.snapshot("backend", [tmp_path])

    assert snap.role == "backend"
    assert snap.scope == CapabilityScope.PROJECT
    assert snap.skills == ()
    assert snap.mcp_server_names == ("a", "b")
    assert snap.plugin_names == ()
