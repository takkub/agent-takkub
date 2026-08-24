"""Project Explorer — collapsible left-panel file tree for a ProjectTab
(#365 phase 1: Workspace Shell + Project Explorer; phase 4 adds the
CHANGES section below).

View only. All filesystem IO is dispatched through `project_file_index.py`
on a background thread (`ProjectFileIndex`/`GitStatusService`); this module
never touches disk itself except for the already-`resolve_and_contain`-ed,
read-only context-menu actions (open externally / reveal / copy path).

"Open in Takkub" (file rows only) and double-click both emit a path for
`ProjectTab` to route to `main_window`'s app-wide `EditorHost` (#365 phase 2).

CHANGES section (#365 phase 4, `git_changes_service.py`): a top-level tree
row "CHANGES (n)", hidden while there are no changes, populated from
`GitChangesService.changesChanged` (background + debounced, same shape as
`GitStatusService`'s own badge refresh). A single click on a change row
emits `changeActivated` — `ProjectTab`/`MainWindow` route that to
`EditorHost.open_file(..., show_diff=True)`, which opens the file and
immediately requests its git-HEAD diff, so this panel never needs its own
diff round-trip or a second Monaco surface. Attribution is deliberately
absent: `FileChange` carries only path + status letter (+ `old_path` for a
rename, #375), never a guessed author/role (05_GIT_DIFF_AND_AGENT_CHANGES.md
— "never guess agent attribution from git alone").

Multi-root / multi-repo (#375 GAP-009): `RepoDiscoveryService` resolves every
configured root to its real git top-level in the background. A project whose
roots span more than one distinct repo groups CHANGES rows under one header
per repo; a single repo (still the common case) renders the same flat list
as before.

Not wired up here (later phase, left as a disabled menu placeholder):
  * per-row "Ask Agent" from the context menu — the editor tab's own Ask
    Agent (Monaco context menu / "?" tab button) covers the phase 3+4 scope
    with a bounded selection; a file-level variant here is future work.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QMenu,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme
from ._win_console import SUBPROCESS_NO_WINDOW
from .config import load_projects
from .git_changes_service import FileChange, GitChangesService, RepoDiscoveryService
from .project_file_index import (
    FileEntry,
    GitStatusService,
    PathEscapesRootsError,
    ProjectFileIndex,
    _safe_resolve,
    resolve_and_contain,
)

logger = logging.getLogger(__name__)

# Item data roles (Qt.ItemDataRole.UserRole + offset) — one tree, one column,
# so plain ints are enough; no need to import the Qt.ItemDataRole enum name
# at every use site.
_PATH_ROLE = 0x0100
_IS_DIR_ROLE = 0x0101
_LOADED_ROLE = 0x0102
_PLACEHOLDER_ROLE = 0x0103
_CHANGES_HEADER_ROLE = 0x0104
_CHANGE_ROW_ROLE = 0x0105
_CHANGES_GROUP_ROLE = 0x0106  # per-repo subgroup header, multi-repo projects only (#375)

_GIT_STATUS_COLORS = {
    "M": cockpit_theme.STATE_WARN_BRIGHT,
    "A": cockpit_theme.STATE_OK_BRIGHT,
    "D": cockpit_theme.STATE_ERROR_BRIGHT,
}
# "R" (rename) only appears in the CHANGES list — GitStatusService's own
# per-row badges (_GIT_STATUS_COLORS above) never see it, porcelain v1.
_GIT_CHANGE_COLORS = {**_GIT_STATUS_COLORS, "R": cockpit_theme.STATE_INFO_BRIGHT}

_TREE_QSS = f"""
QTreeWidget {{
    background: {cockpit_theme.GROUND_SIDEBAR};
    border: none;
    color: {cockpit_theme.TEXT_SECONDARY};
    font-size: 12px;
    outline: none;
}}
QTreeWidget::item {{
    padding: 3px 4px;
    border: none;
}}
QTreeWidget::item:hover {{
    background: {cockpit_theme.GROUND_PANEL};
}}
QTreeWidget::item:selected {{
    background: {cockpit_theme.GROUND_SELECT};
    color: {cockpit_theme.TEXT_PRIMARY};
}}
"""


def project_roots(project_name: str) -> dict[str, Path]:
    """Label -> resolved root path, read straight from projects.json —
    the same source `lead_context._allowed_project_roots` reads (kept as an
    independent lookup here so the UI layer doesn't have to import that
    heavier, Lead-spawn-focused module just for a label->path dict)."""
    data = load_projects()
    proj = (data.get("projects") or {}).get(project_name) or {}
    return {label: _safe_resolve(Path(p)) for label, p in (proj.get("paths") or {}).items()}


class ProjectExplorer(QWidget):
    fileActivated = pyqtSignal(str)  # double-click on a file — absolute path
    # Single click on a CHANGES-section row — absolute path (phase 4).
    changeActivated = pyqtSignal(str)

    # Phase 2 placeholder — declared now so a later phase only has to
    # connect, not re-plumb the menu. Never emitted today: the menu action
    # is disabled below.
    openInTakkubRequested = pyqtSignal(str)
    askAgentRequested = pyqtSignal(str)

    def __init__(self, project_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project_name = project_name
        self.roots = project_roots(project_name)

        self.index = ProjectFileIndex(list(self.roots.values()), self)
        self.index.dirListed.connect(self._on_dir_listed)
        self.index.dirListFailed.connect(self._on_dir_list_failed)
        self._pending: dict[str, list[QTreeWidgetItem]] = {}

        # git status badges key off whichever configured root comes first —
        # fine for the common single-repo project, a no-op for paths outside
        # that root. The CHANGES panel used to make the same simplification
        # for `GitChangesService.repo_root`, silently dropping every row for
        # a project whose first root is a *subdirectory* of its repo (git
        # reports status paths relative to the real top-level, not to that
        # root) and never seeing a second, independent repo at all (#375
        # GAP-009) — `self.git_changes` (first root, sync) stays as the
        # immediate/compact-view default; `_repo_discovery` corrects its
        # `repo_root` and spins up one more `GitChangesService` per extra
        # distinct repo, once the background `git rev-parse --show-toplevel`
        # round trip for every root comes back.
        self.git_status: GitStatusService | None = None
        self.git_changes: GitChangesService | None = None
        self._extra_git_changes: dict[Path, GitChangesService] = {}
        self._changes_by_repo: dict[Path, list[FileChange]] = {}
        self._repo_labels: dict[Path, list[str]] = {}
        self._git_status_map: dict[str, str] = {}
        self._repo_discovery: RepoDiscoveryService | None = None
        self._repo_discovery_started = False
        if self.roots:
            first_root = next(iter(self.roots.values()))
            self.git_status = GitStatusService(first_root, parent=self)
            self.git_status.statusChanged.connect(self._on_git_status_changed)
            self.git_changes = GitChangesService(first_root, list(self.roots.values()), parent=self)
            self.git_changes.changesChanged.connect(self._on_changes_changed)
            self._repo_discovery = RepoDiscoveryService(list(self.roots.values()), parent=self)
            self._repo_discovery.discovered.connect(self._on_repos_discovered)

        self._dir_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self._file_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setStyleSheet(_TREE_QSS)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.itemClicked.connect(self._on_item_clicked)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.tree)

        self._populate_roots()

        # Appended after the per-root folders (not before) so it never
        # shifts every existing root-folder index — hidden until the first
        # non-empty status. Populated by the same trigger GitStatusService's
        # own badges use (a real directory listing, see _on_dir_listed) plus
        # MainWindow routing EditorHost.gitRefreshNeeded here after a save
        # or an external disk change — no eager refresh at construction, to
        # avoid arming GitChangesService's debounce timer for every tab that
        # never touches git (matches the existing lazy-tree philosophy).
        self._changes_item = QTreeWidgetItem(self.tree, ["CHANGES"])
        self._changes_item.setData(0, _CHANGES_HEADER_ROLE, True)
        self._changes_item.setHidden(True)

    # ------------------------------------------------------------------
    # tree population (lazy — one directory per request, worker thread)
    # ------------------------------------------------------------------
    def _add_placeholder(self, item: QTreeWidgetItem) -> None:
        ph = QTreeWidgetItem(item, ["…"])
        ph.setData(0, _PLACEHOLDER_ROLE, True)
        ph.setFlags(Qt.ItemFlag.NoItemFlags)

    def _populate_roots(self) -> None:
        for label, root in self.roots.items():
            item = QTreeWidgetItem(self.tree, [label])
            item.setIcon(0, self._dir_icon)
            item.setData(0, _PATH_ROLE, str(root))
            item.setData(0, _IS_DIR_ROLE, True)
            item.setData(0, _LOADED_ROLE, False)
            self._add_placeholder(item)

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if not item.data(0, _IS_DIR_ROLE) or item.data(0, _LOADED_ROLE):
            return
        item.setData(0, _LOADED_ROLE, True)
        item.takeChildren()
        path = item.data(0, _PATH_ROLE)
        if not path:
            return
        self._pending.setdefault(path, []).append(item)
        self.index.request_list(Path(path))

    def _on_dir_listed(self, path: str, entries: list[FileEntry]) -> None:
        items = self._pending.pop(path, [])
        for item in items:
            for entry in entries:
                child = QTreeWidgetItem(item, [entry.name])
                child.setData(0, _PATH_ROLE, str(entry.path))
                child.setData(0, _IS_DIR_ROLE, entry.is_dir)
                if entry.is_dir:
                    child.setIcon(0, self._dir_icon)
                    child.setData(0, _LOADED_ROLE, False)
                    self._add_placeholder(child)
                else:
                    child.setIcon(0, self._file_icon)
                self._apply_badge(child)
        self.refresh_changes()

    def _on_dir_list_failed(self, path: str, error: str) -> None:
        self._pending.pop(path, None)
        logger.debug("project explorer: listing %s failed: %s", path, error)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.data(0, _PLACEHOLDER_ROLE) or item.data(0, _IS_DIR_ROLE):
            return  # dirs already toggle expand via QTreeWidget's own default
        path = item.data(0, _PATH_ROLE)
        if path:
            self.fileActivated.emit(path)

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if not item.data(0, _CHANGE_ROW_ROLE):
            return
        path = item.data(0, _PATH_ROLE)
        if path:
            self.changeActivated.emit(path)

    # ------------------------------------------------------------------
    # git status badges (M/A/D — background + debounced, see GitStatusService)
    # ------------------------------------------------------------------
    def _on_git_status_changed(self, status: dict) -> None:
        self._git_status_map = status
        self._repaint_badges(self.tree.invisibleRootItem())

    def _repaint_badges(self, parent_item: QTreeWidgetItem) -> None:
        for i in range(parent_item.childCount()):
            item = parent_item.child(i)
            # Change rows get their own status-letter color from
            # _on_changes_changed (includes "R", which GitStatusService's
            # porcelain-v1 badges never emit) — never overwritten here.
            if item.data(0, _PLACEHOLDER_ROLE) or item.data(0, _CHANGE_ROW_ROLE):
                continue
            self._apply_badge(item)
            self._repaint_badges(item)

    # ------------------------------------------------------------------
    # CHANGES panel (M/A/D/R vs HEAD — background + debounced,
    # see GitChangesService; #365 phase 4, multi-repo #375)
    # ------------------------------------------------------------------
    def refresh_changes(self) -> None:
        """Ask every git service to re-check. Called after a directory
        listing, and by `MainWindow` after an editor save or an external
        disk-change notification (`EditorHost.gitRefreshNeeded`).

        `_repo_discovery` only starts here, on the first call — not eagerly
        at construction — matching the rest of this module's lazy-tree
        philosophy (module docstring): a tab that's opened but never
        expanded/refreshed shouldn't fire a background `git` subprocess for
        every one of its configured roots for nothing.
        """
        if not self._repo_discovery_started and self._repo_discovery is not None:
            self._repo_discovery_started = True
            self._repo_discovery.discover()
        if self.git_status is not None:
            self.git_status.request_refresh()
        if self.git_changes is not None:
            self.git_changes.request_refresh()
        for svc in self._extra_git_changes.values():
            svc.request_refresh()

    def _on_repos_discovered(self, mapping: dict[Path, Path]) -> None:
        """`RepoDiscoveryService.discovered` result — `{configured_root:
        toplevel}`, omitting any root that isn't inside a repo. Groups the
        configured roots by distinct repo, corrects `self.git_changes` if
        its root was actually a subdirectory of the real top-level, and
        starts one more `GitChangesService` per additional distinct repo
        the project spans (#375 GAP-009)."""
        if self.git_changes is None:
            return
        by_repo: dict[Path, list[str]] = {}
        for label, root in self.roots.items():
            resolved_root = _safe_resolve(Path(root))
            toplevel = mapping.get(resolved_root, resolved_root)
            by_repo.setdefault(toplevel, []).append(label)
        self._repo_labels = by_repo

        primary_root = _safe_resolve(Path(next(iter(self.roots.values()))))
        primary_toplevel = mapping.get(primary_root, primary_root)
        if primary_toplevel != self.git_changes.repo_root:
            self.git_changes.changesChanged.disconnect(self._on_changes_changed)
            self._changes_by_repo.pop(self.git_changes.repo_root, None)
            self.git_changes.deleteLater()
            self.git_changes = GitChangesService(
                primary_toplevel, list(self.roots.values()), parent=self
            )
            self.git_changes.changesChanged.connect(self._on_changes_changed)
            self.git_changes.request_refresh()

        for toplevel in by_repo:
            if toplevel == primary_toplevel or toplevel in self._extra_git_changes:
                continue
            svc = GitChangesService(toplevel, list(self.roots.values()), parent=self)
            svc.changesChanged.connect(
                lambda changes, tl=toplevel: self._on_repo_changes_changed(tl, changes)
            )
            self._extra_git_changes[toplevel] = svc
            svc.request_refresh()
        self._render_changes()

    def _on_changes_changed(self, changes: list[FileChange]) -> None:
        if self.git_changes is None:
            return
        self._changes_by_repo[self.git_changes.repo_root] = changes
        self._render_changes()

    def _on_repo_changes_changed(self, repo_root: Path, changes: list[FileChange]) -> None:
        self._changes_by_repo[repo_root] = changes
        self._render_changes()

    def _render_changes(self) -> None:
        """Rebuild the CHANGES section from `_changes_by_repo`. A single
        repo (the common case, and every project until `_repo_discovery`
        resolves) renders the flat list `_on_changes_changed` always has —
        unchanged from before #375. More than one distinct repo groups rows
        under one header per repo, labelled with whichever configured root
        label(s) map to it and that repo's own change count."""
        self._changes_item.takeChildren()
        total = sum(len(v) for v in self._changes_by_repo.values())
        self._changes_item.setText(0, f"CHANGES ({total})")
        self._changes_item.setHidden(total == 0)
        if len(self._repo_labels) <= 1:
            for repo_root, changes in self._changes_by_repo.items():
                self._add_change_rows(self._changes_item, repo_root, changes)
        else:
            for repo_root, labels in self._repo_labels.items():
                changes = self._changes_by_repo.get(repo_root, [])
                group = QTreeWidgetItem(
                    self._changes_item, [f"{', '.join(labels)} ({len(changes)})"]
                )
                group.setData(0, _CHANGES_GROUP_ROLE, True)
                self._add_change_rows(group, repo_root, changes)
                if changes:
                    group.setExpanded(True)
        if total:
            self._changes_item.setExpanded(True)

    def _add_change_rows(
        self, parent_item: QTreeWidgetItem, repo_root: Path, changes: list[FileChange]
    ) -> None:
        for change in changes:
            try:
                resolved = resolve_and_contain(repo_root / change.path, list(self.roots.values()))
            except PathEscapesRootsError:
                continue  # shouldn't happen for a well-formed git status line — refuse, don't crash
            label = f"{change.status}  {change.path}"
            if change.status == "R" and change.old_path:
                label = f"R  {change.old_path} -> {change.path}"
            row = QTreeWidgetItem(parent_item, [label])
            row.setIcon(0, self._file_icon)
            row.setData(0, _PATH_ROLE, str(resolved))
            row.setData(0, _IS_DIR_ROLE, False)
            row.setData(0, _CHANGE_ROW_ROLE, True)
            color = _GIT_CHANGE_COLORS.get(change.status)
            row.setForeground(0, QColor(color) if color else QColor(cockpit_theme.TEXT_SECONDARY))

    def _apply_badge(self, item: QTreeWidgetItem) -> None:
        if self.git_status is None or not self._git_status_map:
            return
        path_str = item.data(0, _PATH_ROLE)
        if not path_str:
            return
        try:
            rel = _safe_resolve(Path(path_str)).relative_to(self.git_status.repo_root).as_posix()
        except ValueError:
            return
        color = _GIT_STATUS_COLORS.get(self._git_status_map.get(rel, ""))
        item.setForeground(0, QColor(color) if color else QColor(cockpit_theme.TEXT_SECONDARY))

    # ------------------------------------------------------------------
    # context menu: Open externally / Reveal / Copy path
    # ------------------------------------------------------------------
    def _on_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None or item.data(0, _PLACEHOLDER_ROLE):
            return
        path_str = item.data(0, _PATH_ROLE)
        if not path_str:
            return
        try:
            path = resolve_and_contain(Path(path_str), list(self.roots.values()))
        except PathEscapesRootsError:
            return  # shouldn't happen (path came from our own listing) — refuse, don't crash
        is_dir = bool(item.data(0, _IS_DIR_ROLE))
        menu = self._build_context_menu(path, is_dir)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _build_context_menu(self, path: Path, is_dir: bool) -> QMenu:
        """Split out from `_on_context_menu` so tests can inspect the menu
        (action text/enabled state) and trigger an action directly —
        `QMenu.exec()` opens a real modal event loop that a headless test
        has no way to dismiss."""
        menu = QMenu(self.tree)
        menu.addAction("Open externally").triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        )
        menu.addAction("Reveal").triggered.connect(lambda: self._reveal(path, is_dir))
        menu.addAction("Copy path").triggered.connect(
            lambda: QApplication.clipboard().setText(str(path))
        )
        menu.addSeparator()
        act_open_takkub = menu.addAction("Open in Takkub")
        if is_dir:
            act_open_takkub.setEnabled(False)
            act_open_takkub.setToolTip("Select a file, not a folder")
        else:
            act_open_takkub.triggered.connect(lambda: self.openInTakkubRequested.emit(str(path)))
        act_ask_agent = menu.addAction("Ask Agent")
        act_ask_agent.setEnabled(False)
        act_ask_agent.setToolTip("Coming in a later phase")
        return menu

    @staticmethod
    def _reveal(path: Path, is_dir: bool) -> None:
        """Show `path` in the OS file manager, pre-selected when possible.
        Best-effort: a failure here is a lost convenience, never a crash."""
        try:
            if sys.platform == "win32":
                if is_dir:
                    os.startfile(str(path))
                else:
                    subprocess.Popen(
                        ["explorer", f"/select,{path}"], creationflags=SUBPROCESS_NO_WINDOW
                    )
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)], creationflags=SUBPROCESS_NO_WINDOW)
            else:
                target = path if is_dir else path.parent
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        except OSError as exc:
            logger.debug("project explorer: reveal failed for %s: %s", path, exc)
