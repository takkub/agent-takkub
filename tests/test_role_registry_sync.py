"""Integration coverage for the role-registry single-source-of-truth fix.

Simulates the exact "New Role" flow — `custom_roles.create_role()` +
`roles.register_role()`, the same two calls `settings_window`'s
`_on_create_role_clicked` makes so a freshly created role is spawnable
without a cockpit restart — and asserts the new role shows up on every
surface that lists "every role", the audit this test backs:
`pipeline_config` (Pipeline Builder palette + Providers & Roles),
`pane_tools_dialog` (MCP/Plugins matrix rows), `pane_tools_policy` (CLI
`takkub mcp/plugins allow|deny`), `settings_window`'s own role-list
functions, and `skill_audit` (Skill Catalog).
"""

from __future__ import annotations

import pytest

from agent_takkub import (
    cockpit_theme,
    config,
    custom_roles,
    pane_tools_dialog,
    pane_tools_policy,
    pipeline_config,
    roles,
    settings_window,
    skill_audit,
)


@pytest.fixture
def new_custom_role(tmp_path, monkeypatch):
    """Create a real custom role exactly the way the New Role dialog does,
    then unregister it again so state never leaks into other tests."""
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(config, "CUSTOM_AGENTS_DIR", tmp_path / "agents")

    name = "sales-sync-test"
    ok, err = custom_roles.create_role(name, "Sales", "#112233", 1, 7, "custom role instructions")
    assert ok, err
    role = custom_roles.load_custom_roles()[name]
    roles.register_role(role)
    try:
        yield name
    finally:
        roles.unregister_role(name)


def test_appears_in_pipeline_builder_palette(new_custom_role):
    assert new_custom_role in pipeline_config.valid_roles()
    assert new_custom_role in settings_window._pipeline_palette_roles()


def test_appears_in_providers_roles_view(new_custom_role):
    assert new_custom_role in settings_window._overridable_roles()


def test_appears_in_mcp_plugins_matrix(new_custom_role):
    assert new_custom_role in pane_tools_dialog.matrix_roles()
    assert new_custom_role in settings_window._matrix_roles()


def test_appears_in_pane_tools_known_roles_for_cli(new_custom_role):
    assert new_custom_role in pane_tools_policy.known_roles()
    assert new_custom_role in pane_tools_policy.known_roles_base()


def test_appears_in_skill_catalog(new_custom_role):
    docs = skill_audit.load_all_role_docs()
    assert new_custom_role in docs


def test_lead_still_excluded_from_pipeline_eligible_roles(new_custom_role):
    # Intentional filter (not a sync bug): Lead is the coordinator seat, not
    # a pipeline participant — must hold regardless of custom-role state.
    assert "lead" not in pipeline_config.valid_roles()
    assert "lead" not in settings_window._pipeline_palette_roles()


def test_shell_still_excluded_from_pipeline_palette_and_matrix(new_custom_role):
    # Intentional filters: shell is an ad-hoc terminal pane (not a directed
    # pipeline participant, no --mcp-config ever loaded for it). Note
    # pipeline_config.valid_roles() itself still includes "shell" (a
    # pre-existing quirk left unchanged by this fix — the palette/matrix
    # layers are what actually filter it out; see their own docstrings).
    assert "shell" not in settings_window._pipeline_palette_roles()
    assert "shell" not in pane_tools_dialog.matrix_roles()


def test_unregistering_role_drops_it_from_every_surface(tmp_path, monkeypatch):
    monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
    monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
    name = "temp-role-sync-test"
    ok, err = custom_roles.create_role(name, "Temp", "#112233", 2, 9, "x")
    assert ok, err
    role = custom_roles.load_custom_roles()[name]
    roles.register_role(role)
    assert name in pipeline_config.valid_roles()
    assert name in pane_tools_dialog.matrix_roles()

    roles.unregister_role(name)
    assert name not in pipeline_config.valid_roles()
    assert name not in pane_tools_dialog.matrix_roles()


def test_builtin_role_colors_mirror_cockpit_theme_role_colors():
    """Single-source-of-truth guard for role identity color (2026-07-11 UI
    migration): roles.py Role.color is duplicated as plain hex (pure-leaf, no
    PyQt6 import) so it MUST equal cockpit_theme.ROLE_COLORS for every built-in
    role listed there. A drift here is exactly the "grid vs Settings render
    different role hues" bug the reconciliation fixed."""
    for role in roles.ALL_DEFAULT:
        canonical = cockpit_theme.ROLE_COLORS.get(role.name)
        if canonical is None:
            continue  # a built-in with no design-system entry keeps its own hex
        assert role.color == canonical, (
            f"role '{role.name}' color {role.color} != ROLE_COLORS {canonical} — "
            "update BOTH roles.py and cockpit_theme.ROLE_COLORS together"
        )


def test_every_builtin_role_has_a_cockpit_theme_color():
    """Every built-in role should have a canonical ROLE_COLORS entry so no
    surface has to fall back to Role.color for a built-in (fallback is for
    custom roles only)."""
    for role in roles.ALL_DEFAULT:
        assert role.name in cockpit_theme.ROLE_COLORS, (
            f"built-in role '{role.name}' missing from cockpit_theme.ROLE_COLORS"
        )


def test_pipeline_hop_with_custom_role_survives_save_load_roundtrip(
    new_custom_role, tmp_path, monkeypatch
):
    """The deeper bug behind the UI symptom: even if a picker showed the
    role, _norm_entry validated hop roles against a frozen import-time set
    that never included custom roles — saving a hop with one would have
    silently dropped it. Prove the round trip keeps it."""
    monkeypatch.setattr(pipeline_config, "_PATH", tmp_path / "pipelines.json")
    payload = pipeline_config.load()
    payload["templates"].append(
        {
            "id": "custom-tpl",
            "name": "Custom",
            "builtin": False,
            "hops": [
                [{"role": new_custom_role, "cwd": "", "requiresCommit": False, "autoChain": False}]
            ],
        }
    )
    pipeline_config.save(payload)

    reloaded = pipeline_config.load()
    custom_tpl = next(t for t in reloaded["templates"] if t["id"] == "custom-tpl")
    assert [e["role"] for e in custom_tpl["hops"][0]] == [new_custom_role]
    assert new_custom_role in reloaded["rolesEnabled"]
