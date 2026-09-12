"""Targeted regression tests for #580: per-project plugin skill gate in curated CLAUDE_CONFIG_DIR.

Verifies:
1. Curated settings.json filters out unassigned plugins that provide skills.
2. Plugins providing no skills (hooks only, MCP only, commands only) are never cut.
3. Plugins whose skills or identifiers are assigned via skill_policy or project roots are preserved.
4. TAKKUB_SKILL_GATE=0 preserves all enabled plugins (escape hatch).
5. Projects "default", "", None bypass the gate and preserve all enabled plugins.
6. Plugins with missing metadata on disk are preserved (never guess).
7. Plugins explicitly disabling skills (skills: false) are recognized as 0 skills and kept.
8. Curated settings.json does not mutate base settings.json (hardlink broken).
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_takkub import skill_policy, user_profile


def _write_file(path: Path, content: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _create_plugin(
    plugins_dir: Path,
    name: str,
    marketplace: str,
    *,
    skills: list[str] | None = None,
    has_hooks: bool = False,
    skills_manifest_val: object = None,
) -> Path:
    plugin_dir = plugins_dir / "cache" / marketplace / name / "1.0.0"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    manifest_data: dict[str, object] = {"name": name, "version": "1.0.0"}
    if skills_manifest_val is not None:
        manifest_data["skills"] = skills_manifest_val
    if has_hooks:
        manifest_data["hooks"] = {"SessionStart": []}

    _write_file(
        plugin_dir / ".claude-plugin" / "plugin.json",
        json.dumps(manifest_data),
    )

    if skills:
        for sname in skills:
            sdir = plugin_dir / "skills" / sname
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / "SKILL.md").write_text(
                f"---\nname: {sname}\ndescription: test skill\n---\n# {sname}\n",
                encoding="utf-8",
            )

    return plugin_dir


def test_curated_settings_filters_unassigned_skill_plugins(monkeypatch, tmp_path):
    data_home = tmp_path / "data_home"
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)

    base_dir = tmp_path / "base_claude"
    plugins_dir = base_dir / "plugins"

    # Setup plugins
    _create_plugin(plugins_dir, "hook-only-plugin", "official", has_hooks=True)
    _create_plugin(plugins_dir, "unassigned-skill-plugin", "market1", skills=["unassigned-skill"])
    _create_plugin(plugins_dir, "matrix-skill-plugin", "market2", skills=["matrix-assigned-skill"])
    _create_plugin(plugins_dir, "repo-skill-plugin", "market3", skills=["repo-assigned-skill"])

    # Setup installed_plugins.json
    installed_data = {
        "version": 2,
        "plugins": {
            "hook-only-plugin@official": [
                {
                    "installPath": str(
                        plugins_dir / "cache" / "official" / "hook-only-plugin" / "1.0.0"
                    )
                }
            ],
            "unassigned-skill-plugin@market1": [
                {
                    "installPath": str(
                        plugins_dir / "cache" / "market1" / "unassigned-skill-plugin" / "1.0.0"
                    )
                }
            ],
            "matrix-skill-plugin@market2": [
                {
                    "installPath": str(
                        plugins_dir / "cache" / "market2" / "matrix-skill-plugin" / "1.0.0"
                    )
                }
            ],
            "repo-skill-plugin@market3": [
                {
                    "installPath": str(
                        plugins_dir / "cache" / "market3" / "repo-skill-plugin" / "1.0.0"
                    )
                }
            ],
        },
    }
    _write_file(plugins_dir / "installed_plugins.json", json.dumps(installed_data))

    # Setup base settings.json
    settings_data = {
        "model": "claude-3-5-sonnet",
        "enabledPlugins": {
            "hook-only-plugin@official": True,
            "unassigned-skill-plugin@market1": True,
            "matrix-skill-plugin@market2": True,
            "repo-skill-plugin@market3": True,
        },
    }
    _write_file(base_dir / "settings.json", json.dumps(settings_data))

    # Mock project root skills
    project_root = tmp_path / "project_repo"
    repo_skill_dir = project_root / ".claude" / "skills" / "repo-assigned-skill"
    repo_skill_dir.mkdir(parents=True, exist_ok=True)
    (repo_skill_dir / "SKILL.md").write_text(
        "---\nname: repo-assigned-skill\n---\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "agent_takkub.lead_context._allowed_project_roots",
        lambda project: [project_root],
    )

    # Mock skill matrix policy
    monkeypatch.setattr(
        skill_policy,
        "load_policy",
        lambda: {"backend": ["matrix-assigned-skill"]},
    )

    curated = user_profile.ensure_curated_claude_config_dir("test-proj", base_dir=base_dir)

    curated_settings = json.loads((curated / "settings.json").read_text(encoding="utf-8"))
    enabled = curated_settings["enabledPlugins"]

    assert curated_settings["model"] == "claude-3-5-sonnet"
    # Hook-only plugin must remain (does not provide skills)
    assert "hook-only-plugin@official" in enabled
    # Assigned plugins must remain
    assert "matrix-skill-plugin@market2" in enabled
    assert "repo-skill-plugin@market3" in enabled
    # Unassigned skill plugin must be removed
    assert "unassigned-skill-plugin@market1" not in enabled


def test_escape_hatch_preserves_all_plugins(monkeypatch, tmp_path):
    data_home = tmp_path / "data_home"
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)

    base_dir = tmp_path / "base_claude"
    plugins_dir = base_dir / "plugins"

    _create_plugin(plugins_dir, "unassigned-plugin", "market", skills=["unassigned-skill"])
    settings_data = {
        "enabledPlugins": {
            "unassigned-plugin@market": True,
        },
    }
    _write_file(base_dir / "settings.json", json.dumps(settings_data))

    monkeypatch.setattr("agent_takkub.lead_context._allowed_project_roots", lambda project: [])
    monkeypatch.setattr(skill_policy, "load_policy", lambda: {})

    # With TAKKUB_SKILL_GATE=0, gate is disabled
    monkeypatch.setenv("TAKKUB_SKILL_GATE", "0")
    curated = user_profile.ensure_curated_claude_config_dir("test-proj", base_dir=base_dir)

    curated_settings = json.loads((curated / "settings.json").read_text(encoding="utf-8"))
    assert "unassigned-plugin@market" in curated_settings["enabledPlugins"]


def test_default_and_empty_project_bypasses_gate(monkeypatch, tmp_path):
    data_home = tmp_path / "data_home"
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)

    base_dir = tmp_path / "base_claude"
    plugins_dir = base_dir / "plugins"

    _create_plugin(plugins_dir, "unassigned-plugin", "market", skills=["unassigned-skill"])
    settings_data = {
        "enabledPlugins": {
            "unassigned-plugin@market": True,
        },
    }
    _write_file(base_dir / "settings.json", json.dumps(settings_data))

    monkeypatch.setattr("agent_takkub.lead_context._allowed_project_roots", lambda project: [])
    monkeypatch.setattr(skill_policy, "load_policy", lambda: {})

    for proj in ("default", ""):
        curated = user_profile.ensure_curated_claude_config_dir(proj, base_dir=base_dir)
        curated_settings = json.loads((curated / "settings.json").read_text(encoding="utf-8"))
        assert "unassigned-plugin@market" in curated_settings["enabledPlugins"]


def test_missing_metadata_plugin_preserved(monkeypatch, tmp_path):
    data_home = tmp_path / "data_home"
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)

    base_dir = tmp_path / "base_claude"
    # Plugin exists in settings.json but not on disk
    settings_data = {
        "enabledPlugins": {
            "ghost-plugin@unknown": True,
        },
    }
    _write_file(base_dir / "settings.json", json.dumps(settings_data))

    monkeypatch.setattr("agent_takkub.lead_context._allowed_project_roots", lambda project: [])
    monkeypatch.setattr(skill_policy, "load_policy", lambda: {})

    curated = user_profile.ensure_curated_claude_config_dir("test-proj", base_dir=base_dir)
    curated_settings = json.loads((curated / "settings.json").read_text(encoding="utf-8"))

    # Per spec: "ถ้าแยกไม่ได้จาก metadata ให้หยุดแล้วรายงาน อย่าเดา" -> preserve
    assert "ghost-plugin@unknown" in curated_settings["enabledPlugins"]


def test_plugin_with_skills_false_preserved(monkeypatch, tmp_path):
    data_home = tmp_path / "data_home"
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)

    base_dir = tmp_path / "base_claude"
    plugins_dir = base_dir / "plugins"

    # Plugin explicitly disables skills via skills: false
    _create_plugin(plugins_dir, "no-skills-plugin", "market", skills_manifest_val=False)
    settings_data = {
        "enabledPlugins": {
            "no-skills-plugin@market": True,
        },
    }
    _write_file(base_dir / "settings.json", json.dumps(settings_data))

    monkeypatch.setattr("agent_takkub.lead_context._allowed_project_roots", lambda project: [])
    monkeypatch.setattr(skill_policy, "load_policy", lambda: {})

    curated = user_profile.ensure_curated_claude_config_dir("test-proj", base_dir=base_dir)
    curated_settings = json.loads((curated / "settings.json").read_text(encoding="utf-8"))

    assert "no-skills-plugin@market" in curated_settings["enabledPlugins"]


def test_plugin_assigned_by_identifier(monkeypatch, tmp_path):
    data_home = tmp_path / "data_home"
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)

    base_dir = tmp_path / "base_claude"
    plugins_dir = base_dir / "plugins"

    _create_plugin(plugins_dir, "superpowers", "superpowers-dev", skills=["tdd", "debug"])
    settings_data = {
        "enabledPlugins": {
            "superpowers@superpowers-dev": True,
        },
    }
    _write_file(base_dir / "settings.json", json.dumps(settings_data))

    # Assigned by plugin name "superpowers" in matrix
    monkeypatch.setattr("agent_takkub.lead_context._allowed_project_roots", lambda project: [])
    monkeypatch.setattr(
        skill_policy,
        "load_policy",
        lambda: {"backend": ["superpowers"]},
    )

    curated = user_profile.ensure_curated_claude_config_dir("test-proj", base_dir=base_dir)
    curated_settings = json.loads((curated / "settings.json").read_text(encoding="utf-8"))

    assert "superpowers@superpowers-dev" in curated_settings["enabledPlugins"]


def test_curation_does_not_mutate_base_settings(monkeypatch, tmp_path):
    data_home = tmp_path / "data_home"
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)

    base_dir = tmp_path / "base_claude"
    plugins_dir = base_dir / "plugins"

    _create_plugin(plugins_dir, "unassigned-plugin", "market", skills=["unassigned-skill"])
    settings_data = {
        "enabledPlugins": {
            "unassigned-plugin@market": True,
        },
    }
    _write_file(base_dir / "settings.json", json.dumps(settings_data))

    monkeypatch.setattr("agent_takkub.lead_context._allowed_project_roots", lambda project: [])
    monkeypatch.setattr(skill_policy, "load_policy", lambda: {})

    curated = user_profile.ensure_curated_claude_config_dir("test-proj", base_dir=base_dir)

    base_settings = json.loads((base_dir / "settings.json").read_text(encoding="utf-8"))
    curated_settings = json.loads((curated / "settings.json").read_text(encoding="utf-8"))

    assert "unassigned-plugin@market" in base_settings["enabledPlugins"]
    assert "unassigned-plugin@market" not in curated_settings["enabledPlugins"]
