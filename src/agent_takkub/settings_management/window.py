"""SettingsManagementWindow — the redesigned Settings surface's shell.

Sidebar (5 entities per SPEC.md IA — ``Roles``/``Skills``/``MCP Servers``/
``Plugins`` wired through Phase 3; Providers still shows a "coming soon"
placeholder so the nav is honest about scope instead of a dead link) + a
``QStackedWidget`` content area, themed via ``cockpit_theme.build_stylesheet``
at the window root (SPEC.md "Visual").
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import cockpit_theme as theme
from .pages.mcp_page import McpPage
from .pages.plugins_page import PluginsPage
from .pages.providers_page import ProvidersPage
from .pages.roles_page import RolesPage
from .pages.skills_page import SkillsPage

_SIDEBAR_ENTITIES = ("Roles", "Skills", "MCP Servers", "Plugins", "Providers")
_WIRED = {"Roles", "Skills", "MCP Servers", "Plugins", "Providers"}


class SettingsManagementWindow(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsWindow")
        self.setWindowTitle("Takkub Cockpit — Settings (new)")
        self.resize(1320, 848)

        fonts = theme.ensure_fonts_loaded()
        self.setStyleSheet(theme.build_stylesheet(fonts["sans"], fonts["mono"]))

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar_col = QVBoxLayout()
        sidebar_col.setContentsMargins(0, 0, 0, 0)
        sidebar_col.setSpacing(0)

        self.sidebar = QListWidget(self)
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(200)
        for name in _SIDEBAR_ENTITIES:
            item = QListWidgetItem(name if name in _WIRED else f"{name} (soon)")
            self.sidebar.addItem(item)
        self.sidebar.currentRowChanged.connect(self._on_nav_changed)
        sidebar_col.addWidget(self.sidebar, 1)

        # SPEC.md "Coexistence" — the redesign hasn't reached every legacy
        # view yet (Pipeline Builder/Templates/Users/Role Overlap/matrix), so
        # this window always offers a way back instead of trapping the user
        # in an incomplete surface. Hook, not a signal, so the page stays
        # importable/testable standalone (see manage_roles_requested).
        self.open_legacy_requested: Callable[[], None] = lambda: None
        self._legacy_link = theme.secondary_button("Open legacy settings", self)
        self._legacy_link.setFixedWidth(200)
        self._legacy_link.clicked.connect(lambda: self.open_legacy_requested())
        sidebar_col.addWidget(self._legacy_link)

        root.addLayout(sidebar_col)

        content = QVBoxLayout()
        content.setContentsMargins(20, 20, 20, 20)
        self.content_stack = QStackedWidget(self)
        content.addWidget(self.content_stack)
        root.addLayout(content, 1)

        self.roles_page = RolesPage(self)
        self.content_stack.addWidget(self.roles_page)
        self.roles_page.refresh()

        self.skills_page = SkillsPage(self)
        self.skills_page.manage_roles_requested = self._go_to_roles
        self.content_stack.addWidget(self.skills_page)
        self.skills_page.refresh()

        self.mcp_page = McpPage(self)
        self.mcp_page.manage_roles_requested = self._go_to_roles
        self.content_stack.addWidget(self.mcp_page)
        self.mcp_page.refresh()

        self.plugins_page = PluginsPage(self)
        self.plugins_page.manage_roles_requested = self._go_to_roles
        self.content_stack.addWidget(self.plugins_page)
        self.plugins_page.refresh()

        self.providers_page = ProvidersPage(self)
        self.providers_page.manage_roles_requested = self._go_to_roles
        self.content_stack.addWidget(self.providers_page)
        self.providers_page.refresh()

        self._pages = {
            "Roles": self.roles_page,
            "Skills": self.skills_page,
            "MCP Servers": self.mcp_page,
            "Plugins": self.plugins_page,
            "Providers": self.providers_page,
        }

        self._placeholder_index: dict[str, int] = {}
        for name in _SIDEBAR_ENTITIES:
            if name in _WIRED:
                continue
            placeholder = QLabel(f"{name} — coming in a later phase.", self)
            placeholder.setStyleSheet(f"color: {theme.TEXT_MUTED}; padding: 24px;")
            idx = self.content_stack.addWidget(placeholder)
            self._placeholder_index[name] = idx

        self.sidebar.setCurrentRow(0)

    def _go_to_roles(self) -> None:
        self.sidebar.setCurrentRow(_SIDEBAR_ENTITIES.index("Roles"))

    def _on_nav_changed(self, row: int) -> None:
        if row < 0:
            return
        name = _SIDEBAR_ENTITIES[row]
        if name in self._pages:
            self.content_stack.setCurrentWidget(self._pages[name])
        else:
            self.content_stack.setCurrentIndex(self._placeholder_index[name])
