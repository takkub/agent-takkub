"""SettingsWindow — the unified Takkub Cockpit Settings window (Phase 1).

Implements the gold/IBM-Plex design system from
`docs/design-review/2026-07-10-cockpit-settings-design-system.md`: a
titlebar + status strip + sidebar (PIPELINE/POLICY sections + "+ New Role")
+ content (header, a 7-view ``QStackedWidget``, footer).

Phase 1 wires two views for real:

* **New Role** — reuses :mod:`custom_roles` (``create_role`` + its existing
  validation) exactly like the old guided-create form in
  :mod:`pane_tools_dialog` did, just re-laid-out to the new design. MCP/plugin
  policy assignment (the old form's per-tool checkbox grid) is intentionally
  NOT wired here — the design collapses that to a single "use column
  defaults" toggle that has no backing concept in :mod:`pane_tools_policy`
  yet, so it's decorative pending Phase 2. Use 🔧 Tools ▸ Team & Roles for
  per-tool policy on a freshly created role in the meantime.
* **Providers & Roles** — reuses :mod:`provider_state` (provider on/off) and
  :mod:`provider_config` / :mod:`pipeline_config` (per-role CLI override +
  per-role pipeline-enable), the same three modules
  :mod:`pipeline_dialog`'s HTML settings page already bridges to. Provider
  on/off is staged into :attr:`SettingsWindow.pending_provider_disabled`
  (mirroring ``_PipelineBridge.pending_provider_disabled``) rather than
  written directly, so the caller can route it through
  ``orchestrator.toggle_provider`` and get the same live "[system] ...
  ENABLED/DISABLED" broadcast + status-bar repaint a chip click produces.

The remaining 5 views (Pipeline Builder / Templates / MCP Matrix / Plugins
Matrix / Skill Catalog) are placeholders — their real logic still lives in
:mod:`pipeline_dialog` (the Pipeline Settings page) and :mod:`pane_tools_dialog`
(🔧 Tools), both of which are kept alive during this transition per the task
spec (not deleted).

**Import constraint:** mirrors ``pane_tools_dialog``'s — this module MUST NOT
import ``app`` or ``cli`` (plain UI dialog, no engine/CLI coupling).
"""

from __future__ import annotations

from PyQt6.QtCore import QLocale, Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__ as _COCKPIT_VERSION
from . import (
    cockpit_theme,
    custom_roles,
    pipeline_config,
    project_nav,
    provider_config,
    provider_state,
)
from . import roles as roles_mod

# ── view indices (QStackedWidget page order) ────────────────────
VIEW_PIPELINE_BUILDER = 0
VIEW_TEMPLATES = 1
VIEW_PROVIDERS_ROLES = 2
VIEW_MCP_MATRIX = 3
VIEW_PLUGINS_MATRIX = 4
VIEW_SKILL_CATALOG = 5
VIEW_NEW_ROLE = 6

# (view index, nav label, sidebar section) — New Role is reached via the
# dedicated "+ New Role" button, not this list, so it isn't a normal nav item.
_NAV_VIEWS: tuple[tuple[int, str, str], ...] = (
    (VIEW_PIPELINE_BUILDER, "Pipeline Builder", "PIPELINE"),
    (VIEW_TEMPLATES, "Templates", "PIPELINE"),
    (VIEW_PROVIDERS_ROLES, "Providers & Roles", "POLICY"),
    (VIEW_MCP_MATRIX, "MCP Matrix", "POLICY"),
    (VIEW_PLUGINS_MATRIX, "Plugins Matrix", "POLICY"),
    (VIEW_SKILL_CATALOG, "Skill Catalog", "POLICY"),
)

_VIEW_HEADERS: dict[int, tuple[str, str]] = {
    VIEW_PIPELINE_BUILDER: ("Pipeline Builder", "ลาก-วาง hop และ role ใน pipeline template"),
    VIEW_TEMPLATES: ("Templates", "จัดการ pipeline template ที่บันทึกไว้"),
    VIEW_PROVIDERS_ROLES: (
        "Providers & Roles",
        "เปิด/ปิด provider (codex/gemini) + กำหนด CLI ต่อ role",
    ),
    VIEW_MCP_MATRIX: ("MCP Matrix", "role × MCP server policy"),
    VIEW_PLUGINS_MATRIX: ("Plugins Matrix", "role × plugin policy"),
    VIEW_SKILL_CATALOG: ("Skill Catalog", "browse skill ที่ role ใช้ได้"),
    VIEW_NEW_ROLE: ("New Role", "สร้าง custom role ใหม่"),
}

# Roles offered a per-role CLI override in "Providers & Roles". Excludes
# lead/codex/gemini (provider_config.FORCED_ROLES — CLI is fixed) and shell
# (not a pipeline-eligible role — see pipeline_config.VALID_ROLES's own note).
_OVERRIDABLE_ROLES: tuple[str, ...] = tuple(
    r for r in pipeline_config.VALID_ROLES if r not in provider_config.FORCED_ROLES and r != "shell"
)

_PROVIDER_DESC: dict[str, str] = {
    "codex": "OpenAI Codex CLI — second opinion / refactor cross-check",
    "gemini": "Google Antigravity (agy) — planning / long-context second opinion",
}


class SettingsWindow(QDialog):
    """The unified Settings window. One instance per open — construct fresh
    each time (mirrors ``PaneToolsDialog``/``PipelineSettingsDialog``, no
    singleton/caching), so it always reflects on-disk state at open time."""

    def __init__(
        self,
        parent: QWidget | None = None,
        project: str | None = None,
        initial_view: int = VIEW_PROVIDERS_ROLES,
    ) -> None:
        super().__init__(parent)
        self._project = project
        self._dirty = False
        # Staged provider on/off — mirrors pipeline_dialog._PipelineBridge's
        # pending_provider_disabled contract: {provider: desired_disabled},
        # only for providers whose target differs from disk. Populated on
        # Save & Apply; the caller applies it via orchestrator.toggle_provider
        # AFTER exec() returns Accepted, so live Lead panes get the same
        # broadcast a status-bar chip click produces.
        self.pending_provider_disabled: dict[str, bool] = {}

        self.setObjectName("settingsWindow")
        self.setWindowTitle("Takkub Cockpit — Settings")
        self.setMinimumSize(900, 600)
        self.resize(1320, 848)
        self.setSizeGripEnabled(True)

        fonts = cockpit_theme.ensure_fonts_loaded()
        self._fonts = fonts
        self.setStyleSheet(cockpit_theme.build_stylesheet(str(fonts["sans"]), str(fonts["mono"])))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_titlebar())
        outer.addWidget(self._build_status_strip())

        body = QWidget(self)
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)
        body_lay.addWidget(self._build_sidebar())
        body_lay.addWidget(self._build_content(), 1)
        outer.addWidget(body, 1)

        self._goto_view(initial_view)

    # ──────────────────────────────────────────────────────────
    # chrome: titlebar / status strip
    # ──────────────────────────────────────────────────────────

    def _build_titlebar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("titlebar")
        bar.setFixedHeight(38)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(8)

        square = QLabel(bar)
        square.setFixedSize(10, 10)
        square.setStyleSheet(f"background: {cockpit_theme.ACCENT_GOLD}; border-radius: 2px;")
        lay.addWidget(square)

        label = QLabel("takkub cockpit — settings", bar)
        label.setObjectName("titlebarLabel")
        lay.addWidget(label)
        lay.addStretch(1)

        dots = QLabel("● ● ●", bar)
        dots.setObjectName("titlebarDots")
        lay.addWidget(dots)
        return bar

    def _build_status_strip(self) -> QWidget:
        strip = QWidget(self)
        strip.setObjectName("statusStrip")
        strip.setFixedHeight(56)
        lay = QHBoxLayout(strip)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(10)

        brand = QLabel("takkub COCKPIT", strip)
        brand.setObjectName("statusBrand")
        lay.addWidget(brand)

        payload = pipeline_config.load(self._project)
        active_id = payload.get("activeTemplate", "")
        active_name = next(
            (t["name"] for t in payload.get("templates", []) if t.get("id") == active_id), active_id
        )
        if active_name:
            lay.addWidget(cockpit_theme.gold_soft_chip(str(active_name), strip))

        roles_enabled = payload.get("rolesEnabled", {})
        for role in pipeline_config.VALID_ROLES:
            if not roles_enabled.get(role, True):
                continue
            r = roles_mod.by_name(role)
            if r is None:
                continue
            color = cockpit_theme.ROLE_COLORS.get(role, r.color)
            lay.addWidget(cockpit_theme.role_chip(r.label, color, strip))

        lay.addStretch(1)

        for provider in sorted(provider_state.TOGGLABLE):
            enabled = not provider_state.is_disabled(provider)
            dot = QLabel("●", strip)
            color = cockpit_theme.ACCENT_GOLD if enabled else cockpit_theme.TEXT_FAINT
            dot.setStyleSheet(f"color: {color}; font-size: 10px;")
            dot.setToolTip(f"{provider}: {'enabled' if enabled else 'disabled'}")
            lay.addWidget(dot)

        version = QLabel(f"v{_COCKPIT_VERSION}", strip)
        version.setObjectName("statusVersion")
        lay.addWidget(version)
        return strip

    # ──────────────────────────────────────────────────────────
    # chrome: sidebar
    # ──────────────────────────────────────────────────────────

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget(self)
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(236)
        lay = QVBoxLayout(sidebar)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(0)

        self._nav_buttons: dict[int, QPushButton] = {}
        last_section: str | None = None
        for view_idx, label, section in _NAV_VIEWS:
            if section != last_section:
                sec_lbl = QLabel(section, sidebar)
                sec_lbl.setObjectName("sidebarSection")
                lay.addWidget(sec_lbl)
                last_section = section
            # QPushButton treats a lone "&" as a mnemonic escape (swallows the
            # next char) — "Providers & Roles" would render as "Providers
            # _Roles". Double it so it displays literally.
            btn = QPushButton(label.replace("&", "&&"), sidebar)
            btn.setObjectName("navButton")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("active", False)
            btn.clicked.connect(lambda _checked=False, v=view_idx: self._goto_view(v))
            lay.addWidget(btn)
            self._nav_buttons[view_idx] = btn

        lay.addStretch(1)

        new_role_btn = QPushButton("＋ New Role", sidebar)
        new_role_btn.setObjectName("newRoleButton")
        new_role_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_role_btn.clicked.connect(lambda: self._goto_view(VIEW_NEW_ROLE))
        lay.addWidget(new_role_btn)

        return sidebar

    def _goto_view(self, view_idx: int) -> None:
        for idx, btn in self._nav_buttons.items():
            btn.setProperty("active", idx == view_idx)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._stack.setCurrentIndex(view_idx)
        title, sub = _VIEW_HEADERS.get(view_idx, ("", ""))
        self._content_title.setText(title)
        self._content_sub.setText(sub)

    # ──────────────────────────────────────────────────────────
    # chrome: content (header + stack + footer)
    # ──────────────────────────────────────────────────────────

    def _build_content(self) -> QWidget:
        content = QWidget(self)
        content.setObjectName("content")
        outer = QVBoxLayout(content)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header_body = QWidget(content)
        hb_lay = QVBoxLayout(header_body)
        hb_lay.setContentsMargins(24, 20, 24, 16)
        hb_lay.setSpacing(4)

        self._content_title = QLabel("", header_body)
        self._content_title.setObjectName("contentTitle")
        hb_lay.addWidget(self._content_title)
        self._content_sub = QLabel("", header_body)
        self._content_sub.setObjectName("contentSub")
        self._content_sub.setWordWrap(True)
        hb_lay.addWidget(self._content_sub)
        hb_lay.addSpacing(8)

        self._stack = QStackedWidget(header_body)
        # Index order MUST match the VIEW_* constants above.
        self._stack.addWidget(self._wrap_scroll(self._build_pipeline_builder_view()))
        self._stack.addWidget(self._wrap_scroll(self._build_templates_view()))
        self._stack.addWidget(self._wrap_scroll(self._build_providers_roles_view()))
        self._stack.addWidget(self._wrap_scroll(self._build_mcp_matrix_view()))
        self._stack.addWidget(self._wrap_scroll(self._build_plugins_matrix_view()))
        self._stack.addWidget(self._wrap_scroll(self._build_skill_catalog_view()))
        self._stack.addWidget(self._wrap_scroll(self._build_new_role_view()))
        hb_lay.addWidget(self._stack, 1)

        outer.addWidget(header_body, 1)
        outer.addWidget(self._build_footer())
        return content

    def _wrap_scroll(self, inner: QWidget) -> QScrollArea:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)
        return scroll

    def _build_footer(self) -> QWidget:
        footer = QWidget(self)
        footer.setObjectName("footer")
        footer.setFixedHeight(60)
        lay = QHBoxLayout(footer)
        lay.setContentsMargins(24, 0, 24, 0)
        lay.setSpacing(10)

        reset_btn = cockpit_theme.secondary_button("↺ Reset to default", footer)
        reset_btn.clicked.connect(self._on_reset_clicked)
        lay.addWidget(reset_btn)
        lay.addStretch(1)

        self._unsaved_dot = QLabel("●", footer)
        self._unsaved_dot.setObjectName("unsavedDot")
        self._unsaved_dot.setVisible(False)
        lay.addWidget(self._unsaved_dot)
        self._unsaved_label = QLabel("unsaved changes", footer)
        self._unsaved_label.setObjectName("unsavedLabel")
        self._unsaved_label.setVisible(False)
        lay.addWidget(self._unsaved_label)

        cancel_btn = cockpit_theme.secondary_button("Cancel", footer)
        cancel_btn.clicked.connect(self.reject)
        lay.addWidget(cancel_btn)

        self._save_btn = cockpit_theme.gold_button("Save && Apply", footer)
        self._save_btn.clicked.connect(self._on_save_apply_clicked)
        lay.addWidget(self._save_btn)

        return footer

    def _mark_dirty(self, *_args: object) -> None:
        self._dirty = True
        self._unsaved_dot.setVisible(True)
        self._unsaved_label.setVisible(True)

    def _clear_dirty(self) -> None:
        self._dirty = False
        self._unsaved_dot.setVisible(False)
        self._unsaved_label.setVisible(False)

    def _on_reset_clicked(self) -> None:
        """Reset the currently-visible view's editable fields back to the
        on-disk state. Placeholder views have nothing to reset (no-op)."""
        idx = self._stack.currentIndex()
        if idx == VIEW_PROVIDERS_ROLES:
            self._reset_providers_roles_view()
        elif idx == VIEW_NEW_ROLE:
            self._reset_new_role_form()
        self._clear_dirty()

    def _on_save_apply_clicked(self) -> None:
        """Persist Providers & Roles edits. New Role commits independently
        via its own "+ Create Role" button — this only ever touches
        provider/role state, so it's a safe no-op if the user never opened
        that view."""
        try:
            self.pending_provider_disabled = {}
            for provider, toggle in self._provider_toggles.items():
                desired_disabled = not toggle.isChecked()
                if desired_disabled != provider_state.is_disabled(provider):
                    self.pending_provider_disabled[provider] = desired_disabled

            role_providers = {
                role: combo.currentData() for role, combo in self._role_provider_combos.items()
            }
            provider_config.save_role_overrides(role_providers, self._project)

            payload = pipeline_config.load(self._project)
            roles_enabled = dict(payload.get("rolesEnabled", {}))
            for role, toggle in self._role_toggles.items():
                roles_enabled[role] = toggle.isChecked()
            payload["rolesEnabled"] = roles_enabled
            pipeline_config.save(payload, self._project)
        except OSError as e:
            QMessageBox.critical(self, "Save failed", f"บันทึกไม่สำเร็จ: {e}")
            return
        self._clear_dirty()
        self.accept()

    # ──────────────────────────────────────────────────────────
    # view: Providers & Roles (real)
    # ──────────────────────────────────────────────────────────

    def _build_providers_roles_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(14)

        banner = QLabel(
            "provider ที่ปิดหรือยังไม่ติดตั้ง → Claude รับตำแหน่งแทนอัตโนมัติ "
            "(role เดิม, engine เปลี่ยนเป็น claude — เสีย model diversity)",
            view,
        )
        banner.setObjectName("infoBanner")
        banner.setWordWrap(True)
        lay.addWidget(banner)

        provider_panel = QWidget(view)
        provider_panel.setObjectName("panel")
        pp_lay = QVBoxLayout(provider_panel)
        pp_lay.setContentsMargins(14, 12, 14, 12)
        pp_lay.setSpacing(10)
        pp_title = QLabel("Providers", provider_panel)
        pp_title.setObjectName("panelTitle")
        pp_lay.addWidget(pp_title)

        self._provider_toggles: dict[str, cockpit_theme.ToggleSwitch] = {}
        for provider in sorted(provider_state.TOGGLABLE):
            row = QWidget(provider_panel)
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(10)
            provider_role = roles_mod.by_name(provider)
            color = cockpit_theme.ROLE_COLORS.get(
                provider, provider_role.color if provider_role else "#94a3b8"
            )
            row_lay.addWidget(cockpit_theme.role_chip(provider.capitalize(), color, row))
            desc = QLabel(_PROVIDER_DESC.get(provider, ""), row)
            desc.setObjectName("panelHint")
            row_lay.addWidget(desc, 1)
            toggle = cockpit_theme.ToggleSwitch(
                row, checked=not provider_state.is_disabled(provider)
            )
            toggle.toggled.connect(self._mark_dirty)
            row_lay.addWidget(toggle)
            self._provider_toggles[provider] = toggle
            pp_lay.addWidget(row)
        lay.addWidget(provider_panel)

        role_panel = QWidget(view)
        role_panel.setObjectName("panel")
        rp_lay = QVBoxLayout(role_panel)
        rp_lay.setContentsMargins(14, 12, 14, 12)
        rp_lay.setSpacing(10)
        rp_title = QLabel("Roles", role_panel)
        rp_title.setObjectName("panelTitle")
        rp_lay.addWidget(rp_title)

        roles_enabled = pipeline_config.load(self._project).get("rolesEnabled", {})
        role_providers = provider_config.role_provider_map(_OVERRIDABLE_ROLES, self._project)

        self._role_toggles = {}
        self._role_provider_combos = {}

        lead_row = self._build_role_row(
            "lead",
            "Lead",
            cockpit_theme.ROLE_COLORS["lead"],
            "Cockpit coordinator — provider fixed, always on",
            role_panel,
            locked=True,
        )
        rp_lay.addWidget(lead_row)

        for role in _OVERRIDABLE_ROLES:
            r = roles_mod.by_name(role)
            label = r.label if r else role.capitalize()
            color = cockpit_theme.ROLE_COLORS.get(role, r.color if r else "#94a3b8")
            row = self._build_role_row(
                role,
                label,
                color,
                "",
                role_panel,
                locked=False,
                enabled=roles_enabled.get(role, True),
                current_provider=role_providers.get(role, provider_config.CLAUDE),
            )
            rp_lay.addWidget(row)

        lay.addWidget(role_panel)
        lay.addStretch(1)
        return view

    def _build_role_row(
        self,
        role: str,
        label: str,
        color: str,
        desc: str,
        parent: QWidget,
        *,
        locked: bool,
        enabled: bool = True,
        current_provider: str | None = None,
    ) -> QWidget:
        row = QWidget(parent)
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 4, 0, 4)
        row_lay.setSpacing(10)
        row_lay.addWidget(cockpit_theme.role_chip(label, color, row))
        desc_lbl = QLabel(desc, row)
        desc_lbl.setObjectName("panelHint")
        row_lay.addWidget(desc_lbl, 1)

        if locked:
            locked_lbl = QLabel("Claude (fixed)", row)
            locked_lbl.setObjectName("panelHint")
            row_lay.addWidget(locked_lbl)
            toggle = cockpit_theme.ToggleSwitch(row, checked=True)
            toggle.setEnabled(False)
            row_lay.addWidget(toggle)
            return row

        combo = QComboBox(row)
        for provider in sorted(provider_config.VALID_PROVIDERS):
            combo.addItem(provider.capitalize(), provider)
        idx = combo.findData(current_provider)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.currentIndexChanged.connect(self._mark_dirty)
        row_lay.addWidget(combo)
        self._role_provider_combos[role] = combo

        toggle = cockpit_theme.ToggleSwitch(row, checked=enabled)
        toggle.toggled.connect(self._mark_dirty)
        row_lay.addWidget(toggle)
        self._role_toggles[role] = toggle
        return row

    def _reset_providers_roles_view(self) -> None:
        for provider, toggle in self._provider_toggles.items():
            toggle.blockSignals(True)
            toggle.setChecked(not provider_state.is_disabled(provider))
            toggle.blockSignals(False)

        roles_enabled = pipeline_config.load(self._project).get("rolesEnabled", {})
        for role, toggle in self._role_toggles.items():
            toggle.blockSignals(True)
            toggle.setChecked(roles_enabled.get(role, True))
            toggle.blockSignals(False)

        role_providers = provider_config.role_provider_map(_OVERRIDABLE_ROLES, self._project)
        for role, combo in self._role_provider_combos.items():
            combo.blockSignals(True)
            idx = combo.findData(role_providers.get(role, provider_config.CLAUDE))
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    # ──────────────────────────────────────────────────────────
    # view: New Role (real)
    # ──────────────────────────────────────────────────────────

    def _build_new_role_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(12)

        form = QWidget(view)
        form.setObjectName("panel")
        f_lay = QVBoxLayout(form)
        f_lay.setContentsMargins(16, 14, 16, 14)
        f_lay.setSpacing(10)

        name_row = QHBoxLayout()
        name_col = QVBoxLayout()
        name_col.addWidget(QLabel("Name (--role)", form))
        self._nr_name = QLineEdit(form)
        self._nr_name.setPlaceholderText("data-eng (a-z0-9-_ เท่านั้น)")
        name_col.addWidget(self._nr_name)
        name_row.addLayout(name_col, 1)

        label_col = QVBoxLayout()
        label_col.addWidget(QLabel("Label", form))
        self._nr_label = QLineEdit(form)
        self._nr_label.setPlaceholderText("🧬 Data Eng")
        label_col.addWidget(self._nr_label)
        name_row.addLayout(label_col, 1)
        f_lay.addLayout(name_row)

        grid_row = QHBoxLayout()
        col_col = QVBoxLayout()
        col_col.addWidget(QLabel("Grid column", form))
        self._nr_column = QComboBox(form)
        self._nr_column.addItem("1 · Dev column", 1)
        self._nr_column.addItem("2 · Support column", 2)
        self._nr_column.setCurrentIndex(1)
        col_col.addWidget(self._nr_column)
        grid_row.addLayout(col_col)

        row_col = QVBoxLayout()
        row_col.addWidget(QLabel("Grid row", form))
        self._nr_row = QSpinBox(form)
        # QSpinBox renders digits with the OS locale's native numeral system
        # by default — on a Thai-locale machine that's ๐-๙, not 0-9. This
        # field feeds a JSON int (custom_roles.create_role's `row` param), so
        # force ASCII digits regardless of locale.
        self._nr_row.setLocale(QLocale(QLocale.Language.C))
        self._nr_row.setRange(0, 99)
        self._nr_row.setValue(99)
        row_col.addWidget(self._nr_row)
        grid_row.addLayout(row_col)
        grid_row.addStretch(1)
        f_lay.addLayout(grid_row)

        f_lay.addWidget(QLabel("Accent", form))
        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(6)
        self._nr_color = "#94a3b8"
        self._nr_swatch_btns: list[QPushButton] = []
        for color in project_nav._AVATAR_COLORS:
            sw = QPushButton("", form)
            sw.setFixedSize(20, 20)
            sw.setCursor(Qt.CursorShape.PointingHandCursor)
            sw.clicked.connect(lambda _checked=False, c=color: self._on_swatch_clicked(c))
            self._nr_swatch_btns.append(sw)
            swatch_row.addWidget(sw)
        swatch_row.addStretch(1)
        f_lay.addLayout(swatch_row)
        self._update_swatch_selection()

        toggle_row = QHBoxLayout()
        toggle_row.addWidget(QLabel("ใช้ default MCP+Plugins ตาม column (แนะนำ)", form), 1)
        self._nr_default_tools_toggle = cockpit_theme.ToggleSwitch(form, checked=True)
        toggle_row.addWidget(self._nr_default_tools_toggle)
        f_lay.addLayout(toggle_row)
        tools_hint = QLabel(
            "Phase 1: toggle นี้ยังไม่ต่อ policy จริง — ตั้ง MCP/Plugins ต่อ role ผ่าน "
            "🔧 Tools ▸ Team & Roles ตามปกติ (Phase 2 จะรวมเข้าที่นี่)",
            form,
        )
        tools_hint.setObjectName("panelHint")
        tools_hint.setWordWrap(True)
        f_lay.addWidget(tools_hint)

        f_lay.addWidget(QLabel("Instructions", form))
        self._nr_instructions = QPlainTextEdit(form)
        self._nr_instructions.setPlaceholderText("บอก role ตัวเองว่าทำหน้าที่อะไร ขอบเขตงานคืออะไร...")
        self._nr_instructions.setMinimumHeight(90)
        f_lay.addWidget(self._nr_instructions)

        self._nr_status = QLabel("", form)
        self._nr_status.setObjectName("panelHint")
        self._nr_status.setWordWrap(True)
        f_lay.addWidget(self._nr_status)

        create_row = QHBoxLayout()
        create_btn = cockpit_theme.gold_button("+ Create Role", form)
        create_btn.clicked.connect(self._on_create_role_clicked)
        create_row.addWidget(create_btn)
        create_row.addStretch(1)
        f_lay.addLayout(create_row)

        lay.addWidget(form)
        lay.addStretch(1)
        return view

    def _on_swatch_clicked(self, color: str) -> None:
        self._nr_color = color
        self._update_swatch_selection()

    def _update_swatch_selection(self) -> None:
        for btn, color in zip(self._nr_swatch_btns, project_nav._AVATAR_COLORS, strict=False):
            border = cockpit_theme.ACCENT_GOLD if color == self._nr_color else "transparent"
            btn.setStyleSheet(
                f"background:{color}; border-radius:10px; border: 2px solid {border};"
            )

    def _on_create_role_clicked(self) -> None:
        name = self._nr_name.text().strip().lower()
        label = self._nr_label.text().strip()
        column = self._nr_column.currentData()
        row = self._nr_row.value()
        instructions = self._nr_instructions.toPlainText().strip() or None

        ok, err = custom_roles.create_role(name, label, self._nr_color, column, row, instructions)
        if not ok:
            self._nr_status.setText(f"⚠️ {err}")
            return

        # Register in THIS process immediately so `--role <name>` spawns
        # without waiting for a cockpit restart (roles.py otherwise only
        # loads custom-roles.json at boot) — same pattern pane_tools_dialog
        # uses for the same reason.
        role = custom_roles.load_custom_roles().get(name)
        if role is not None:
            roles_mod.register_role(role)

        self._nr_status.setText(
            f"✓ สร้าง role '{name}' แล้ว — spawn ได้ทันทีด้วย "
            f'`takkub assign --role {name} "..."` (ไม่ต้อง restart cockpit)'
        )
        self._reset_new_role_form(clear_status=False)

    def _reset_new_role_form(self, clear_status: bool = True) -> None:
        self._nr_name.clear()
        self._nr_label.clear()
        self._nr_instructions.clear()
        self._nr_column.setCurrentIndex(1)
        self._nr_row.setValue(99)
        self._nr_color = "#94a3b8"
        self._update_swatch_selection()
        self._nr_default_tools_toggle.setChecked(True)
        if clear_status:
            self._nr_status.setText("")

    # ──────────────────────────────────────────────────────────
    # views: placeholders (Phase 2)
    # ──────────────────────────────────────────────────────────

    def _build_placeholder(self, note: str) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        lay.addStretch(1)
        badge = QLabel(f"🚧 Phase 2\n\n{note}", w)
        badge.setObjectName("placeholderBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setWordWrap(True)
        lay.addWidget(badge)
        lay.addStretch(1)
        return w

    def _build_pipeline_builder_view(self) -> QWidget:
        return self._build_placeholder(
            "Pipeline Builder — hop drag/drop editor ยังอยู่ที่ Pipeline Settings เดิม "
            "(👥 Team ▸ คลิกขวา) จนกว่า Phase 2 จะย้ายเข้ามาที่นี่"
        )

    def _build_templates_view(self) -> QWidget:
        return self._build_placeholder(
            "Templates — list เดิมอยู่ใน Pipeline Settings (👥 Team ▸ คลิกขวา) จนกว่า Phase 2"
        )

    def _build_mcp_matrix_view(self) -> QWidget:
        return self._build_placeholder(
            "MCP Matrix — role × MCP server เดิมอยู่ใน 🔧 Tools จนกว่า Phase 2 จะย้ายเข้ามาที่นี่"
        )

    def _build_plugins_matrix_view(self) -> QWidget:
        return self._build_placeholder(
            "Plugins Matrix — role × plugin เดิมอยู่ใน 🔧 Tools จนกว่า Phase 2"
        )

    def _build_skill_catalog_view(self) -> QWidget:
        return self._build_placeholder(
            "Skill Catalog — browse skill เดิมอยู่ใน 🔧 Tools ▸ Team & Roles จนกว่า Phase 2"
        )
