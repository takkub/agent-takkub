"""core.capabilities.design_integrations — #365 phase 7: real Storybook
detection; #373: real 21st.dev/Figma/Penpot clients, both gated through the
SAME `pane_tools_policy` layer `PermissionEngine` reads (no separate opt-in
flag, no bypass)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import pane_tools_policy
from agent_takkub.core.capabilities.design_clients import (
    FigmaClient,
    PenpotClient,
    TwentyFirstClient,
)
from agent_takkub.core.capabilities.design_integrations import (
    OPTIONAL_DESIGN_MCPS,
    IntegrationDeniedError,
    IntegrationNotConfiguredError,
    build_client,
    detect_storybook,
    integration_config_status,
    optional_design_mcp_status,
    register_twentyfirst_mcp,
    resolve_design_integrations,
)
from agent_takkub.core.capabilities.permission_engine import PermissionEngine
from agent_takkub.core.capabilities.registry import CapabilityRegistry
from agent_takkub.core.secrets.backends import BackendStatus, SecretUnavailableError


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

    def test_provider_off_designer_workflow_still_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every optional MCP denied for the role — the existing #365
        Designer flow (Storybook detection + snapshot shape) must keep
        working unchanged; #373 adds new opt-in surface, it must never make
        the old default-off path fail or change shape."""
        (tmp_path / ".storybook").mkdir()
        monkeypatch.setattr(
            pane_tools_policy, "effective_mcps", lambda role, default=None: frozenset()
        )

        snapshot = resolve_design_integrations("designer", [tmp_path])

        assert snapshot.storybook.detected is True
        assert all(m.enabled_for_role is False for m in snapshot.optional_mcps)


class _FakeSecretManager:
    """Minimal `SecretManager`-shaped fake — `get_secret`/`status` only,
    matching exactly what `build_client`/`integration_config_status` call."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def status(self, secret_ref: str) -> BackendStatus:
        return BackendStatus.FOUND if secret_ref in self._values else BackendStatus.MISSING

    def get_secret(self, secret_ref: str) -> str:
        try:
            return self._values[secret_ref]
        except KeyError:
            raise SecretUnavailableError(f"no secret for {secret_ref}") from None


class _FakePermissionEngine:
    def __init__(self, allowed: frozenset[str] | None) -> None:
        self._allowed = allowed

    def mcp_allowed(self, role: str) -> frozenset[str] | None:
        return self._allowed


class TestIntegrationConfigStatus:
    def test_configured_when_secret_found(self) -> None:
        manager = _FakeSecretManager({"secret://figma/default": "tok"})

        configured, detail = integration_config_status("figma", secret_manager=manager)

        assert configured is True
        assert "configured" in detail

    def test_not_configured_when_secret_missing(self) -> None:
        manager = _FakeSecretManager({})

        configured, detail = integration_config_status("figma", secret_manager=manager)

        assert configured is False
        assert "no credential" in detail


class TestBuildClient:
    def test_unknown_integration_id_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            build_client("not-a-real-one", "designer")

    def test_denied_when_permission_engine_says_no(self) -> None:
        with pytest.raises(IntegrationDeniedError):
            build_client(
                "figma",
                "designer",
                permission_engine=_FakePermissionEngine(frozenset()),
                secret_manager=_FakeSecretManager({"secret://figma/default": "tok"}),
            )

    def test_denied_when_no_policy_at_all_none_is_not_a_free_pass(self) -> None:
        """`None` from `mcp_allowed` means legacy master passthrough for
        the SHELL/MCP-spawn gate — but a #373 design integration must stay
        default-deny even then; this is the one place `None` must NOT be
        treated as "everything allowed"."""
        with pytest.raises(IntegrationDeniedError):
            build_client(
                "figma",
                "designer",
                permission_engine=_FakePermissionEngine(None),
                secret_manager=_FakeSecretManager({"secret://figma/default": "tok"}),
            )

    def test_not_configured_when_allowed_but_no_secret(self) -> None:
        with pytest.raises(IntegrationNotConfiguredError):
            build_client(
                "figma",
                "designer",
                permission_engine=_FakePermissionEngine(frozenset({"figma"})),
                secret_manager=_FakeSecretManager({}),
            )

    def test_figma_client_built_from_bare_token(self) -> None:
        client = build_client(
            "figma",
            "designer",
            permission_engine=_FakePermissionEngine(frozenset({"figma"})),
            secret_manager=_FakeSecretManager({"secret://figma/default": "  tok  "}),
        )

        assert isinstance(client, FigmaClient)

    def test_reference_21st_client_built_from_json_blob(self) -> None:
        secret = json.dumps({"api_key": "k", "base_url": "https://proxy.example.test"})
        client = build_client(
            "reference-21st",
            "designer",
            permission_engine=_FakePermissionEngine(frozenset({"reference-21st"})),
            secret_manager=_FakeSecretManager({"secret://reference-21st/default": secret}),
        )

        assert isinstance(client, TwentyFirstClient)

    def test_reference_21st_client_falls_back_to_bare_token(self) -> None:
        """A bare (non-JSON) stored value is still accepted as the API key
        — matches figma's bare-token contract, just without a base_url."""
        client = build_client(
            "reference-21st",
            "designer",
            permission_engine=_FakePermissionEngine(frozenset({"reference-21st"})),
            secret_manager=_FakeSecretManager({"secret://reference-21st/default": "bare-key"}),
        )

        assert isinstance(client, TwentyFirstClient)

    def test_penpot_client_requires_both_token_and_base_url(self) -> None:
        secret = json.dumps({"token": "tok"})  # missing base_url
        with pytest.raises(IntegrationNotConfiguredError):
            build_client(
                "penpot",
                "designer",
                permission_engine=_FakePermissionEngine(frozenset({"penpot"})),
                secret_manager=_FakeSecretManager({"secret://penpot/default": secret}),
            )

    def test_penpot_client_built_when_fully_configured(self) -> None:
        secret = json.dumps({"token": "tok", "base_url": "https://design.example.test"})
        client = build_client(
            "penpot",
            "designer",
            permission_engine=_FakePermissionEngine(frozenset({"penpot"})),
            secret_manager=_FakeSecretManager({"secret://penpot/default": secret}),
        )

        assert isinstance(client, PenpotClient)

    def test_denied_path_never_touches_secret_manager(self) -> None:
        """Permission check must short-circuit before any credential
        lookup — a denied role should never even reveal whether a
        credential happens to be configured."""
        calls: list[str] = []

        class _WatchedSecretManager(_FakeSecretManager):
            def get_secret(self, secret_ref: str) -> str:
                calls.append(secret_ref)
                return super().get_secret(secret_ref)

        with pytest.raises(IntegrationDeniedError):
            build_client(
                "figma",
                "designer",
                permission_engine=_FakePermissionEngine(frozenset()),
                secret_manager=_WatchedSecretManager({"secret://figma/default": "tok"}),
            )

        assert calls == []


class TestRegisterTwentyfirstMcp:
    def test_registers_with_placeholder_not_literal_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The MCP config written to the shared registry must reference
        the API key via `${VAR}`, never embed a literal secret in a file
        every pane's `--mcp-config` reads."""
        from agent_takkub import shared_dev_tools as sdt

        seen: dict = {}
        monkeypatch.setattr(
            sdt,
            "add_mcp_server",
            lambda name, cfg, force=False: seen.update(name=name, cfg=cfg, force=force) or True,
        )

        ok = register_twentyfirst_mcp()

        assert ok is True
        assert seen["name"] == "reference-21st"
        assert seen["force"] is True
        assert "${TWENTY_FIRST_API_KEY}" in " ".join(seen["cfg"]["args"])
        assert seen["cfg"]["command"] == "npx"
