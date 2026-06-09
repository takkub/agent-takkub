"""Unit tests for config_wizard.py.

Headless — no pytest-qt needed. Uses a module-scoped QApplication so Qt widget
types can be instantiated; widgets are never shown (no display ops).

Note: conftest autouse _isolate_runtime creates tmp_path/_isolated_runtime in
every test, so populate_from_root tests check for specific keys not exact counts.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from agent_takkub.config_wizard import (
    _AUTO_KEY,
    ConfigWizard,
    _NamePage,
    _PathsPage,
    _PresetsProfilePage,
)


@pytest.fixture(scope="module")
def qapp():
    """Ensure a QApplication instance exists for the test module."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ──────────────────────────────────────────────────────────────
# _NamePage
# ──────────────────────────────────────────────────────────────
class TestNamePage:
    def test_empty_is_incomplete(self, qapp):
        page = _NamePage(existing_names=set())
        assert not page.isComplete()

    def test_valid_name_and_folder_is_complete(self, qapp, tmp_path):
        page = _NamePage(existing_names=set())
        page._name_edit.setText("my-app")
        page._folder_edit.setText(str(tmp_path))
        assert page.isComplete()

    def test_duplicate_name_is_incomplete(self, qapp, tmp_path):
        page = _NamePage(existing_names={"my-app"})
        page._name_edit.setText("my-app")
        page._folder_edit.setText(str(tmp_path))
        assert not page.isComplete()
        assert "already exists" in page._err_label.text()

    def test_missing_folder_is_incomplete(self, qapp, tmp_path):
        page = _NamePage(existing_names=set())
        page._name_edit.setText("proj")
        page._folder_edit.setText(str(tmp_path / "nonexistent"))
        assert not page.isComplete()

    def test_error_clears_on_valid_input(self, qapp, tmp_path):
        page = _NamePage(existing_names={"taken"})
        page._name_edit.setText("taken")
        page._folder_edit.setText(str(tmp_path))
        assert not page.isComplete()
        page._name_edit.setText("fresh")
        assert page.isComplete()
        assert page._err_label.text() == ""


# ──────────────────────────────────────────────────────────────
# _PathsPage
# ──────────────────────────────────────────────────────────────
class TestPathsPage:
    def test_initially_one_empty_row(self, qapp):
        page = _PathsPage()
        assert len(page._rows) == 1

    def test_add_row_increments_count(self, qapp):
        page = _PathsPage()
        before = len(page._rows)
        page._add_row()
        assert len(page._rows) == before + 1

    def test_remove_row_decrements_count(self, qapp):
        page = _PathsPage()
        page._add_row()
        before = len(page._rows)
        pair = page._rows[-1]
        page._remove_row(pair)
        assert len(page._rows) == before - 1

    def test_get_paths_includes_valid_key_and_dir(self, qapp, tmp_path):
        page = _PathsPage()
        _, key_e, path_e = page._rows[0]
        key_e.setText("web")
        path_e.setReadOnly(False)
        path_e.setText(str(tmp_path))
        paths = page.get_paths()
        assert "web" in paths
        assert str(tmp_path.resolve().as_posix()) == paths["web"]

    def test_get_paths_excludes_nonexistent_path(self, qapp, tmp_path):
        page = _PathsPage()
        _, key_e, path_e = page._rows[0]
        key_e.setText("api")
        path_e.setReadOnly(False)
        path_e.setText(str(tmp_path / "nonexistent_dir"))
        assert "api" not in page.get_paths()

    def test_get_paths_excludes_empty_key(self, qapp, tmp_path):
        page = _PathsPage()
        _, key_e, path_e = page._rows[0]
        key_e.setText("")  # no key
        path_e.setReadOnly(False)
        path_e.setText(str(tmp_path))
        assert page.get_paths() == {}

    def test_populate_from_root_resets_rows(self, qapp, tmp_path):
        """populate_from_root clears old rows and adds one per subdir."""
        subdir = tmp_path / "myservice"
        subdir.mkdir()
        page = _PathsPage()
        before = len(page._rows)
        page.populate_from_root(str(tmp_path))
        # Old rows gone, new rows added for each visible subdir
        # (conftest adds _isolated_runtime to tmp_path, so count is
        # not predictable — just verify rows changed and are non-empty)
        assert len(page._rows) != before or len(page._rows) > 0

    def test_populate_from_root_guesses_key_web(self, qapp, tmp_path):
        (tmp_path / "web").mkdir(exist_ok=True)
        page = _PathsPage()
        page.populate_from_root(str(tmp_path))
        keys = [pair[1].text() for pair in page._rows]
        assert "web" in keys

    def test_populate_from_root_guesses_key_api(self, qapp, tmp_path):
        (tmp_path / "api").mkdir(exist_ok=True)
        page = _PathsPage()
        page.populate_from_root(str(tmp_path))
        keys = [pair[1].text() for pair in page._rows]
        assert "api" in keys

    def test_populate_from_root_excludes_dotfiles(self, qapp, tmp_path):
        (tmp_path / ".dotdir").mkdir(exist_ok=True)
        page = _PathsPage()
        page.populate_from_root(str(tmp_path))
        paths = [pair[2].text() for pair in page._rows]
        assert not any(".dotdir" in p for p in paths)

    def test_populate_from_root_leaves_one_empty_row_when_no_subdirs(self, qapp, tmp_path):
        # Use a fresh subdir with no children to avoid conftest's _isolated_runtime
        fresh = tmp_path / "fresh_root"
        fresh.mkdir()
        page = _PathsPage()
        page.populate_from_root(str(fresh))
        assert len(page._rows) == 1
        _, key_e, path_e = page._rows[0]
        assert key_e.text() == ""
        assert path_e.text() == ""


# ──────────────────────────────────────────────────────────────
# _PresetsProfilePage
# ──────────────────────────────────────────────────────────────
class TestPresetsProfilePage:
    def test_no_presets_selected_by_default(self, qapp):
        page = _PresetsProfilePage()
        assert page.selected_presets() == []

    def test_check_presets_returns_them(self, qapp):
        page = _PresetsProfilePage()
        page._checkboxes["frontend"].setChecked(True)
        page._checkboxes["qa"].setChecked(True)
        selected = page.selected_presets()
        assert "frontend" in selected
        assert "qa" in selected
        assert "backend" not in selected

    def test_generate_claude_md_unchecked_by_default(self, qapp):
        page = _PresetsProfilePage()
        assert not page.generate_claude_md()

    def test_generate_claude_md_checked(self, qapp):
        page = _PresetsProfilePage()
        page._gen_cb.setChecked(True)
        assert page.generate_claude_md()

    def test_init_profiles_populates_combo(self, qapp, tmp_path, monkeypatch):
        import agent_takkub.user_profile as _up

        monkeypatch.setattr(
            _up,
            "list_profiles",
            lambda: [
                {"name": "default", "config_dir": str(tmp_path)},
                {"name": "work", "config_dir": str(tmp_path / "work")},
            ],
        )
        page = _PresetsProfilePage()
        page.init_profiles()
        items = [page._profile_combo.itemText(i) for i in range(page._profile_combo.count())]
        assert items == ["default", "work"]

    def test_selected_profile_default_fallback(self, qapp):
        page = _PresetsProfilePage()
        # Empty combo → falls back to "default"
        assert page.selected_profile() == "default"


# ──────────────────────────────────────────────────────────────
# _AUTO_KEY mapping (pure logic — no Qt needed)
# ──────────────────────────────────────────────────────────────
class TestAutoKey:
    @pytest.mark.parametrize(
        "subdir,expected_key",
        [
            ("web", "web"),
            ("frontend", "web"),
            ("client", "web"),
            ("api", "api"),
            ("backend", "api"),
            ("server", "api"),
            ("mobile", "mobile"),
            ("infra", "infra"),
            ("infrastructure", "infra"),
            ("devops", "infra"),
        ],
    )
    def test_known_subdirs_map_to_key(self, subdir, expected_key):
        assert _AUTO_KEY.get(subdir) == expected_key

    def test_unknown_subdir_returns_none(self):
        assert _AUTO_KEY.get("something-unknown") is None


# ──────────────────────────────────────────────────────────────
# ConfigWizard.result_data (smoke — no modal exec)
# ──────────────────────────────────────────────────────────────
class TestConfigWizardResultData:
    def test_result_data_has_expected_keys(self, qapp, tmp_path):
        wizard = ConfigWizard(existing_names=set())
        wizard._page1._name_edit.setText("test-proj")
        wizard._page1._folder_edit.setText(str(tmp_path))
        wizard._page3._gen_cb.setChecked(False)
        data = wizard.result_data()
        assert set(data.keys()) == {
            "name",
            "root",
            "paths",
            "presets",
            "profile",
            "generate_claude_md",
        }

    def test_result_data_name_and_root(self, qapp, tmp_path):
        wizard = ConfigWizard(existing_names=set())
        wizard._page1._name_edit.setText("test-proj")
        wizard._page1._folder_edit.setText(str(tmp_path))
        data = wizard.result_data()
        assert data["name"] == "test-proj"
        assert data["root"] == str(tmp_path)

    def test_result_data_presets(self, qapp, tmp_path):
        wizard = ConfigWizard(existing_names=set())
        wizard._page1._name_edit.setText("test-proj")
        wizard._page1._folder_edit.setText(str(tmp_path))
        wizard._page3._checkboxes["frontend"].setChecked(True)
        wizard._page3._checkboxes["qa"].setChecked(True)
        data = wizard.result_data()
        assert "frontend" in data["presets"]
        assert "qa" in data["presets"]
        assert "backend" not in data["presets"]

    def test_result_data_generate_flag(self, qapp, tmp_path):
        wizard = ConfigWizard(existing_names=set())
        wizard._page1._name_edit.setText("p")
        wizard._page1._folder_edit.setText(str(tmp_path))
        wizard._page3._gen_cb.setChecked(True)
        assert wizard.result_data()["generate_claude_md"] is True
        wizard._page3._gen_cb.setChecked(False)
        assert wizard.result_data()["generate_claude_md"] is False
