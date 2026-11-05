"""New-project template picker: catalog, scaffold files, dialog and wizard flow.

Replaced the AI-rules flow (``claude -p`` writing CLAUDE.md) — nothing here
may depend on a provider binary.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from agent_takkub import project_template as pt


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _valid_values(key: str) -> set[str]:
    for layer in pt.LAYERS:
        if layer.id == key:
            return {n for n, _ in layer.options}
        for sub in layer.subs:
            if sub.id == key:
                return set(sub.options)
    raise AssertionError(f"unknown selection key {key!r}")


@pytest.mark.parametrize("name", list(pt.PRESETS))
def test_every_preset_uses_only_catalog_values(name: str) -> None:
    for key, value in pt.preset(name).items():
        allowed = _valid_values(key)
        values = value if isinstance(value, list) else [value]
        assert set(values) <= allowed, (name, key, value)


def test_scaffold_is_provider_neutral_and_matches_preview(tmp_path: Path) -> None:
    sel = pt.preset("SaaS เต็มชุด")
    root = tmp_path / "evc"
    result = pt.write_scaffold(root, "evc", sel)

    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    for needle in (
        "Next.js + Tailwind CSS + shadcn/ui",
        "NestJS",
        "PostgreSQL",
        "IBM Plex Sans Thai",
    ):
        assert needle in agents
    # every row of the preview tree exists on disk
    for label, _comment, is_dir in pt.tree_entries("evc", sel)[1:]:
        rel = label.strip()
        if label.startswith("  "):
            rel = "db/" + rel
        assert (root / rel).is_dir() if is_dir else (root / rel).is_file(), rel
    assert result.kept == [] and len(result.created) > 10
    assert "claude" not in (root / "README.md").read_text(encoding="utf-8").lower()


def test_compose_holds_previewed_services_and_app_services_wait_for_dockerfile(
    tmp_path: Path,
) -> None:
    sel = pt.preset("SaaS เต็มชุด")
    pt.write_scaffold(tmp_path, "evc", sel)
    compose = (tmp_path / "docker-compose.yml").read_text(encoding="utf-8")
    for svc in pt.compose_services(sel):
        assert f"\n  {svc}:\n" in compose
    assert compose.count('profiles: ["app"]') == 4  # api, web, admin, worker
    assert "db-data:/var/lib/postgresql/data" in compose
    env = (tmp_path / ".env.example").read_text(encoding="utf-8")
    assert "DATABASE_URL=postgresql://app:app@localhost:5432/app" in env
    assert "${" not in env  # literal values — works without docker compose interpolation


def test_scaffold_never_overwrites(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("mine", encoding="utf-8")
    result = pt.write_scaffold(tmp_path, "x", pt.preset("Web + API + DB"))
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "mine"
    assert tmp_path / "AGENTS.md" in result.kept


def test_project_paths_route_roles_to_part_folders(tmp_path: Path) -> None:
    paths = pt.project_paths(tmp_path, pt.preset("LINE LIFF"))
    root = tmp_path.resolve()
    assert paths["main"] == root.as_posix()
    assert paths["mobile"] == (root / "mobile").as_posix()
    assert paths["api"] == (root / "api").as_posix()
    assert "web" not in paths


def test_empty_preset_writes_only_root_files(tmp_path: Path) -> None:
    sel = pt.preset("ว่างเปล่า")
    root = tmp_path / "blank"
    pt.write_scaffold(root, "blank", sel)
    assert sorted(p.name for p in root.iterdir()) == [
        ".env.example",
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
    ]
    assert pt.compose_services(sel) == []


def test_dialog_picks_and_returns_selection(qapp, tmp_path: Path) -> None:
    from agent_takkub.new_project_dialog import NewProjectDialog

    dlg = NewProjectDialog(None, default_parent=tmp_path)
    try:
        dlg._apply_preset("API อย่างเดียว")
        assert dlg.sel["api"] == "FastAPI" and "web" not in dlg.sel
        chip = next(b for k, v, _m, b in dlg._chips if k == "api" and v == "ElysiaJS")
        chip.click()
        none_db = next(b for k, v, _m, b in dlg._chips if k == "db" and v == "")
        none_db.click()
        redis = next(b for k, v, _m, b in dlg._chips if k == "extras" and v == "Redis")
        redis.click()  # preset had Redis → toggles off
        assert dlg.sel["api"] == "ElysiaJS" and "db" not in dlg.sel and dlg.sel["extras"] == []
        assert chip.isChecked() and none_db.isChecked() and not redis.isChecked()
        assert "เลือกไว้ 1 ส่วน" in dlg.count_label.text()
        assert "api = ElysiaJS" in dlg.rules_label.text()

        dlg.name_edit.setText("my-api")
        dlg._accept()
        name, target, sel = dlg.result_value()
        assert name == "my-api" and target == tmp_path / "my-api" and sel["api"] == "ElysiaJS"
    finally:
        dlg.deleteLater()


def test_typed_project_name_is_escaped_in_the_tree_preview(qapp, tmp_path: Path) -> None:
    """Same class as CodeQL #66 in the mockup: the typed name goes into a
    rich-text label and must render as text, not markup."""
    from agent_takkub.new_project_dialog import NewProjectDialog

    dlg = NewProjectDialog(None, default_parent=tmp_path)
    try:
        dlg.name_edit.setText("<b>x</b>")
        assert "&lt;b&gt;x&lt;/b&gt;/" in dlg.tree_label.text()
        assert "<b>x</b>" not in dlg.tree_label.text()
    finally:
        dlg.deleteLater()


def test_wizard_template_flow_scaffolds_and_registers(qapp, tmp_path: Path, monkeypatch) -> None:
    import agent_takkub.new_project_dialog as npd
    from agent_takkub.project_wizard import ProjectWizardMixin

    target = tmp_path / "Shop App"

    class _FakeDialog:
        DialogCode = npd.NewProjectDialog.DialogCode

        def __init__(self, *_a, **_k):
            pass

        def exec(self):
            return self.DialogCode.Accepted

        def result_value(self):
            return ("Shop App", target, pt.preset("Web + API + DB"))

    monkeypatch.setattr(npd, "NewProjectDialog", _FakeDialog)
    monkeypatch.setattr("agent_takkub.config.load_projects", lambda: {"projects": {}})

    class _Status:
        def showMessage(self, *_a):
            pass

    class _Host(QWidget, ProjectWizardMixin):
        def __init__(self):
            super().__init__()
            self._status = _Status()
            self.saved: list = []

        def _default_clone_parent(self):
            return tmp_path

        def _save_and_open_project(self, name, p, paths, presets=None, description=None):
            self.saved.append((name, p, paths, description))

    host = _Host()
    try:
        host._new_project_from_template()
    finally:
        host.deleteLater()

    assert (target / "AGENTS.md").is_file() and (target / "web" / "README.md").is_file()
    name, p, paths, description = host.saved[0]
    assert name == "shop-app" and p == target and description == "Shop App"
    assert set(paths) == {"main", "web", "api"}
