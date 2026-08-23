"""Project Explorer view (#365 phase 1): lazy tree population — never a
direct filesystem scan, always a dispatch to `ProjectFileIndex` — plus the
disabled phase-2 menu placeholders.

`project_roots()` is monkeypatched directly (not routed through a real
projects.json) so these tests stay isolated from config/DATA_HOME entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from agent_takkub import project_explorer as pe
from agent_takkub.project_file_index import FileEntry, GitStatusService, ProjectFileIndex

from ._qt_timer_leak_guard import stop_timers_after


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _stop_git_status_timer(monkeypatch):
    # Every ProjectExplorer constructs its own GitStatusService, whose
    # `_timer` only arms once `request_refresh()` fires (e.g. after a real
    # directory listing) — several tests below exercise exactly that path.
    # The explorer (and its child GitStatusService/QTimer) is usually already
    # garbage-collected by the time this runs, since nothing in these tests
    # keeps a longer-lived reference — tolerate that race rather than the
    # rarer case where something does keep it alive.
    finalize = stop_timers_after(monkeypatch, GitStatusService, "_timer")
    yield
    try:
        finalize()
    except RuntimeError:
        pass


@pytest.fixture
def one_root_project(monkeypatch, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(pe, "project_roots", lambda name: {"main": root})
    return root


def _wait_until(predicate, timeout_ms: int = 2000, step_ms: int = 25) -> None:
    elapsed = 0
    while not predicate() and elapsed < timeout_ms:
        QTest.qWait(step_ms)
        elapsed += step_ms
    assert predicate(), "condition not met before timeout"


class TestLazyPopulation:
    def test_construction_adds_only_a_placeholder_no_listing_dispatched(
        self, qapp, one_root_project, monkeypatch
    ) -> None:
        calls: list = []
        monkeypatch.setattr(ProjectFileIndex, "request_list", lambda self, d: calls.append(d))

        explorer = pe.ProjectExplorer("proj")

        assert calls == []  # construction never touches the filesystem
        assert explorer.tree.topLevelItemCount() == 1
        root_item = explorer.tree.topLevelItem(0)
        assert root_item.text(0) == "main"
        assert root_item.childCount() == 1  # just the "…" placeholder
        assert root_item.child(0).data(0, pe._PLACEHOLDER_ROLE) is True

    def test_expand_dispatches_to_index_not_a_direct_scan(
        self, qapp, one_root_project, monkeypatch
    ) -> None:
        calls: list[Path] = []
        monkeypatch.setattr(ProjectFileIndex, "request_list", lambda self, d: calls.append(Path(d)))
        explorer = pe.ProjectExplorer("proj")
        root_item = explorer.tree.topLevelItem(0)

        explorer._on_item_expanded(root_item)

        assert calls == [one_root_project.resolve()]
        assert root_item.childCount() == 0  # placeholder cleared, awaiting the worker
        assert root_item.data(0, pe._LOADED_ROLE) is True

    def test_re_expanding_an_already_loaded_item_does_not_re_dispatch(
        self, qapp, one_root_project, monkeypatch
    ) -> None:
        calls: list = []
        monkeypatch.setattr(ProjectFileIndex, "request_list", lambda self, d: calls.append(d))
        explorer = pe.ProjectExplorer("proj")
        root_item = explorer.tree.topLevelItem(0)

        explorer._on_item_expanded(root_item)
        explorer._on_item_expanded(root_item)

        assert len(calls) == 1

    def test_end_to_end_listing_via_real_background_worker(self, qapp, one_root_project) -> None:
        (one_root_project / "sub").mkdir()
        (one_root_project / "a.py").write_text("x")
        explorer = pe.ProjectExplorer("proj")
        root_item = explorer.tree.topLevelItem(0)

        explorer._on_item_expanded(root_item)  # dispatches to the real QThreadPool worker
        _wait_until(lambda: root_item.childCount() > 0)

        names = sorted(root_item.child(i).text(0) for i in range(root_item.childCount()))
        assert names == ["a.py", "sub"]
        children = [root_item.child(i) for i in range(root_item.childCount())]
        sub_item = next(c for c in children if c.text(0) == "sub")
        assert sub_item.data(0, pe._IS_DIR_ROLE) is True
        assert sub_item.childCount() == 1  # its own lazy placeholder


class TestFileActivation:
    def _populate_with(self, explorer, root_item, root: Path, entries: list[FileEntry]) -> None:
        # Mirror what `_on_item_expanded` does before dispatching: clear the
        # "…" lazy-load placeholder first.
        root_item.takeChildren()
        key = str(root.resolve())
        explorer._pending[key] = [root_item]
        explorer._on_dir_listed(key, entries)

    def test_double_click_file_emits_file_activated(self, qapp, one_root_project) -> None:
        f = one_root_project / "a.py"
        f.write_text("x")
        explorer = pe.ProjectExplorer("proj")
        root_item = explorer.tree.topLevelItem(0)
        self._populate_with(
            explorer, root_item, one_root_project, [FileEntry(name="a.py", path=f, is_dir=False)]
        )
        file_item = root_item.child(0)

        received: list[str] = []
        explorer.fileActivated.connect(received.append)
        explorer._on_item_double_clicked(file_item, 0)

        assert received == [str(f.resolve())]

    def test_double_click_dir_does_not_emit(self, qapp, one_root_project) -> None:
        explorer = pe.ProjectExplorer("proj")
        root_item = explorer.tree.topLevelItem(0)

        received: list[str] = []
        explorer.fileActivated.connect(received.append)
        explorer._on_item_double_clicked(root_item, 0)

        assert received == []


class TestContextMenu:
    def test_ask_agent_is_a_disabled_placeholder(self, qapp, one_root_project) -> None:
        explorer = pe.ProjectExplorer("proj")
        menu = explorer._build_context_menu(one_root_project, True)

        by_text = {a.text(): a for a in menu.actions() if a.text()}
        assert by_text["Ask Agent"].isEnabled() is False
        assert by_text["Open externally"].isEnabled() is True
        assert by_text["Reveal"].isEnabled() is True
        assert by_text["Copy path"].isEnabled() is True

    def test_open_in_takkub_disabled_for_a_directory(self, qapp, one_root_project) -> None:
        explorer = pe.ProjectExplorer("proj")
        menu = explorer._build_context_menu(one_root_project, True)

        by_text = {a.text(): a for a in menu.actions() if a.text()}
        assert by_text["Open in Takkub"].isEnabled() is False

    def test_open_in_takkub_emits_signal_for_a_file(self, qapp, one_root_project) -> None:
        f = one_root_project / "a.py"
        f.write_text("x")
        explorer = pe.ProjectExplorer("proj")
        menu = explorer._build_context_menu(f, False)
        by_text = {a.text(): a for a in menu.actions() if a.text()}
        assert by_text["Open in Takkub"].isEnabled() is True

        received: list[str] = []
        explorer.openInTakkubRequested.connect(received.append)
        by_text["Open in Takkub"].trigger()

        assert received == [str(f)]

    def test_copy_path_action_copies_to_clipboard(self, qapp, one_root_project) -> None:
        explorer = pe.ProjectExplorer("proj")
        menu = explorer._build_context_menu(one_root_project, True)
        by_text = {a.text(): a for a in menu.actions() if a.text()}

        by_text["Copy path"].trigger()

        assert QApplication.clipboard().text() == str(one_root_project)

    def test_context_menu_refuses_a_path_outside_roots(
        self, qapp, one_root_project, tmp_path, monkeypatch
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        explorer = pe.ProjectExplorer("proj")
        root_item = explorer.tree.topLevelItem(0)
        root_item.setData(0, pe._PATH_ROLE, str(outside))  # simulate a corrupted item
        # Stub hit-testing instead of relying on real visual layout (never
        # shown/laid out in a headless test) to resolve a QPoint to an item.
        monkeypatch.setattr(explorer.tree, "itemAt", lambda pos: root_item)

        built: list = []
        monkeypatch.setattr(explorer, "_build_context_menu", lambda *a: built.append(a))
        explorer._on_context_menu(QPoint(0, 0))

        assert built == []  # refused before ever building a menu
