"""core.capabilities.design_integrations — #365 phase 7: real Storybook
detection + the opt-in-only 21st.dev/Figma/Penpot registry stubs, gated
through the SAME `pane_tools_policy` layer `PermissionEngine` reads (no
separate opt-in flag, no bypass)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import pane_tools_policy
from agent_takkub.core.capabilities.design_integrations import (
    OPTIONAL_DESIGN_MCPS,
    detect_storybook,
    optional_design_mcp_status,
    resolve_design_integrations,
)
from agent_takkub.core.capabilities.permission_engine import PermissionEngine
from agent_takkub.core.capabilities.registry import CapabilityRegistry


class TestDetectStorybook:
    def test_no_roots_no_markers_not_detected(self, tmp_path: Path) -> None:
        status = detect_storybook([tmp_path])
        assert status.detected is False
        assert status.preview_url is None

    def test_config_dir_detected_with_default_port(self, tmp_path: Path) -> None:
        (tmp_path / ".storybook").mkdir()

        status = detect_storybook([tmp_path])

        assert status.detected is True
        assert status.root == str(tmp_path)
        assert status.port == 6006
        assert status.preview_url == "http://localhost:6006"

    def test_package_json_script_detected(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"storybook": "storybook dev -p 6007"}}),
            encoding="utf-8",
        )

        status = detect_storybook([tmp_path])

        assert status.detected is True
        assert status.script_name == "storybook"
        assert status.port == 6007
        assert status.preview_url == "http://localhost:6007"

    def test_malformed_package_json_does_not_raise(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{not json", encoding="utf-8")

        status = detect_storybook([tmp_path])

        assert status.detected is False

    def test_first_matching_root_wins(self, tmp_path: Path) -> None:
        empty_root = tmp_path / "a"
        empty_root.mkdir()
        hit_root = tmp_path / "b"
        hit_root.mkdir()
        (hit_root / ".storybook").mkdir()

        status = detect_storybook([empty_root, hit_root])

        assert status.detected is True
        assert status.root == str(hit_root)


class TestOptionalDesignMcpStatus:
    def test_default_off_for_every_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pane_tools_policy, "effective_mcps", lambda role, default=None: None)

        status = optional_design_mcp_status("designer")

        assert len(status) == len(OPTIONAL_DESIGN_MCPS)
        assert all(m.enabled_for_role is False for m in status)

    def test_opt_in_reflects_pane_tools_policy_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            pane_tools_policy,
            "effective_mcps",
            lambda role, default=None: frozenset({"figma"}),
        )

        status = {m.id: m for m in optional_design_mcp_status("designer")}

        assert status["figma"].enabled_for_role is True
        assert status["penpot"].enabled_for_role is False
        assert status["reference-21st"].enabled_for_role is False

    def test_agrees_with_permission_engine_no_second_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same underlying policy call backs both façades — enabling a
        design MCP can never disagree with what `PermissionEngine.
        mcp_allowed` (the real spawn-time gate) grants."""
        monkeypatch.setattr(
            pane_tools_policy,
            "effective_mcps",
            lambda role, default=None: frozenset({"penpot"}),
        )

        design_status = optional_design_mcp_status("designer")
        allowed = PermissionEngine().mcp_allowed("designer")

        assert allowed is not None and "penpot" in allowed
        assert next(m for m in design_status if m.id == "penpot").enabled_for_role is True


class TestResolveDesignIntegrations:
    def test_snapshot_bundles_storybook_and_mcps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".storybook").mkdir()
        monkeypatch.setattr(pane_tools_policy, "effective_mcps", lambda role, default=None: None)

        snapshot = resolve_design_integrations("designer", [tmp_path])

        assert snapshot.storybook.detected is True
        assert len(snapshot.optional_mcps) == len(OPTIONAL_DESIGN_MCPS)

    def test_capability_registry_delegates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pane_tools_policy, "effective_mcps", lambda role, default=None: None)

        snapshot = CapabilityRegistry().resolve_design_integrations("designer", [tmp_path])

        assert snapshot.storybook.detected is False
