from agent_takkub import native_skills
from agent_takkub.provider_spec import PROVIDER_REGISTRY, capability_state


def test_codex_skills_supported_and_gemini_gap():
    assert capability_state("codex", "skills") == "supported"
    assert capability_state("gemini", "skills") == "partial"
    assert native_skills.native_skill_gap(PROVIDER_REGISTRY["gemini"])
    assert native_skills.native_skill_gap(PROVIDER_REGISTRY["codex"]) is None


def test_link_into_isolated_home_keeps_system(tmp_path, monkeypatch):
    store = tmp_path / "gs"
    (store / "demo").mkdir(parents=True)
    (store / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n")
    home = tmp_path / "codex-home"
    (home / "skills" / ".system").mkdir(parents=True)
    monkeypatch.setattr(native_skills.config, "global_skills_dir", lambda: store)
    monkeypatch.setattr(
        native_skills.config, "provider_home_env", lambda p: {"CODEX_HOME": str(home)}
    )
    assert native_skills.link_native_skills(PROVIDER_REGISTRY["codex"], "") == ("linked", [])
    assert (home / "skills" / "demo" / "SKILL.md").is_file()
    assert (home / "skills" / ".system").is_dir()


def test_no_isolated_home_never_touches_real_home(monkeypatch):
    monkeypatch.setattr(native_skills.config, "provider_home_env", lambda p: {})
    status, _ = native_skills.link_native_skills(PROVIDER_REGISTRY["codex"], "")
    assert status == "no_isolated_home"


def test_link_failure_reports(tmp_path, monkeypatch):
    store = tmp_path / "gs"
    (store / "d").mkdir(parents=True)
    (store / "d" / "SKILL.md").write_text("x")
    monkeypatch.setattr(native_skills.config, "global_skills_dir", lambda: store)
    monkeypatch.setattr(
        native_skills.config, "provider_home_env", lambda p: {"CODEX_HOME": str(tmp_path / "h")}
    )
    monkeypatch.setattr(native_skills, "_make_link", lambda s, d: "denied")
    status, errs = native_skills.link_native_skills(PROVIDER_REGISTRY["codex"], "")
    assert status == "failed" and errs


def _setup(tmp_path, monkeypatch):
    gs, ps = tmp_path / "gs", tmp_path / "ps"
    home = tmp_path / "codex-home"
    monkeypatch.setattr(native_skills.config, "global_skills_dir", lambda: gs)
    monkeypatch.setattr(native_skills.config, "PROJECT_SKILLS_HOME", ps)
    monkeypatch.setattr(native_skills.config, "project_skills_dir", lambda ns: ps / ns)
    monkeypatch.setattr(
        native_skills.config, "provider_home_env", lambda p: {"CODEX_HOME": str(home)}
    )
    return gs, ps, home


def _skill(root, name, body="x"):
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(body)


def test_project_skill_does_not_leak_across_projects(tmp_path, monkeypatch):
    gs, ps, home = _setup(tmp_path, monkeypatch)
    _skill(gs, "shared")
    _skill(ps / "A", "same", "A-body")
    _skill(ps / "B", "same", "B-body")
    spec = PROVIDER_REGISTRY["codex"]
    assert native_skills.link_native_skills(spec, "A")[0] == "linked"
    assert native_skills.link_native_skills(spec, "B")[0] == "linked"
    assert (home / "skills" / "shared" / "SKILL.md").is_file()
    assert not (home / "skills" / "same").exists()
    assert native_skills.project_has_skills("A") and not native_skills.project_has_skills("Z")


def test_reconcile_removes_stale_links_keeps_real(tmp_path, monkeypatch):
    import shutil

    gs, ps, home = _setup(tmp_path, monkeypatch)
    _skill(gs, "gone")
    _skill(ps / "A", "old")
    spec = PROVIDER_REGISTRY["codex"]
    native_skills.link_native_skills(spec, "A")
    # older build linked a project skill; a global skill is later deleted
    assert native_skills._make_link(ps / "A" / "old", home / "skills" / "old") is None
    _skill(home / "skills", "real", "mine")
    shutil.rmtree(gs / "gone")
    native_skills.link_native_skills(spec, "A")
    assert not (home / "skills" / "gone").exists()
    assert not (home / "skills" / "old").exists()
    assert (home / "skills" / "real" / "SKILL.md").read_text() == "mine"
