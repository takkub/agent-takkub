"""Targeted regression tests for #563: per-project curated CLAUDE_CONFIG_DIR skill gate.

Verifies:
1. Credential mirroring (.credentials.json, settings.json, settings.local.json, keybindings.json).
2. Shared directories linking (projects, todos, plugins).
3. Personal skills excluded from curated catalog.
4. Project-owned and policy-assigned skills preserved.
5. Pruning of de-listed skills.
6. Doctor finding with #103 provider gap notice.
7. Pane env injection helper behaviour and escape hatch.
"""

from __future__ import annotations

from pathlib import Path

from agent_takkub import doctor, pane_env, skill_policy, user_profile


def _write_file(path: Path, content: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_skill(skills_dir: Path, name: str, description: str = "") -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_credential_and_settings_mirroring(monkeypatch, tmp_path):
    data_home = tmp_path / "data_home"
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)

    base_dir = tmp_path / "base_claude"
    base_dir.mkdir(parents=True, exist_ok=True)
    _write_file(base_dir / ".credentials.json", '{"token": "secret-auth-token"}')
    _write_file(base_dir / "settings.json", '{"model": "claude-3-opus"}')
    _write_file(base_dir / "settings.local.json", '{"local": true}')
    _write_file(base_dir / "keybindings.json", '{"ctrl+c": "abort"}')
    _write_file(base_dir / "CLAUDE.md", "# Global rules")

    (base_dir / "projects").mkdir(parents=True, exist_ok=True)
    (base_dir / "todos").mkdir(parents=True, exist_ok=True)
    (base_dir / "plugins").mkdir(parents=True, exist_ok=True)

    curated = user_profile.ensure_curated_claude_config_dir("my-project", base_dir=base_dir)

    assert curated.is_dir()
    assert (curated / ".credentials.json").read_text(
        encoding="utf-8"
    ) == '{"token": "secret-auth-token"}'
    assert (curated / "settings.json").read_text(encoding="utf-8") == '{"model": "claude-3-opus"}'
    assert (curated / "settings.local.json").read_text(encoding="utf-8") == '{"local": true}'
    assert (curated / "keybindings.json").read_text(encoding="utf-8") == '{"ctrl+c": "abort"}'
    assert (curated / "CLAUDE.md").read_text(encoding="utf-8") == "# Global rules"

    assert (curated / "projects").exists()
    assert (curated / "todos").exists()
    assert (curated / "plugins").exists()


def test_personal_skills_filtered_and_project_skills_kept(monkeypatch, tmp_path):
    data_home = tmp_path / "data_home"
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)

    base_dir = tmp_path / "base_claude"
    base_skills = base_dir / "skills"
    _write_skill(base_skills, "personal-unrelated-notes", "personal note taking")
    _write_skill(base_skills, "random-crypto-tracker", "crypto tools")
    _write_skill(base_skills, "project-specific-skill", "project tool")
    _write_skill(base_skills, "matrix-assigned-skill", "policy tool")

    # Mock project-owned skills
    project_root = tmp_path / "project_repo"
    _write_skill(project_root / ".claude" / "skills", "project-specific-skill")
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
    curated_skills = curated / "skills"

    assert curated_skills.is_dir()
    assert (curated_skills / "project-specific-skill").exists()
    assert (curated_skills / "matrix-assigned-skill").exists()
    assert not (curated_skills / "personal-unrelated-notes").exists()
    assert not (curated_skills / "random-crypto-tracker").exists()


def test_pruning_removes_unallowed_skills(monkeypatch, tmp_path):
    data_home = tmp_path / "data_home"
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)

    base_dir = tmp_path / "base_claude"
    curated = user_profile.curated_config_dir_for("clean-proj")
    curated_skills = curated / "skills"
    _write_skill(curated_skills, "stale-skill-to-remove")

    monkeypatch.setattr(
        "agent_takkub.lead_context._allowed_project_roots",
        lambda project: [],
    )
    monkeypatch.setattr(skill_policy, "load_policy", lambda: {})

    user_profile.ensure_curated_claude_config_dir("clean-proj", base_dir=base_dir)

    assert not (curated_skills / "stale-skill-to-remove").exists()


def test_doctor_finding_includes_provider_gap_note(monkeypatch, tmp_path):
    monkeypatch.setattr(user_profile, "config_dir_for", lambda project: tmp_path)
    _write_skill(tmp_path / "skills", "sample-skill")

    findings, _ = doctor.check_boot_context(role="backend", project="agent-takkub")
    hits = [f for f in findings if f.name == "native_skill_catalog"]

    assert len(hits) == 1
    detail = hits[0].detail
    assert "no per-skill CLI gate" in detail
    assert "provider gap: codex/gemini/etc. have no equivalent CLI skill gate per #103" in detail


def test_inject_curated_claude_config_dir(monkeypatch, tmp_path):
    data_home = tmp_path / "data_home"
    monkeypatch.setattr("agent_takkub.config.DATA_HOME", data_home)

    env: dict[str, str] = {}
    pane_env.inject_curated_claude_config_dir(env, "web-app")
    assert "CLAUDE_CONFIG_DIR" in env
    assert "providers" in env["CLAUDE_CONFIG_DIR"]
    assert "claude" in env["CLAUDE_CONFIG_DIR"]

    # Test escape hatch TAKKUB_SKILL_GATE=0
    monkeypatch.setenv("TAKKUB_SKILL_GATE", "0")
    env2: dict[str, str] = {}
    pane_env.inject_curated_claude_config_dir(env2, "web-app")
    assert "CLAUDE_CONFIG_DIR" not in env2

    # Test skip for default/empty
    monkeypatch.delenv("TAKKUB_SKILL_GATE", raising=False)
    env3: dict[str, str] = {}
    pane_env.inject_curated_claude_config_dir(env3, "default")
    assert "CLAUDE_CONFIG_DIR" not in env3
