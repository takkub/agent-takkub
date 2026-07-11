"""SettingsWindow — the unified Takkub Cockpit Settings window.

Implements the gold/IBM-Plex design system from
`docs/design-review/2026-07-10-cockpit-settings-design-system.md`: a
status strip + sidebar (PIPELINE/POLICY/ACCOUNT sections + "+ New
Role") + content (header, an 8-view ``QStackedWidget``, footer). The
original design also had a decorative faux titlebar above the status
strip — dropped 2026-07-11 (UI walkthrough #55): it duplicated the OS
title bar's "Takkub Cockpit — Settings" text with no other function.

Phase 1 wired **Providers & Roles** and **New Role**. Phase 2 wires the
remaining five:

* **MCP Matrix** / **Plugins Matrix** — native ``QGridLayout`` role×item
  toggle grids, backed by the pure per-role policy helpers in
  :mod:`pane_tools_dialog` (``build_matrix``/``matrix_to_role_items``/
  ``diff_role_items``/``discover_marketplaces``/``master_mcps``/
  ``policy_role_items`` — module-level functions, no Qt) so this view reads/
  writes :mod:`pane_tools_policy` identically to how the old standalone
  "🔧 Tools" dialog did before it was removed (2026-07-10, fully superseded
  by this window). Plugins Matrix keeps the denylist banner (security-guidance/
  remember are never toggleable — see :mod:`lead_context`'s
  ``_PANE_PLUGIN_DENYLIST``, enforced at pane-spawn time, not in this UI).
* **Role Overlap** (ROLE section) — role list + read-only mono doc viewer,
  backed by :mod:`skill_audit` (``load_all_role_docs`` + ``audit_existing_
  role`` — "✓ won't overlap" for the selected role). This is a ROLE-scope
  audit (TF-IDF), *not* a skill browser — it was mislabeled "Skill Catalog"
  before 2026-07-11.
* **Skill Catalog** (SKILL section) — the real skill browser: lists
  ``.claude/skills/*/SKILL.md`` via :mod:`skill_scan` (the scanner the New
  Role picker uses) with each skill's description + which role docs mention
  it. Read-only browse, like Role Overlap.
* **Pipeline Builder** / **Templates** — native hop editor + template
  list/detail, backed by :mod:`pipeline_config` directly (a from-scratch
  reimplementation of :mod:`pipeline_dialog`'s QWebEngineView page per the
  task spec, not a wrapper around it). Structural template edits (Duplicate/
  Delete) write immediately, mirroring the Add/Remove MCP pattern; in-flight
  hop edits are staged in :attr:`SettingsWindow._pb_hops` and only persist on
  Save & Apply, mirroring the toggle-matrix staging pattern.
* **New Role**'s "use default MCP+Plugins ตาม column" toggle now actually
  seeds :mod:`pane_tools_policy` on create (checked → the same MCP/plugin
  defaults the matching dev/support-column built-in roles get; unchecked →
  an explicit empty policy the operator configures via the Matrix views).
* **Users** (ACCOUNT section, 2026-07-11) — Profiles + Claude Auth tabs,
  ported from :mod:`user_actions`'s standalone ``open_user_profiles_dialog``
  modal (removed the same day — 100% superseded). Reached both as a normal
  sidebar nav item and via the 👥 Team chip's right-click "Add / Remove
  user…" entry (:meth:`user_actions.UserActionsMixin._on_add_user_clicked`),
  which now opens straight to this view instead of its own popup.

:mod:`pipeline_dialog` (the old Pipeline Settings page, still reachable via
the 👥 Team chip's right-click menu — see :mod:`user_actions`'s
``_show_pipelines_menu``) is kept alive as an alternate entry point to the
same underlying config. The old standalone "🔧 Tools" dialog
(:mod:`pane_tools_dialog`'s ``PaneToolsDialog`` — 100% redundant with this
window's Providers & Roles / MCP Matrix / Plugins Matrix / Skill Catalog /
New Role views) was removed 2026-07-10; only its pure per-role policy
helper functions remain in that module, imported below.

**Import constraint:** mirrors ``pane_tools_dialog``'s — this module MUST NOT
import ``app`` or ``cli`` (plain UI dialog, no engine/CLI coupling).
"""

from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtCore import QLocale, QSize, Qt
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__ as _COCKPIT_VERSION
from . import (
    cockpit_theme,
    config,
    custom_roles,
    pane_tools_dialog,
    pane_tools_policy,
    pipeline_config,
    project_nav,
    provider_config,
    provider_state,
    shared_dev_tools,
    skill_audit,
    skill_scan,
    user_profile,
)
from . import roles as roles_mod
from .claude_auth_config import ClaudeAuthConfig, load_claude_auth, save_claude_auth
from .lead_context import _allowed_project_roots

# ── view indices (QStackedWidget page order) ────────────────────
# Order is stable for indices 0–7 (external callers/tests reference these
# constants); the real Skill Catalog was appended as index 8 rather than
# renumbering. VIEW_ROLE_OVERLAP (5) is the old "Skill Catalog" page — it was
# never a skill browser, it audits ROLE-doc scope overlap (TF-IDF), so it was
# renamed to say what it does. VIEW_SKILL_CATALOG (8) is the new, real skill
# browser backed by `skill_scan` (the same scanner the New Role picker uses).
VIEW_PIPELINE_BUILDER = 0
VIEW_TEMPLATES = 1
VIEW_PROVIDERS_ROLES = 2
VIEW_MCP_MATRIX = 3
VIEW_PLUGINS_MATRIX = 4
VIEW_ROLE_OVERLAP = 5
VIEW_NEW_ROLE = 6
VIEW_USERS = 7
VIEW_SKILL_CATALOG = 8

# (view index, nav label, sidebar section) — New Role is reached via the
# dedicated "+ New Role" button, not this list, so it isn't a normal nav item.
# Sections keep the two orthogonal concepts apart: ROLE = team seats (who),
# TOOLS = per-role MCP/plugin policy, SKILL = reusable knowledge files (what a
# role can *read*). "Skill Catalog" lives under its own SKILL section, NOT
# mixed in with role/policy views.
_NAV_VIEWS: tuple[tuple[int, str, str], ...] = (
    (VIEW_PIPELINE_BUILDER, "Pipeline Builder", "PIPELINE"),
    (VIEW_TEMPLATES, "Templates", "PIPELINE"),
    (VIEW_PROVIDERS_ROLES, "Providers & Roles", "ROLE"),
    (VIEW_ROLE_OVERLAP, "Role Overlap", "ROLE"),
    (VIEW_MCP_MATRIX, "MCP Matrix", "TOOLS"),
    (VIEW_PLUGINS_MATRIX, "Plugins Matrix", "TOOLS"),
    (VIEW_SKILL_CATALOG, "Skill Catalog", "SKILL"),
    (VIEW_USERS, "Users", "ACCOUNT"),
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
    VIEW_ROLE_OVERLAP: (
        "Role Overlap",
        "ตรวจว่า scope (instructions) ของแต่ละ role ทับกันแค่ไหน — TF-IDF ไม่ใช่ skill browser",
    ),
    VIEW_NEW_ROLE: ("New Role", "สร้าง custom role ใหม่"),
    VIEW_USERS: (
        "Users",
        "จัดการ Claude profile (add/remove, share sessions) + per-profile auth override",
    ),
    VIEW_SKILL_CATALOG: (
        "Skill Catalog",
        "skill จริงใน .claude/skills/ (SKILL.md) — ความรู้ที่ role อ้างถึง/อ่านได้",
    ),
}


# Roles offered a per-role CLI override in "Providers & Roles". Excludes
# lead/codex/gemini (provider_config.FORCED_ROLES — CLI is fixed) and shell
# (not a pipeline-eligible role — see pipeline_config.valid_roles()'s own
# note). A function — not a frozen tuple — since custom roles register at
# runtime and this must reflect them the next time the Settings window opens
# (SettingsWindow is constructed fresh on every open, so a function called
# from inside a `_build_*_view()` picks up a just-created role with no
# cockpit restart; a module-level constant computed once at import time
# never would).
def _overridable_roles() -> tuple[str, ...]:
    return tuple(
        r
        for r in pipeline_config.valid_roles()
        if r not in provider_config.FORCED_ROLES and r != "shell"
    )


_PROVIDER_DESC: dict[str, str] = {
    "codex": "OpenAI Codex CLI — second opinion / refactor cross-check",
    "gemini": "Google Antigravity (agy) — planning / long-context second opinion",
}


# Roles rendered as rows in the MCP/Plugins matrices — same set (and order)
# pane_tools_dialog.matrix_roles() defines, so a role's policy reads
# identically from either surface. Also a function, for the same
# fresh-per-open reason as `_overridable_roles()` above.
def _matrix_roles() -> tuple[str, ...]:
    return pane_tools_dialog.matrix_roles()


# Roles offered in the Pipeline Builder's role palette / per-hop add-role
# select. Every valid_roles() entry except "shell" — an ad-hoc terminal pane,
# not a directed pipeline participant (see `_overridable_roles()`'s own
# note). Also a function, for the same fresh-per-open reason.
def _pipeline_palette_roles() -> tuple[str, ...]:
    return tuple(r for r in pipeline_config.valid_roles() if r != "shell")


# New Role's "use default MCP+Plugins ตาม column" toggle (#6): no per-role
# policy exists yet for a freshly created custom role, so this maps the
# form's existing Dev(1)/Support(2) column choice onto the same MCP
# defaults the matching built-in roles already get in
# shared_dev_tools._ROLE_MCP_POLICY (dev column = frontend/backend/devops,
# lean; support column = qa/critic/designer, browser-driving).
_NEW_ROLE_COLUMN_MCPS: dict[int, frozenset[str]] = {
    1: frozenset(),
    2: frozenset({"playwright", "chrome-devtools"}),
}


def _append_skill_references(instructions: str, skills: list[skill_scan.SkillInfo]) -> str:
    """Embed a "## Skills ที่เกี่ยวข้อง" section listing every selected skill
    into the role's generated instructions text — applies whether
    `instructions` is the user's own typed text or the default template
    (custom_roles._default_role_template), so a selected skill is never
    silently dropped just because the Instructions box was left empty."""
    lines = "\n".join(
        f"- อ่าน skill: {s.name} — {s.description} ก่อนเริ่มงานที่เกี่ยวข้อง"
        if s.description
        else f"- อ่าน skill: {s.name} ก่อนเริ่มงานที่เกี่ยวข้อง"
        for s in skills
    )
    return f"{instructions.rstrip()}\n\n## Skills ที่เกี่ยวข้อง\n{lines}\n"


class SettingsWindow(QDialog):
    """The unified Settings window. One instance per open — construct fresh
    each time (mirrors the old, now-removed ``PaneToolsDialog``/
    ``PipelineSettingsDialog``'s no-singleton/no-caching pattern), so it
    always reflects on-disk state at open time."""

    def __init__(
        self,
        parent: QWidget | None = None,
        project: str | None = None,
        initial_view: int = VIEW_PROVIDERS_ROLES,
    ) -> None:
        super().__init__(parent)
        self._project = project
        self._dirty = False
        # Which views (VIEW_* indices) have unsaved staged edits right now —
        # per-view so Reset on one view doesn't clear another view's dirty
        # state, and so a fresh dialog with nothing touched keeps Save &
        # Apply disabled (#6/#16). `_dirty` above is kept as a plain bool
        # mirror (`bool(self._dirty_views)`) since existing call sites/tests
        # read it as "is anything unsaved" — see `_refresh_dirty_indicator`.
        self._dirty_views: set[int] = set()
        # Staged provider on/off — mirrors pipeline_dialog._PipelineBridge's
        # pending_provider_disabled contract: {provider: desired_disabled},
        # only for providers whose target differs from disk. Populated on
        # Save & Apply; the caller applies it via orchestrator.toggle_provider
        # AFTER exec() returns Accepted, so live Lead panes get the same
        # broadcast a status-bar chip click produces.
        self.pending_provider_disabled: dict[str, bool] = {}
        # Pipeline Builder/Templates share this in-memory copy of pipelines.json
        # (structural edits — Duplicate/Delete — write through immediately and
        # refresh it; hop edits stay staged here until Save & Apply).
        self._pipeline_payload = pipeline_config.load(self._project)

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
        # UI walkthrough #55 ("Header ซ้ำ 3 ที่ใน Settings") — the OS title
        # bar (setWindowTitle above), this faux titlebar, and the status
        # strip's brand label all said "takkub cockpit / settings" back to
        # back. Dropped the faux titlebar (purely decorative — traffic-light
        # dots + a duplicate title string); the OS titlebar + status strip
        # brand still identify the window.
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
            # Critic visual-review round-2 #3 — a bare template name (e.g.
            # "Feature (UI+API)") read as an unlabeled, unexplained pill that
            # looked like it had leaked in from the main window's plan chip.
            # It's a real per-project summary (see the walkthrough #56 note
            # below), so it stays — just prefixed + given a tooltip so its
            # purpose is self-evident instead of relying on the reader
            # already knowing what a "template" is in this cockpit.
            chip = cockpit_theme.gold_soft_chip(f"Template: {active_name}", strip)
            chip.setToolTip(
                "Pipeline template ที่ active อยู่สำหรับโปรเจคนี้ — เปลี่ยนได้ที่ Templates / Pipeline Builder"
            )
            lay.addWidget(chip)

        # UI walkthrough #56 — this used to also render a role-chip per
        # enabled role, duplicating the Providers & Roles view's own Roles
        # list (same names, same colors) one click away. Dropped; the active
        # template chip is the only per-project summary this strip needs.
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
        self._nav_indicators: dict[int, QFrame] = {}
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

            # Gemini #13 — the active-view marker is a 5px rounded color bar
            # in the design spec; QSS's `border-left` can't round only one
            # side of a box, so a real QFrame stands in for it instead.
            nav_row = QWidget(sidebar)
            nav_row_lay = QHBoxLayout(nav_row)
            nav_row_lay.setContentsMargins(0, 0, 0, 0)
            nav_row_lay.setSpacing(0)
            indicator = QFrame(nav_row)
            indicator.setObjectName("navIndicator")
            indicator.setFixedWidth(5)
            indicator.setVisible(False)
            nav_row_lay.addWidget(indicator)
            nav_row_lay.addWidget(btn, 1)
            lay.addWidget(nav_row)

            self._nav_buttons[view_idx] = btn
            self._nav_indicators[view_idx] = indicator

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
        for idx, indicator in self._nav_indicators.items():
            indicator.setVisible(idx == view_idx)
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
        # Index order MUST match the VIEW_* constants above (0–7 unchanged;
        # the real Skill Catalog is index 8, appended last).
        self._stack.addWidget(self._wrap_scroll(self._build_pipeline_builder_view()))
        self._stack.addWidget(self._wrap_scroll(self._build_templates_view()))
        self._stack.addWidget(self._wrap_scroll(self._build_providers_roles_view()))
        self._stack.addWidget(self._wrap_scroll(self._build_mcp_matrix_view()))
        self._stack.addWidget(self._wrap_scroll(self._build_plugins_matrix_view()))
        self._stack.addWidget(self._wrap_scroll(self._build_role_overlap_view()))
        self._stack.addWidget(self._wrap_scroll(self._build_new_role_view()))
        self._stack.addWidget(self._wrap_scroll(self._build_users_view()))
        self._stack.addWidget(self._wrap_scroll(self._build_skill_catalog_view()))
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

        # Codex Medium #5 — this button reloads the CURRENT view's on-disk
        # state (i.e. discards staged edits), it does not restore factory
        # defaults. "Reset to default" over-promised; label it for what it
        # actually does.
        reset_btn = cockpit_theme.secondary_button("↺ Revert unsaved changes", footer)
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
        # Gemini #16 — nothing staged yet at open time, so there's nothing
        # to apply; `_refresh_dirty_indicator` re-enables it the moment any
        # view goes dirty.
        self._save_btn.setEnabled(False)
        lay.addWidget(self._save_btn)

        return footer

    def _mark_dirty(self, *_args: object) -> None:
        self._dirty_views.add(self._stack.currentIndex())
        self._refresh_dirty_indicator()

    def _clear_dirty(self) -> None:
        self._dirty_views.clear()
        self._refresh_dirty_indicator()

    def _refresh_dirty_indicator(self) -> None:
        """Recompute the aggregate `_dirty` flag from `_dirty_views` and sync
        the footer's unsaved-dot/label + Save & Apply enabled state."""
        self._dirty = bool(self._dirty_views)
        self._unsaved_dot.setVisible(self._dirty)
        self._unsaved_label.setVisible(self._dirty)
        self._save_btn.setEnabled(self._dirty)

    def _on_reset_clicked(self) -> None:
        """Revert the currently-visible view's editable fields back to the
        on-disk state, clearing only THIS view's dirty flag — a different
        view's still-staged edits (#6) must survive. Templates/Role Overlap/
        Skill Catalog have nothing staged to reset (structural template edits
        write immediately; the audit and catalog are read-only), so they
        no-op."""
        idx = self._stack.currentIndex()
        if idx == VIEW_PROVIDERS_ROLES:
            self._reset_providers_roles_view()
        elif idx == VIEW_NEW_ROLE:
            self._reset_new_role_form()
        elif idx == VIEW_MCP_MATRIX:
            self._reload_mcp_matrix()
        elif idx == VIEW_PLUGINS_MATRIX:
            self._reload_plugins_matrix()
        elif idx == VIEW_PIPELINE_BUILDER and getattr(self, "_pb_template_id", None):
            self._load_pb_hops(self._pb_template_id)
        self._dirty_views.discard(idx)
        self._refresh_dirty_indicator()

    def _on_save_apply_clicked(self) -> None:
        """Persist every staged edit across all views in one Save & Apply:
        Providers & Roles, Pipeline Builder's in-flight hop edits (rolled
        into the same pipelines.json write as rolesEnabled), and the
        MCP/Plugins matrices. Templates' Duplicate/Delete commit
        independently (their own buttons), so this is a safe no-op for
        whichever of those the user never touched.

        New Role (#2) is a special case: the footer button doesn't "save
        provider/pipeline state" while that view is showing — it dispatches
        to the exact same create transaction as the in-view "+ Create Role"
        button, and only closes the dialog when that create actually
        succeeds (an invalid/incomplete form must not discard the user's
        typed input by accepting anyway).
        """
        if self._stack.currentIndex() == VIEW_NEW_ROLE:
            if self._on_create_role_clicked():
                self._dirty_views.discard(VIEW_NEW_ROLE)
                self._refresh_dirty_indicator()
                self.accept()
            return

        # Snapshot every on-disk store this transaction can touch so a
        # failure partway through (#3) rolls back instead of leaving stores
        # inconsistent — e.g. a role-provider override written but the
        # pipelines.json write failing right after, or a tools-policy write
        # failing after providers/roles already landed.
        snapshot_paths = (
            provider_config.config_path(self._project),
            pipeline_config.path(self._project),
            pane_tools_policy.PANE_TOOLS_POLICY_FILE,
        )
        snapshots = {p: (p.read_bytes() if p.exists() else None) for p in snapshot_paths}

        def _rollback() -> None:
            for p, content in snapshots.items():
                try:
                    if content is None:
                        p.unlink(missing_ok=True)
                    else:
                        p.write_bytes(content)
                except OSError:
                    pass

        try:
            self.pending_provider_disabled = {}
            for provider, toggle in self._provider_toggles.items():
                desired_disabled = not toggle.isChecked()
                if desired_disabled != provider_state.is_disabled(provider):
                    self.pending_provider_disabled[provider] = desired_disabled

            role_providers = {
                role: combo.currentData() for role, combo in self._role_provider_combos.items()
            }
            # scope=_overridable_roles() (#1): this page only renders a control
            # for these roles — anything else already on disk (a custom
            # role's override, say) must be preserved, not silently dropped
            # by a naive full-replace write.
            provider_config.save_role_overrides(
                role_providers, self._project, scope=_overridable_roles()
            )

            payload = pipeline_config.load(self._project)
            roles_enabled = dict(payload.get("rolesEnabled", {}))
            for role, toggle in self._role_toggles.items():
                roles_enabled[role] = toggle.isChecked()
            payload["rolesEnabled"] = roles_enabled

            pb_template_id = getattr(self, "_pb_template_id", None)
            if pb_template_id:
                for t in payload["templates"]:
                    if t["id"] == pb_template_id:
                        t["hops"] = [[dict(entry) for entry in hop] for hop in self._pb_hops]
                        break

            pipeline_config.save(payload, self._project)
            self._pipeline_payload = pipeline_config.load(self._project)

            updated_mcps = pane_tools_dialog.matrix_to_role_items(
                {
                    role: {item: t.isChecked() for item, t in items.items()}
                    for role, items in self._mcp_toggles.items()
                }
            )
            updated_plugins = pane_tools_dialog.matrix_to_role_items(
                {
                    role: {item: t.isChecked() for item, t in items.items()}
                    for role, items in self._plugin_toggles.items()
                }
            )
            mcp_changes = pane_tools_dialog.diff_role_items(self._orig_mcp_items, updated_mcps)
            plugin_changes = pane_tools_dialog.diff_role_items(
                self._orig_plugin_items, updated_plugins
            )
            # Write BOTH kinds for every role that changed EITHER — see
            # pane_tools_dialog._on_save_clicked's own note: set_role_items
            # seeds a fresh role entry's sibling kind to [] (an explicit deny),
            # so a plugins-only persist would silently wipe that role's MCPs.
            # set_role_items() never raises (validation/IO failures return
            # False) — check it explicitly so a failed write here triggers
            # the same rollback+error path as the other stages instead of
            # silently continuing as if it had succeeded.
            hidden = getattr(self, "_hidden_plugin_defaults", {})
            for role in set(mcp_changes) | set(plugin_changes):
                if not pane_tools_policy.set_role_items(role, "mcps", updated_mcps[role]):
                    raise OSError(f"เขียน tools policy ของ role '{role}' (mcps) ไม่สำเร็จ")
                if not pane_tools_policy.set_role_items(
                    role, "plugins", updated_plugins[role] + hidden.get(role, [])
                ):
                    raise OSError(f"เขียน tools policy ของ role '{role}' (plugins) ไม่สำเร็จ")
            if mcp_changes or plugin_changes:
                shared_dev_tools.regen_role_variants()
            self._orig_mcp_items = updated_mcps
            self._orig_plugin_items = updated_plugins
        except OSError as e:
            _rollback()
            self._pipeline_payload = pipeline_config.load(self._project)
            QMessageBox.critical(
                self, "Save failed", f"บันทึกไม่สำเร็จ (rolled back ทุก store ที่แก้ไปแล้ว): {e}"
            )
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
                provider,
                provider_role.color if provider_role else cockpit_theme.ROLE_COLOR_FALLBACK,
            )
            row_lay.addWidget(cockpit_theme.role_chip(provider.capitalize(), color, row))
            desc = QLabel(_PROVIDER_DESC.get(provider, ""), row)
            desc.setObjectName("panelHint")
            row_lay.addWidget(desc, 1)
            toggle = cockpit_theme.ToggleSwitch(
                row, checked=not provider_state.is_disabled(provider)
            )
            toggle.setAccessibleName(f"{provider.capitalize()} provider")
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
        role_providers = provider_config.role_provider_map(_overridable_roles(), self._project)

        self._role_toggles = {}
        self._role_provider_combos = {}
        self._role_provider_badges: dict[str, QLabel] = {}

        lead_row = self._build_role_row(
            "lead",
            "Lead",
            cockpit_theme.ROLE_COLORS["lead"],
            "Cockpit coordinator — provider fixed, always on",
            role_panel,
            locked=True,
        )
        rp_lay.addWidget(lead_row)

        for role in _overridable_roles():
            r = roles_mod.by_name(role)
            label = r.label if r else role.capitalize()
            color = cockpit_theme.ROLE_COLORS.get(
                role, r.color if r else cockpit_theme.ROLE_COLOR_FALLBACK
            )
            row = self._build_role_row(
                role,
                label,
                color,
                "",
                role_panel,
                locked=False,
                enabled=roles_enabled.get(role, True),
                current_provider=role_providers.get(role, provider_config.CLAUDE),
                deletable=role in custom_roles.list_role_names(),
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
        deletable: bool = False,
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
            # Gemini #8/#15 — the switch is now painted muted (see
            # ToggleSwitch.paintEvent's isEnabled() branch); pair that with
            # an accessible name/tooltip so keyboard/screen-reader users get
            # the same "locked on, not a live control" signal.
            toggle.setAccessibleName(f"{label} provider — always on, locked")
            toggle.setToolTip("Lead provider เป็น Claude เสมอ — ปิด/สลับไม่ได้")
            row_lay.addWidget(toggle)
            return row

        combo = QComboBox(row)
        for provider in sorted(provider_config.VALID_PROVIDERS):
            combo.addItem(provider.capitalize(), provider)
        idx = combo.findData(current_provider)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        row_lay.addWidget(combo)
        self._role_provider_combos[role] = combo

        # Gemini #12 — surface when the role's configured provider would
        # actually be substituted by Claude right now (toggled off or not
        # installed), as a styled badge rather than plain banner text.
        badge = QLabel("→ Claude", row)
        badge.setObjectName("substituteBadge")
        badge.setToolTip("provider นี้ปิดหรือยังไม่ติดตั้ง — Claude รับตำแหน่งแทน")
        row_lay.addWidget(badge)
        self._role_provider_badges[role] = badge

        combo.currentIndexChanged.connect(lambda _i=0, r=role: self._sync_role_provider_badge(r))
        combo.currentIndexChanged.connect(self._mark_dirty)
        self._sync_role_provider_badge(role)

        toggle = cockpit_theme.ToggleSwitch(row, checked=enabled)
        toggle.setAccessibleName(f"{label} role — {'enabled' if enabled else 'disabled'}")
        toggle.setToolTip(f"เปิด/ปิด role {label} ในทีม")
        toggle.toggled.connect(self._mark_dirty)
        row_lay.addWidget(toggle)
        self._role_toggles[role] = toggle

        # Critic visual-review round-2 #1 — a custom role could be created
        # but never removed from this view (Nielsen #3, user control &
        # freedom). Built-in roles never get this button (deletable=False
        # is the caller's default), so there's no way to delete a shipped
        # role by mistake.
        if deletable:
            delete_btn = QPushButton("✕", row)
            delete_btn.setFixedWidth(28)
            delete_btn.setToolTip(f"ลบ custom role '{role}'")
            delete_btn.setAccessibleName(f"Delete {label} role")
            delete_btn.clicked.connect(
                lambda _checked=False, r=role, w=row: self._on_delete_custom_role_clicked(r, w)
            )
            row_lay.addWidget(delete_btn)

        return row

    def _on_delete_custom_role_clicked(self, role: str, row: QWidget) -> None:
        role_file = custom_roles.role_file_path(role)
        confirm = QMessageBox.question(
            self,
            "Delete role",
            f"ลบ custom role '{role}'?\n\n"
            f"จะลบทั้ง registry entry และไฟล์ instructions ({role_file.name}) — undo ไม่ได้",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if not custom_roles.delete_role(role):
            QMessageBox.critical(self, "Delete failed", f"ลบ role '{role}' ไม่สำเร็จ")
            return
        roles_mod.unregister_role(role)
        self._role_toggles.pop(role, None)
        self._role_provider_combos.pop(role, None)
        self._role_provider_badges.pop(role, None)
        layout = row.parentWidget().layout() if row.parentWidget() else None
        if layout is not None:
            layout.removeWidget(row)
        row.deleteLater()

    def _sync_role_provider_badge(self, role: str) -> None:
        """Show/hide the "→ Claude" substitute badge for `role` based on its
        combo's CURRENT selection (not what's on disk) — reflects what would
        happen if the user saves with this selection right now."""
        combo = self._role_provider_combos.get(role)
        badge = self._role_provider_badges.get(role)
        if combo is None or badge is None:
            return
        from .provider_config import CLAUDE, _provider_available

        provider = combo.currentData()
        badge.setVisible(provider != CLAUDE and not _provider_available(provider))

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

        role_providers = provider_config.role_provider_map(_overridable_roles(), self._project)
        for role, combo in self._role_provider_combos.items():
            combo.blockSignals(True)
            idx = combo.findData(role_providers.get(role, provider_config.CLAUDE))
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)
            self._sync_role_provider_badge(role)

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
        self._nr_name.textChanged.connect(self._mark_dirty)
        name_col.addWidget(self._nr_name)
        name_row.addLayout(name_col, 1)

        label_col = QVBoxLayout()
        label_col.addWidget(QLabel("Label", form))
        self._nr_label = QLineEdit(form)
        self._nr_label.setPlaceholderText("🧬 Data Eng")
        self._nr_label.textChanged.connect(self._mark_dirty)
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
        self._nr_column.currentIndexChanged.connect(self._mark_dirty)
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
        self._nr_row.valueChanged.connect(self._mark_dirty)
        row_col.addWidget(self._nr_row)
        grid_row.addLayout(row_col)
        grid_row.addStretch(1)
        f_lay.addLayout(grid_row)

        f_lay.addWidget(QLabel("Accent", form))
        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(6)
        # Codex/Gemini #17 — a gray not in the selectable palette meant no
        # swatch showed as "selected" on first open; default to the
        # palette's own first color instead.
        self._nr_color = project_nav._AVATAR_COLORS[0]
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
        self._nr_default_tools_toggle.toggled.connect(self._mark_dirty)
        toggle_row.addWidget(self._nr_default_tools_toggle)
        f_lay.addLayout(toggle_row)
        tools_hint = QLabel(
            "เปิด (แนะนำ) = role นี้ได้ default MCP/Plugins ตาม column (Dev=เปล่า, "
            "Support=playwright+chrome-devtools) · ปิด = ไม่มี MCP/Plugins เลย ตั้งเองทีหลังผ่าน "
            "MCP Matrix / Plugins Matrix",
            form,
        )
        tools_hint.setObjectName("panelHint")
        tools_hint.setWordWrap(True)
        f_lay.addWidget(tools_hint)

        f_lay.addWidget(QLabel("Skills ที่ role นี้ควรรู้จัก", form))
        skills_hint = QLabel(
            "สแกนจาก .claude/skills/ จริงในโปรเจค — ติ๊กเพื่อฝัง reference "
            "เข้า instructions ให้อัตโนมัติตอนบันทึก role นี้ (ปุ่ม Create Role "
            "หรือ Save & Apply ด้านล่างทำเหมือนกัน)",
            form,
        )
        skills_hint.setObjectName("panelHint")
        skills_hint.setWordWrap(True)
        f_lay.addWidget(skills_hint)
        self._nr_skills_container = QWidget(form)
        self._nr_skills_lay = QVBoxLayout(self._nr_skills_container)
        self._nr_skills_lay.setContentsMargins(0, 2, 0, 2)
        self._nr_skills_lay.setSpacing(4)
        f_lay.addWidget(self._nr_skills_container)
        self._nr_skill_checks: list[tuple[skill_scan.SkillInfo, QCheckBox]] = []
        self._reload_new_role_skills()

        f_lay.addWidget(QLabel("Instructions", form))
        self._nr_instructions = QPlainTextEdit(form)
        self._nr_instructions.setPlaceholderText("บอก role ตัวเองว่าทำหน้าที่อะไร ขอบเขตงานคืออะไร...")
        self._nr_instructions.setMinimumHeight(90)
        self._nr_instructions.textChanged.connect(self._mark_dirty)
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
        self._mark_dirty()

    def _update_swatch_selection(self) -> None:
        for btn, color in zip(self._nr_swatch_btns, project_nav._AVATAR_COLORS, strict=False):
            border = cockpit_theme.ACCENT_GOLD if color == self._nr_color else "transparent"
            btn.setStyleSheet(
                f"background:{color}; border-radius:10px; border: 2px solid {border};"
            )

    def _new_role_skill_roots(self) -> list[Path]:
        """Where to look for real `.claude/skills/` — every configured path
        of the currently-active project first (so project-specific skills
        win a name collision), plus the cockpit's own checkout as a
        fallback/supplement (dogfooding: cockpit-ui-style etc. are relevant
        to any role, and this keeps the picker non-empty even when no
        project is open, e.g. in tests that construct SettingsWindow()
        bare)."""
        roots: list[Path] = []
        if self._project:
            roots.extend(_allowed_project_roots(self._project))
        roots.append(config.REPO_ROOT)
        return roots

    def _reload_new_role_skills(self) -> None:
        while self._nr_skills_lay.count():
            item = self._nr_skills_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._nr_skill_checks = []

        skills = skill_scan.scan_skills(self._new_role_skill_roots())
        if not skills:
            empty = QLabel("ไม่พบ skill ใน .claude/skills/ ของโปรเจคนี้", self._nr_skills_container)
            empty.setObjectName("panelHint")
            self._nr_skills_lay.addWidget(empty)
            return
        for skill in skills:
            text = f"{skill.name} — {skill.description}" if skill.description else skill.name
            chk = QCheckBox(text, self._nr_skills_container)
            chk.setToolTip(skill.description or skill.name)
            chk.toggled.connect(self._mark_dirty)
            self._nr_skills_lay.addWidget(chk)
            self._nr_skill_checks.append((skill, chk))

    def _selected_new_role_skills(self) -> list[skill_scan.SkillInfo]:
        return [skill for skill, chk in self._nr_skill_checks if chk.isChecked()]

    def _on_create_role_clicked(self) -> bool:
        """Validate + persist the New Role form. Returns True iff the role
        was actually created — the footer Save & Apply button (#2) uses this
        return value to decide whether it's safe to close the dialog; an
        invalid/incomplete form must not accept() and discard typed input."""
        name = self._nr_name.text().strip().lower()
        label = self._nr_label.text().strip()
        column = self._nr_column.currentData()
        row = self._nr_row.value()
        instructions_text = self._nr_instructions.toPlainText().strip()
        selected_skills = self._selected_new_role_skills()
        if selected_skills:
            base = instructions_text or custom_roles._default_role_template(
                name, label or name.capitalize()
            )
            instructions = _append_skill_references(base, selected_skills)
        else:
            instructions = instructions_text or None

        ok, err = custom_roles.create_role(name, label, self._nr_color, column, row, instructions)
        if not ok:
            self._nr_status.setText(f"⚠️ {err}")
            return False

        # Register in THIS process immediately so `--role <name>` spawns
        # without waiting for a cockpit restart (roles.py otherwise only
        # loads custom-roles.json at boot) — same pattern pane_tools_dialog
        # uses for the same reason.
        role = custom_roles.load_custom_roles().get(name)
        if role is not None:
            roles_mod.register_role(role)

        tools_ok = self._apply_new_role_tools_policy(name, column)

        status = (
            f"✓ สร้าง role '{name}' แล้ว — spawn ได้ทันทีด้วย "
            f'`takkub assign --role {name} "..."` (ไม่ต้อง restart cockpit)'
        )
        if not tools_ok:
            status += "\n⚠️ แต่บันทึก MCP/Plugins default ไม่สำเร็จ — ตั้งเองผ่าน MCP/Plugins Matrix"
        self._nr_status.setText(status)
        self._reset_new_role_form(clear_status=False)
        return True

    def _apply_new_role_tools_policy(self, name: str, column: int) -> bool:
        """Seed pane_tools_policy for a freshly created role. Checked "use
        default" → the same MCP defaults the matching dev/support-column
        built-in role gets, plus the lean teammate plugin set; unchecked →
        an explicit empty policy the operator fills in later via the
        MCP/Plugins Matrix views.

        Returns True iff both writes succeeded — `set_role_items()` never
        raises (validation/IO failures return False), so the caller must
        check this explicitly rather than assume the seed always lands (and
        `regen_role_variants()` only runs when it actually did)."""
        if self._nr_default_tools_toggle.isChecked():
            from .lead_context import _TEAMMATE_PLUGINS

            mcps = list(_NEW_ROLE_COLUMN_MCPS.get(column, frozenset()))
            plugins = list(_TEAMMATE_PLUGINS)
        else:
            mcps, plugins = [], []
        mcps_ok = pane_tools_policy.set_role_items(name, "mcps", mcps)
        plugins_ok = pane_tools_policy.set_role_items(name, "plugins", plugins)
        if mcps_ok and plugins_ok:
            shared_dev_tools.regen_role_variants()
        return mcps_ok and plugins_ok

    def _reset_new_role_form(self, clear_status: bool = True) -> None:
        # Block signals while programmatically resetting — every field is
        # wired to _mark_dirty (#6) so a plain .clear()/.setValue() here
        # would immediately re-mark the view dirty right after a successful
        # create, or right after the user asked to revert it.
        for w in (
            self._nr_name,
            self._nr_label,
            self._nr_instructions,
            self._nr_column,
            self._nr_row,
            self._nr_default_tools_toggle,
        ):
            w.blockSignals(True)
        try:
            self._nr_name.clear()
            self._nr_label.clear()
            self._nr_instructions.clear()
            self._nr_column.setCurrentIndex(1)
            self._nr_row.setValue(99)
            self._nr_default_tools_toggle.setChecked(True)
        finally:
            for w in (
                self._nr_name,
                self._nr_label,
                self._nr_instructions,
                self._nr_column,
                self._nr_row,
                self._nr_default_tools_toggle,
            ):
                w.blockSignals(False)
        for _skill, chk in self._nr_skill_checks:
            chk.blockSignals(True)
            chk.setChecked(False)
            chk.blockSignals(False)
        self._nr_color = project_nav._AVATAR_COLORS[0]
        self._update_swatch_selection()
        if clear_status:
            self._nr_status.setText("")

    # ──────────────────────────────────────────────────────────
    # view: Users (real — ported from user_actions.open_user_profiles_dialog,
    # 2026-07-11: previously a standalone modal QDialog reached only via the
    # 👥 Team chip's right-click menu; the user wanted it back as a directly
    # visible Team tab, not a popup). Every action here writes through
    # immediately (add/remove/share profile, save auth) — same "list ธรรมดา,
    # ไม่มี OK/Cancel" browse-and-act pattern as Skill Catalog, so this view
    # never participates in the footer's dirty-tracking/Save & Apply (no
    # _mark_dirty calls below, mirroring Skill Catalog/Templates).
    # ──────────────────────────────────────────────────────────

    def _build_users_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(10)

        self._up_profiles: list[dict] = user_profile.list_profiles()

        tabs = QTabWidget(view)
        tabs.addTab(self._build_users_profiles_tab(tabs), "Profiles")
        tabs.addTab(self._build_users_auth_tab(tabs), "Claude Auth")
        lay.addWidget(tabs, 1)

        self._up_status = QLabel("", view)
        self._up_status.setObjectName("panelHint")
        self._up_status.setWordWrap(True)
        lay.addWidget(self._up_status)

        return view

    def _users_status(self, msg: str) -> None:
        self._up_status.setText(msg)

    def _build_users_profiles_tab(self, parent: QWidget) -> QWidget:
        tab = QWidget(parent)
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        list_panel = QWidget(tab)
        list_panel.setObjectName("panel")
        lp_lay = QVBoxLayout(list_panel)
        lp_lay.setContentsMargins(14, 12, 14, 12)
        lp_lay.setSpacing(8)
        lp_title = QLabel("Existing profiles", list_panel)
        lp_title.setObjectName("panelTitle")
        lp_lay.addWidget(lp_title)
        lp_hint = QLabel("'default' cannot be removed", list_panel)
        lp_hint.setObjectName("panelHint")
        lp_lay.addWidget(lp_hint)

        self._up_profile_list = QListWidget(list_panel)
        self._up_profile_list.setFrameShape(QFrame.Shape.NoFrame)
        for p in self._up_profiles:
            self._up_profile_list.addItem(f"{p['name']}  →  {p['config_dir']}")
        lp_lay.addWidget(self._up_profile_list)

        btn_row = QHBoxLayout()
        self._up_remove_btn = cockpit_theme.secondary_button("Remove selected", list_panel)
        self._up_remove_btn.setEnabled(False)
        self._up_share_btn = cockpit_theme.secondary_button(
            "🔗 Share sessions with default", list_panel
        )
        self._up_share_btn.setEnabled(False)
        self._up_share_btn.setToolTip(
            "Convert this profile to shared-session mode: its existing\n"
            "sessions/todos/plugins/skills are merged into the default\n"
            "profile (nothing overwritten, originals kept as *.pre-share-backup),\n"
            "then linked — from then on switching users changes ONLY the\n"
            "account; history and plugins are the same everywhere."
        )
        btn_row.addWidget(self._up_remove_btn)
        btn_row.addWidget(self._up_share_btn)
        btn_row.addStretch(1)
        lp_lay.addLayout(btn_row)
        lay.addWidget(list_panel)

        self._up_profile_list.currentRowChanged.connect(self._on_users_profile_row_changed)
        self._up_remove_btn.clicked.connect(self._on_users_remove_profile_clicked)
        self._up_share_btn.clicked.connect(self._on_users_share_profile_clicked)

        add_panel = QWidget(tab)
        add_panel.setObjectName("panel")
        ap_lay = QVBoxLayout(add_panel)
        ap_lay.setContentsMargins(14, 12, 14, 12)
        ap_lay.setSpacing(8)
        ap_title = QLabel("Add new profile", add_panel)
        ap_title.setObjectName("panelTitle")
        ap_lay.addWidget(ap_title)

        form = QFormLayout()
        self._up_add_name = QLineEdit(add_panel)
        self._up_add_name.setPlaceholderText("e.g. work, personal")
        self._up_add_dir = QLineEdit(add_panel)
        self._up_add_dir.setPlaceholderText("path to Claude config dir, e.g. ~/.claude-work")
        dir_row = QWidget(add_panel)
        dir_row_lay = QHBoxLayout(dir_row)
        dir_row_lay.setContentsMargins(0, 0, 0, 0)
        dir_row_lay.addWidget(self._up_add_dir)
        browse_btn = cockpit_theme.secondary_button("Browse…", add_panel)
        browse_btn.setFixedWidth(84)
        dir_row_lay.addWidget(browse_btn)
        form.addRow("Name:", self._up_add_name)
        form.addRow("Config dir:", dir_row)

        self._up_add_share_chk = QCheckBox(
            "🔗 Share sessions/plugins with default (switch account only)", add_panel
        )
        self._up_add_share_chk.setChecked(True)
        self._up_add_share_chk.setToolTip(
            "Recommended. The new profile links sessions/todos/plugins/skills\n"
            "to the default profile — switching users changes ONLY the login.\n"
            "Uncheck for a fully isolated profile (old behaviour).\n"
            "Leave Config dir blank to use ~/.claude-<name>."
        )
        form.addRow("", self._up_add_share_chk)
        ap_lay.addLayout(form)

        browse_btn.clicked.connect(self._on_users_browse_clicked)

        add_btn = cockpit_theme.gold_button("+ Add Profile", add_panel)
        add_btn.clicked.connect(self._on_users_add_profile_clicked)
        ap_lay.addWidget(add_btn)

        lay.addWidget(add_panel)
        lay.addStretch(1)
        return tab

    def _on_users_profile_row_changed(self, row: int) -> None:
        self._up_remove_btn.setEnabled(row > 0)  # row 0 = "default", not removable
        self._up_share_btn.setEnabled(row > 0)

    def _on_users_remove_profile_clicked(self) -> None:
        row = self._up_profile_list.currentRow()
        if row <= 0 or row >= len(self._up_profiles):
            return
        try:
            user_profile.remove_profile(self._up_profiles[row]["name"])
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot remove", str(exc))
            return
        # Unlink shared junctions FIRST so a later manual delete of the
        # profile folder can't traverse a junction into ~/.claude data.
        try:
            user_profile.cleanup_profile_links(self._up_profiles[row]["config_dir"])
        except Exception:
            pass
        self._up_profile_list.takeItem(row)
        self._up_profiles.pop(row)
        self._reload_users_auth_combo()

    def _on_users_share_profile_clicked(self) -> None:
        row = self._up_profile_list.currentRow()
        if row <= 0 or row >= len(self._up_profiles):
            return
        p = self._up_profiles[row]
        confirm = QMessageBox.question(
            self,
            "Share sessions?",
            f"Convert '{p['name']}' ({p['config_dir']}) to shared-session mode?\n\n"
            "• Its sessions/todos/plugins/skills merge into the default\n"
            "  profile — nothing is overwritten, originals are kept as\n"
            "  *.pre-share-backup inside the profile dir.\n"
            "• Login/credentials stay separate — only the account differs.\n"
            "• Panes already open keep their old view until respawned.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            return
        results = user_profile.convert_profile_to_shared(p["config_dir"])
        QMessageBox.information(
            self,
            "Shared-session conversion",
            "\n".join(f"{k}: {v}" for k, v in results.items()),
        )

    def _on_users_browse_clicked(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select Claude config directory")
        if d:
            self._up_add_dir.setText(d)

    def _on_users_add_profile_clicked(self) -> None:
        n = self._up_add_name.text().strip()
        d = self._up_add_dir.text().strip()
        if not n:
            return
        if not d:
            if not self._up_add_share_chk.isChecked():
                return  # isolated profiles must name their dir explicitly
            d = str(Path.home() / f".claude-{n}")
        try:
            linked = user_profile.add_profile(
                n, d, share_sessions=self._up_add_share_chk.isChecked()
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid profile", str(exc))
            return
        new_p = {"name": n, "config_dir": d}
        self._up_profiles.append(new_p)
        suffix = "  🔗shared" if linked else ""
        self._up_profile_list.addItem(f"{n}  →  {d}{suffix}")
        self._up_add_name.clear()
        self._up_add_dir.clear()
        self._reload_users_auth_combo()
        if linked:
            self._users_status(
                f"👤 profile '{n}' created — shares {', '.join(linked)} with default · "
                "run 'claude login' in a pane of that profile to sign in"
            )

    def _build_users_auth_tab(self, parent: QWidget) -> QWidget:
        tab = QWidget(parent)
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        intro = QLabel(
            "Point a profile's Claude Code panes at a different backend — DeepSeek,\n"
            "OpenRouter, a local model — instead of Anthropic. These settings are\n"
            "saved *per profile*: leave them blank and that profile keeps its normal\n"
            "Claude login; set a base URL and only that profile's panes use the API.\n"
            "Applies to the next pane you spawn (restart open panes to pick it up).",
            tab,
        )
        intro.setObjectName("panelHint")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        panel = QWidget(tab)
        panel.setObjectName("panel")
        self._up_auth_panel = panel
        p_lay = QVBoxLayout(panel)
        p_lay.setContentsMargins(14, 12, 14, 12)
        p_lay.setSpacing(8)

        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Settings for profile:", panel))
        self._up_auth_combo = QComboBox(panel)
        for p in self._up_profiles:
            self._up_auth_combo.addItem(p["name"])
        self._up_auth_combo.setToolTip(
            "Each profile has its own auth. Switching reloads that profile's saved\n"
            "values from disk — Save before switching to keep unsaved edits."
        )
        sel_row.addWidget(self._up_auth_combo, 1)
        p_lay.addLayout(sel_row)

        auth_form = QFormLayout()
        auth_form.setHorizontalSpacing(16)
        auth_form.setVerticalSpacing(8)
        p_lay.addLayout(auth_form)

        self._up_base_url = QLineEdit(panel)
        self._up_base_url.setPlaceholderText(
            "blank = Anthropic  ·  e.g. https://api.deepseek.com/anthropic"
        )
        auth_form.addRow("Base URL:", self._up_base_url)

        self._up_api_key = QLineEdit(panel)
        self._up_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._up_api_key.setPlaceholderText("your provider's API key  ·  blank = none")
        auth_form.addRow("API key:", self._up_api_key)

        self._up_auth_token = QLineEdit(panel)
        self._up_auth_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._up_auth_token.setPlaceholderText(
            "usually blank — the API key above is reused as the bearer token"
        )
        auth_form.addRow("Auth token:", self._up_auth_token)

        note = QLabel(
            "Examples:\n"
            "• DeepSeek — Base URL: https://api.deepseek.com/anthropic + API key: your DeepSeek key\n"
            "• OpenRouter — Base URL: https://openrouter.ai/api + Auth token: your OpenRouter key\n"
            "  (then add ANTHROPIC_DEFAULT_SONNET_MODEL below to choose the model)",
            panel,
        )
        note.setObjectName("panelHint")
        note.setWordWrap(True)
        p_lay.addWidget(note)

        env_label = QLabel(
            "Extra environment variables — sent to every pane. Use for a provider key,\n"
            "or to pick a model (e.g. ANTHROPIC_DEFAULT_SONNET_MODEL = qwen/qwen3-coder:free):",
            panel,
        )
        env_label.setObjectName("panelHint")
        env_label.setWordWrap(True)
        p_lay.addWidget(env_label)

        self._up_env_rows: list[tuple[QLineEdit, QLineEdit, QWidget]] = []
        self._up_env_rows_box = QVBoxLayout()
        self._up_env_rows_box.setSpacing(4)
        p_lay.addLayout(self._up_env_rows_box)

        add_env_btn = cockpit_theme.secondary_button("+ Add variable", panel)
        add_env_btn.clicked.connect(lambda: self._add_users_env_row())
        p_lay.addWidget(add_env_btn)

        save_row = QHBoxLayout()
        save_btn = cockpit_theme.gold_button("💾 Save", panel)
        save_btn.clicked.connect(self._on_users_save_auth_clicked)
        save_row.addWidget(save_btn)
        save_row.addStretch(1)
        p_lay.addLayout(save_row)

        lay.addWidget(panel)
        lay.addStretch(1)

        self._up_auth_combo.currentTextChanged.connect(self._load_users_auth_profile)
        self._load_users_auth_profile(self._up_auth_combo.currentText())

        return tab

    def _on_users_save_auth_clicked(self) -> None:
        profile_name = self._up_auth_combo.currentText()
        env_dict: dict[str, str] = {}
        for name_ed, value_ed, _row in self._up_env_rows:
            name = name_ed.text().strip()
            if name:
                env_dict[name] = value_ed.text()
        try:
            save_claude_auth(
                ClaudeAuthConfig(
                    base_url=self._up_base_url.text(),
                    api_key=self._up_api_key.text(),
                    auth_token=self._up_auth_token.text(),
                    extra_env=env_dict,
                ),
                self._users_auth_dir(profile_name),
            )
            self._users_status(
                f"Claude auth saved for profile '{profile_name}' — respawn its "
                "panes to use the new settings."
            )
        except OSError as e:
            QMessageBox.critical(
                self, "Save failed", f"Couldn't write takkub-claude-auth.json:\n{e}"
            )

    def _users_auth_dir(self, profile_name: str) -> Path | None:
        """config_dir for *profile_name* (None → default ~/.claude)."""
        for p in self._up_profiles:
            if p["name"] == profile_name:
                return Path(p["config_dir"])
        return None

    def _reload_users_auth_combo(self) -> None:
        """Refresh the Claude Auth tab's profile combo after Profiles-tab
        add/remove — keeps the current selection if it still exists,
        otherwise falls back to row 0 (matches _build_role_row's
        find-or-default pattern used elsewhere in this window)."""
        combo = self._up_auth_combo
        current = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        for p in self._up_profiles:
            combo.addItem(p["name"])
        idx = combo.findText(current)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)
        self._load_users_auth_profile(combo.currentText())

    def _load_users_auth_profile(self, profile_name: str) -> None:
        """Populate the auth fields from *profile_name*'s saved config."""
        loaded = load_claude_auth(self._users_auth_dir(profile_name))
        self._up_base_url.setText(loaded.base_url)
        self._up_api_key.setText(loaded.api_key)
        self._up_auth_token.setText(loaded.auth_token)
        self._clear_users_env_rows()
        for name, value in loaded.extra_env.items():
            self._add_users_env_row(name, value)
        if not self._up_env_rows:
            self._add_users_env_row()

    def _add_users_env_row(self, name: str = "", value: str = "") -> None:
        row = QWidget(self._up_auth_panel)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        name_edit = QLineEdit(name, row)
        name_edit.setPlaceholderText("NAME — e.g. ANTHROPIC_DEFAULT_SONNET_MODEL")
        value_edit = QLineEdit(value, row)
        value_edit.setPlaceholderText("value — e.g. qwen/qwen3-coder:free")
        remove_btn = QPushButton("✕", row)
        remove_btn.setFixedWidth(28)
        remove_btn.setToolTip("Remove this variable")

        h.addWidget(name_edit, 2)
        h.addWidget(value_edit, 3)
        h.addWidget(remove_btn, 0)

        entry = (name_edit, value_edit, row)
        self._up_env_rows.append(entry)
        self._up_env_rows_box.addWidget(row)

        def _remove() -> None:
            if entry in self._up_env_rows:
                self._up_env_rows.remove(entry)
            self._up_env_rows_box.removeWidget(row)
            row.deleteLater()

        remove_btn.clicked.connect(_remove)

    def _clear_users_env_rows(self) -> None:
        for _n, _v, row in list(self._up_env_rows):
            self._up_env_rows_box.removeWidget(row)
            row.deleteLater()
        self._up_env_rows.clear()

    # ──────────────────────────────────────────────────────────
    # shared: role×item toggle matrix (MCP Matrix / Plugins Matrix)
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _clear_grid(grid: QGridLayout) -> None:
        while grid.count():
            item = grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _populate_matrix_grid(
        self,
        grid: QGridLayout,
        roles: tuple[str, ...],
        items: list[str],
        matrix: dict[str, dict[str, bool]],
    ) -> dict[str, dict[str, cockpit_theme.ToggleSwitch]]:
        """Fill *grid* (already parented to a panel widget) with a role×item
        toggle matrix: col 0 = 180px role-chip column, cols 1..N = 1fr item
        columns of centered ``ToggleSwitch`` cells. Clears any prior content
        first, so this doubles as the reload/refresh path."""
        self._clear_grid(grid)
        panel = grid.parentWidget()
        grid.setColumnMinimumWidth(0, 160)
        grid.setColumnStretch(0, 0)
        for col, item in enumerate(items, start=1):
            header = QLabel(item, panel)
            header.setObjectName("panelHint")
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.setToolTip(item)
            grid.addWidget(header, 0, col)
            grid.setColumnStretch(col, 1)

        boxes: dict[str, dict[str, cockpit_theme.ToggleSwitch]] = {}
        for row, role in enumerate(roles, start=1):
            r = roles_mod.by_name(role)
            label = r.label if r else role.capitalize()
            color = cockpit_theme.ROLE_COLORS.get(
                role, r.color if r else cockpit_theme.ROLE_COLOR_FALLBACK
            )
            grid.addWidget(cockpit_theme.role_chip(label, color, panel), row, 0)
            boxes[role] = {}
            for col, item in enumerate(items, start=1):
                checked = matrix.get(role, {}).get(item, False)
                toggle = cockpit_theme.ToggleSwitch(panel, checked=checked)
                toggle.toggled.connect(self._mark_dirty)
                cell = QWidget(panel)
                cell_lay = QHBoxLayout(cell)
                cell_lay.setContentsMargins(0, 4, 0, 4)
                cell_lay.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignCenter)
                grid.addWidget(cell, row, col)
                boxes[role][item] = toggle
        return boxes

    # ──────────────────────────────────────────────────────────
    # view: MCP Matrix (real)
    # ──────────────────────────────────────────────────────────

    def _build_mcp_matrix_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(12)

        add_row = QHBoxLayout()
        add_btn = cockpit_theme.secondary_button("+ Add MCP server", view)
        add_btn.clicked.connect(self._on_add_mcp_server_clicked)
        add_row.addWidget(add_btn)
        add_row.addStretch(1)
        lay.addLayout(add_row)

        self._mcp_empty = QLabel(
            "ยังไม่มี MCP server ใน master registry — กด “+ Add MCP server”", view
        )
        self._mcp_empty.setObjectName("panelHint")
        self._mcp_empty.setWordWrap(True)
        self._mcp_empty.hide()
        lay.addWidget(self._mcp_empty)

        matrix_panel = QWidget(view)
        matrix_panel.setObjectName("panel")
        self._mcp_grid = QGridLayout(matrix_panel)
        self._mcp_grid.setContentsMargins(14, 12, 14, 12)
        self._mcp_grid.setHorizontalSpacing(6)
        self._mcp_grid.setVerticalSpacing(4)
        lay.addWidget(matrix_panel)
        lay.addStretch(1)

        self._reload_mcp_matrix()
        return view

    def _reload_mcp_matrix(self) -> None:
        items = pane_tools_dialog.master_mcps()
        self._orig_mcp_items = pane_tools_dialog.policy_role_items(_matrix_roles(), "mcps")
        matrix = pane_tools_dialog.build_matrix(_matrix_roles(), items, self._orig_mcp_items)
        self._mcp_toggles = self._populate_matrix_grid(
            self._mcp_grid, _matrix_roles(), items, matrix
        )
        self._mcp_empty.setVisible(not items)

    def _on_add_mcp_server_clicked(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("+ Add MCP server")
        dlg.setStyleSheet(self.styleSheet())
        dlg.setMinimumWidth(420)
        form = QFormLayout(dlg)
        form.setContentsMargins(18, 18, 18, 14)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        name_edit = QLineEdit(dlg)
        command_edit = QLineEdit(dlg)
        args_edit = QLineEdit(dlg)
        args_edit.setPlaceholderText("-y some-mcp-package (เว้นวรรคคั่น)")
        form.addRow("ชื่อ", name_edit)
        form.addRow("Command", command_edit)
        form.addRow("Args", args_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dlg
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        parsed = pane_tools_dialog.parse_install_form(
            name_edit.text(), command_edit.text(), args_edit.text()
        )
        if parsed is None:
            QMessageBox.warning(self, "Add MCP server", "กรอกชื่อและ command ให้ครบ")
            return
        name, cfg = parsed
        try:
            if not shared_dev_tools.add_mcp_server(name, cfg):
                QMessageBox.warning(self, "Add MCP server ไม่สำเร็จ", f"เพิ่ม '{name}' ไม่ได้")
                return
        except Exception as e:
            QMessageBox.warning(self, "Add MCP server ไม่สำเร็จ", str(e))
            return
        self._reload_mcp_matrix()
        self._mark_dirty()

    # ──────────────────────────────────────────────────────────
    # view: Plugins Matrix (real)
    # ──────────────────────────────────────────────────────────

    def _build_plugins_matrix_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(12)

        banner = QLabel(
            "security-guidance และ remember ถูก denylist ปิดเสมอทุก pane "
            "(hook หนัก ทำ spawn ช้า) — policy นี้เปิดให้ไม่ได้",
            view,
        )
        banner.setObjectName("infoBanner")
        banner.setWordWrap(True)
        lay.addWidget(banner)

        self._plugins_empty = QLabel(
            "ไม่พบ marketplace plugin — ยังไม่มีอะไรใน installed_plugins.json", view
        )
        self._plugins_empty.setObjectName("panelHint")
        self._plugins_empty.setWordWrap(True)
        self._plugins_empty.hide()
        lay.addWidget(self._plugins_empty)

        matrix_panel = QWidget(view)
        matrix_panel.setObjectName("panel")
        self._plugins_grid = QGridLayout(matrix_panel)
        self._plugins_grid.setContentsMargins(14, 12, 14, 12)
        self._plugins_grid.setHorizontalSpacing(6)
        self._plugins_grid.setVerticalSpacing(4)
        lay.addWidget(matrix_panel)
        lay.addStretch(1)

        self._reload_plugins_matrix()
        return view

    def _reload_plugins_matrix(self) -> None:
        # Marketplace-granular columns (NOT name@marketplace) — see
        # pane_tools_dialog.discover_marketplaces's own note: the policy
        # stores marketplace names, so only these make a checkbox's identity
        # match what gets read back on Save.
        items = pane_tools_dialog.discover_marketplaces()
        full_orig = pane_tools_dialog.policy_role_items(_matrix_roles(), "plugins")
        rendered = set(items)
        # A role's built-in default can name a marketplace with no column
        # here (not installed on this machine) — stash it so Save re-adds it
        # instead of silently dropping it as "unchecked".
        self._hidden_plugin_defaults = {
            r: [m for m in v if m not in rendered] for r, v in full_orig.items()
        }
        self._orig_plugin_items = {r: [m for m in v if m in rendered] for r, v in full_orig.items()}
        matrix = pane_tools_dialog.build_matrix(_matrix_roles(), items, self._orig_plugin_items)
        self._plugin_toggles = self._populate_matrix_grid(
            self._plugins_grid, _matrix_roles(), items, matrix
        )
        self._plugins_empty.setVisible(not items)

    # ──────────────────────────────────────────────────────────
    # view: Role Overlap (real — ROLE section)
    #
    # NOT a skill browser: it audits how much each ROLE's instruction doc
    # overlaps every other role's scope (TF-IDF cosine on `.claude/agents/*`),
    # so an operator can see whether a role duplicates territory. Historically
    # mislabeled "Skill Catalog"; renamed 2026-07-11 to say what it does. The
    # real skill browser is `_build_skill_catalog_view` (SKILL section) below.
    # ──────────────────────────────────────────────────────────

    def _build_role_overlap_view(self) -> QWidget:
        view = QWidget(self)
        lay = QHBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(12)

        self._overlap_docs = skill_audit.load_all_role_docs()

        list_panel = QWidget(view)
        list_panel.setObjectName("panel")
        list_panel.setFixedWidth(200)
        list_lay = QVBoxLayout(list_panel)
        list_lay.setContentsMargins(6, 6, 6, 6)
        self._overlap_list = QListWidget(list_panel)
        self._overlap_list.setFrameShape(QFrame.Shape.NoFrame)
        for name in sorted(self._overlap_docs):
            r = roles_mod.by_name(name)
            item = QListWidgetItem(r.label if r else name.capitalize(), self._overlap_list)
            item.setData(Qt.ItemDataRole.UserRole, name)
        self._overlap_list.currentItemChanged.connect(self._on_overlap_role_selected)
        list_lay.addWidget(self._overlap_list)
        lay.addWidget(list_panel)

        detail_panel = QWidget(view)
        detail_panel.setObjectName("panel")
        detail_lay = QVBoxLayout(detail_panel)
        detail_lay.setContentsMargins(14, 12, 14, 12)
        detail_lay.setSpacing(8)
        self._overlap_badge = QLabel("", detail_panel)
        self._overlap_badge.setObjectName("panelHint")
        self._overlap_badge.setWordWrap(True)
        detail_lay.addWidget(self._overlap_badge)
        self._overlap_detail_text = QPlainTextEdit(detail_panel)
        self._overlap_detail_text.setReadOnly(True)
        self._overlap_detail_text.setStyleSheet(f'font-family: "{self._fonts["mono"]}";')
        detail_lay.addWidget(self._overlap_detail_text, 1)
        lay.addWidget(detail_panel, 1)

        if self._overlap_list.count():
            self._overlap_list.setCurrentRow(0)
        return view

    def _on_overlap_role_selected(self, current: QListWidgetItem | None, *_args: object) -> None:
        if current is None:
            return
        role = current.data(Qt.ItemDataRole.UserRole)
        self._overlap_detail_text.setPlainText(self._overlap_docs.get(role, ""))
        overlaps = skill_audit.audit_existing_role(role, self._overlap_docs)
        if overlaps:
            names = ", ".join(f"{other} ({sim:.2f})" for other, sim in overlaps)
            self._overlap_badge.setText(f"⚠️ overlap กับ scope role อื่น: {names}")
        else:
            self._overlap_badge.setText("✓ won't overlap — ไม่ทับ scope role อื่น")

    # ──────────────────────────────────────────────────────────
    # view: Skill Catalog (real — SKILL section)
    #
    # The genuine skill browser: lists real Claude Code skills scanned from
    # `.claude/skills/*/SKILL.md` (same `skill_scan` the New Role picker uses,
    # across the active project's roots + the cockpit checkout). For each
    # skill it shows name + description + which ROLE instruction docs mention
    # it (substring match on the skill name over `skill_audit.load_all_role_
    # docs()`), so an operator sees who already relies on a given skill.
    # Read-only browse — no dirty-tracking / Save (mirrors Role Overlap).
    # ──────────────────────────────────────────────────────────

    def _build_skill_catalog_view(self) -> QWidget:
        view = QWidget(self)
        lay = QHBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(12)

        self._catalog_skills = skill_scan.scan_skills(self._new_role_skill_roots())
        self._catalog_role_docs = skill_audit.load_all_role_docs()

        list_panel = QWidget(view)
        list_panel.setObjectName("panel")
        list_panel.setFixedWidth(220)
        list_lay = QVBoxLayout(list_panel)
        list_lay.setContentsMargins(6, 6, 6, 6)
        self._catalog_list = QListWidget(list_panel)
        self._catalog_list.setFrameShape(QFrame.Shape.NoFrame)
        for skill in self._catalog_skills:
            item = QListWidgetItem(skill.name, self._catalog_list)
            item.setData(Qt.ItemDataRole.UserRole, skill.name)
            item.setToolTip(skill.description or skill.name)
        self._catalog_list.currentItemChanged.connect(self._on_catalog_skill_selected)
        list_lay.addWidget(self._catalog_list)
        lay.addWidget(list_panel)

        detail_panel = QWidget(view)
        detail_panel.setObjectName("panel")
        detail_lay = QVBoxLayout(detail_panel)
        detail_lay.setContentsMargins(14, 12, 14, 12)
        detail_lay.setSpacing(8)

        self._catalog_name = QLabel("", detail_panel)
        self._catalog_name.setObjectName("panelTitle")
        detail_lay.addWidget(self._catalog_name)
        self._catalog_desc = QLabel("", detail_panel)
        self._catalog_desc.setObjectName("panelHint")
        self._catalog_desc.setWordWrap(True)
        detail_lay.addWidget(self._catalog_desc)
        self._catalog_roles = QLabel("", detail_panel)
        self._catalog_roles.setObjectName("panelHint")
        self._catalog_roles.setWordWrap(True)
        detail_lay.addWidget(self._catalog_roles)
        self._catalog_path = QLabel("", detail_panel)
        self._catalog_path.setObjectName("panelHint")
        self._catalog_path.setWordWrap(True)
        detail_lay.addWidget(self._catalog_path)
        detail_lay.addStretch(1)
        lay.addWidget(detail_panel, 1)

        if self._catalog_list.count():
            self._catalog_list.setCurrentRow(0)
        else:
            self._catalog_name.setText("— ไม่พบ skill —")
            self._catalog_desc.setText(
                "ยังไม่มี .claude/skills/*/SKILL.md ในโปรเจคนี้หรือ cockpit checkout"
            )
        return view

    def _roles_referencing_skill(self, skill_name: str) -> list[str]:
        """Role instruction docs that reference `skill_name` as a whole word
        (case-insensitive, word-boundary regex — NOT a raw substring). This
        matches both the generated ``อ่าน skill: <name>`` marker that
        `_append_skill_references` embeds and hand-authored prose that names the
        skill directly, while a short/common name (``git``, ``test``) no longer
        false-positives on unrelated words like "github" or "latest"."""
        pattern = re.compile(rf"\b{re.escape(skill_name)}\b", re.IGNORECASE)
        hits = [role for role, doc in self._catalog_role_docs.items() if pattern.search(doc)]
        return sorted(hits)

    def _on_catalog_skill_selected(self, current: QListWidgetItem | None, *_args: object) -> None:
        if current is None:
            return
        name = current.data(Qt.ItemDataRole.UserRole)
        skill = next((s for s in self._catalog_skills if s.name == name), None)
        if skill is None:
            return
        self._catalog_name.setText(skill.name)
        self._catalog_desc.setText(skill.description or "(ไม่มี description ใน frontmatter)")
        refs = self._roles_referencing_skill(skill.name)
        if refs:
            labels = ", ".join(
                (roles_mod.by_name(r).label if roles_mod.by_name(r) else r) for r in refs
            )
            self._catalog_roles.setText(f"อ้างถึงโดย role: {labels}")
        else:
            self._catalog_roles.setText("ยังไม่มี role ไหนอ้างถึง skill นี้")
        self._catalog_path.setText(f"📄 {skill.path}")

    # ──────────────────────────────────────────────────────────
    # view: Pipeline Builder (real)
    # ──────────────────────────────────────────────────────────

    def _build_pipeline_builder_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(12)

        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Editing template:", view))
        self._pb_template_combo = QComboBox(view)
        sel_row.addWidget(self._pb_template_combo, 1)
        lay.addLayout(sel_row)

        palette_panel = QWidget(view)
        palette_panel.setObjectName("panel")
        pal_lay = QHBoxLayout(palette_panel)
        pal_lay.setContentsMargins(12, 10, 12, 10)
        pal_lay.setSpacing(6)
        pal_hint = QLabel("+ hop เดี่ยวจาก role:", palette_panel)
        pal_hint.setObjectName("panelHint")
        pal_lay.addWidget(pal_hint)
        for role in _pipeline_palette_roles():
            r = roles_mod.by_name(role)
            label = r.label if r else role.capitalize()
            color = cockpit_theme.ROLE_COLORS.get(
                role, r.color if r else cockpit_theme.ROLE_COLOR_FALLBACK
            )
            btn = QPushButton(label, palette_panel)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1px solid {color};"
                f" border-radius: 999px; color: {color}; padding: 3px 10px; font-weight: 600;"
                f" font-size: 11px; }}"
                f"QPushButton:hover {{ background: rgba(255,255,255,0.06); }}"
            )
            btn.clicked.connect(
                lambda _checked=False, role_=role: self._on_palette_role_clicked(role_)
            )
            pal_lay.addWidget(btn)
        pal_lay.addStretch(1)
        lay.addWidget(palette_panel)

        self._pb_hops_container = QWidget(view)
        self._pb_hops_lay = QVBoxLayout(self._pb_hops_container)
        self._pb_hops_lay.setContentsMargins(0, 0, 0, 0)
        self._pb_hops_lay.setSpacing(4)
        lay.addWidget(self._pb_hops_container)

        add_hop_btn = QPushButton("+ Add hop", view)
        add_hop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_hop_btn.setStyleSheet(
            f"QPushButton {{ border: 1px dashed {cockpit_theme.BORDER_STRONG}; border-radius:"
            f" {cockpit_theme.RADIUS_SM}px; color: {cockpit_theme.TEXT_MUTED}; padding: 8px; }}"
            f"QPushButton:hover {{ color: {cockpit_theme.TEXT_PRIMARY}; border-color:"
            f" {cockpit_theme.ACCENT_GOLD}; }}"
        )
        add_hop_btn.clicked.connect(self._on_add_hop_clicked)
        lay.addWidget(add_hop_btn)
        lay.addStretch(1)

        self._reload_pb_template_combo()
        self._pb_template_combo.currentIndexChanged.connect(self._on_pb_template_changed)
        self._load_pb_hops(self._pipeline_payload.get("activeTemplate", ""))
        return view

    def _reload_pb_template_combo(self) -> None:
        self._pb_template_combo.blockSignals(True)
        self._pb_template_combo.clear()
        for t in self._pipeline_payload.get("templates", []):
            badge = "  ·  BUILT-IN" if t.get("builtin") else ""
            self._pb_template_combo.addItem(f"{t['name']}{badge}", t["id"])
        active = self._pipeline_payload.get("activeTemplate", "")
        idx = self._pb_template_combo.findData(active)
        self._pb_template_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._pb_template_combo.blockSignals(False)

    def _on_pb_template_changed(self, _index: int) -> None:
        template_id = self._pb_template_combo.currentData()
        if template_id:
            self._load_pb_hops(template_id)

    def _load_pb_hops(self, template_id: str) -> None:
        tpl = next((t for t in self._pipeline_payload["templates"] if t["id"] == template_id), None)
        self._pb_template_id = template_id
        self._pb_hops: list[list[dict]] = (
            [[dict(entry) for entry in hop] for hop in tpl["hops"]] if tpl else []
        )
        self._render_pb_hops()

    def _render_pb_hops(self) -> None:
        while self._pb_hops_lay.count():
            item = self._pb_hops_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        for idx, hop in enumerate(self._pb_hops):
            panel = QWidget(self._pb_hops_container)
            panel.setObjectName("panel")
            p_lay = QVBoxLayout(panel)
            p_lay.setContentsMargins(12, 10, 12, 10)
            p_lay.setSpacing(6)

            head_row = QHBoxLayout()
            head_lbl = QLabel(f"HOP {idx + 1}", panel)
            head_lbl.setObjectName("panelTitle")
            head_row.addWidget(head_lbl)
            if len(hop) > 1:
                chip = QLabel("parallel", panel)
                chip.setStyleSheet(
                    f"background: {cockpit_theme.PARALLEL_CHIP_BG}; border: 1px solid"
                    f" {cockpit_theme.PARALLEL_CHIP_BORDER}; border-radius: 999px; color:"
                    f" {cockpit_theme.PARALLEL_CHIP_TEXT}; padding: 2px 8px; font-size: 11px;"
                    f" font-weight: 600;"
                )
                head_row.addWidget(chip)
            head_row.addStretch(1)
            remove_hop_btn = QPushButton("✕", panel)
            remove_hop_btn.setFixedSize(22, 22)
            remove_hop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            remove_hop_btn.setToolTip("ลบ hop นี้")
            remove_hop_btn.clicked.connect(
                lambda _checked=False, i=idx: self._on_remove_hop_clicked(i)
            )
            head_row.addWidget(remove_hop_btn)
            p_lay.addLayout(head_row)

            roles_row = QHBoxLayout()
            roles_row.setSpacing(6)
            for entry in hop:
                role = entry["role"]
                r = roles_mod.by_name(role)
                label = r.label if r else role.capitalize()
                color = cockpit_theme.ROLE_COLORS.get(
                    role, r.color if r else cockpit_theme.ROLE_COLOR_FALLBACK
                )
                pill = QWidget(panel)
                pill.setStyleSheet("background: rgba(255,255,255,0.05); border-radius: 999px;")
                pill_lay = QHBoxLayout(pill)
                pill_lay.setContentsMargins(8, 3, 4, 3)
                pill_lay.setSpacing(4)
                pill_lay.addWidget(cockpit_theme.role_chip(label, color, pill))
                rm_btn = QPushButton("✕", pill)
                rm_btn.setFixedSize(16, 16)
                rm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                rm_btn.setStyleSheet(
                    "QPushButton { border: none; background: transparent; font-size: 10px; }"
                )
                rm_btn.clicked.connect(
                    lambda _checked=False, i=idx, ro=role: self._on_remove_hop_role_clicked(i, ro)
                )
                pill_lay.addWidget(rm_btn)
                roles_row.addWidget(pill)

            add_combo = QComboBox(panel)
            add_combo.addItem("+ add role", None)
            used = {e["role"] for e in hop}
            for role in _pipeline_palette_roles():
                if role in used:
                    continue
                r = roles_mod.by_name(role)
                add_combo.addItem(r.label if r else role.capitalize(), role)
            add_combo.currentIndexChanged.connect(
                lambda _index=0, i=idx, combo=add_combo: self._on_hop_add_role_selected(i, combo)
            )
            roles_row.addWidget(add_combo)
            roles_row.addStretch(1)
            p_lay.addLayout(roles_row)

            self._pb_hops_lay.addWidget(panel)

            if idx < len(self._pb_hops) - 1:
                conn = QLabel("↓ wait for all", self._pb_hops_container)
                conn.setObjectName("panelHint")
                conn.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self._pb_hops_lay.addWidget(conn)

    def _on_add_hop_clicked(self) -> None:
        self._pb_hops.append([])
        self._render_pb_hops()
        self._mark_dirty()

    def _on_remove_hop_clicked(self, idx: int) -> None:
        if 0 <= idx < len(self._pb_hops):
            del self._pb_hops[idx]
            self._render_pb_hops()
            self._mark_dirty()

    def _on_remove_hop_role_clicked(self, idx: int, role: str) -> None:
        if 0 <= idx < len(self._pb_hops):
            self._pb_hops[idx] = [e for e in self._pb_hops[idx] if e["role"] != role]
            self._render_pb_hops()
            self._mark_dirty()

    def _on_hop_add_role_selected(self, idx: int, combo: QComboBox) -> None:
        role = combo.currentData()
        if role is None or not (0 <= idx < len(self._pb_hops)):
            return
        self._pb_hops[idx].append(
            {"role": role, "cwd": "", "requiresCommit": False, "autoChain": False}
        )
        self._render_pb_hops()
        self._mark_dirty()

    def _on_palette_role_clicked(self, role: str) -> None:
        self._pb_hops.append(
            [{"role": role, "cwd": "", "requiresCommit": False, "autoChain": False}]
        )
        self._render_pb_hops()
        self._mark_dirty()

    # ──────────────────────────────────────────────────────────
    # view: Templates (real)
    # ──────────────────────────────────────────────────────────

    def _build_templates_view(self) -> QWidget:
        view = QWidget(self)
        lay = QHBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(12)

        list_panel = QWidget(view)
        list_panel.setObjectName("panel")
        list_panel.setFixedWidth(220)
        list_lay = QVBoxLayout(list_panel)
        list_lay.setContentsMargins(6, 6, 6, 6)
        self._tpl_list = QListWidget(list_panel)
        self._tpl_list.setFrameShape(QFrame.Shape.NoFrame)
        self._tpl_list.currentItemChanged.connect(self._on_template_selected)
        list_lay.addWidget(self._tpl_list)
        lay.addWidget(list_panel)
        lay.setAlignment(list_panel, Qt.AlignmentFlag.AlignTop)

        detail_panel = QWidget(view)
        detail_panel.setObjectName("panel")
        d_lay = QVBoxLayout(detail_panel)
        d_lay.setContentsMargins(14, 12, 14, 12)
        d_lay.setSpacing(10)

        self._tpl_title = QLabel("", detail_panel)
        self._tpl_title.setObjectName("panelTitle")
        d_lay.addWidget(self._tpl_title)

        self._tpl_hops_summary = QLabel("", detail_panel)
        self._tpl_hops_summary.setWordWrap(True)
        self._tpl_hops_summary.setObjectName("panelHint")
        d_lay.addWidget(self._tpl_hops_summary)
        d_lay.addStretch(1)

        btn_row = QHBoxLayout()
        self._tpl_edit_btn = cockpit_theme.secondary_button("Edit hops →", detail_panel)
        self._tpl_edit_btn.clicked.connect(self._on_template_edit_hops_clicked)
        btn_row.addWidget(self._tpl_edit_btn)
        self._tpl_duplicate_btn = cockpit_theme.secondary_button("Duplicate", detail_panel)
        self._tpl_duplicate_btn.clicked.connect(self._on_template_duplicate_clicked)
        btn_row.addWidget(self._tpl_duplicate_btn)
        self._tpl_delete_btn = cockpit_theme.secondary_button("Delete", detail_panel)
        self._tpl_delete_btn.clicked.connect(self._on_template_delete_clicked)
        btn_row.addWidget(self._tpl_delete_btn)
        btn_row.addStretch(1)
        d_lay.addLayout(btn_row)

        lay.addWidget(detail_panel, 1)

        self._reload_templates_list()
        return view

    _TPL_ROW_HEIGHT = 34
    # list_panel.setFixedWidth(220) in _build_templates_view — the panel's
    # width never changes, so the label's available width can be computed
    # once here instead of chasing live resize events.
    _TPL_LIST_PANEL_WIDTH = 220
    _TPL_LIST_ROW_PADDING = 6 + 6 + 6 + 6 + 2  # list_lay margins + row_lay margins + border

    @staticmethod
    def _compact_chip_width(metrics: QFontMetrics, text: str) -> int:
        """Predicted pixel width of a `gold_soft_chip(..., compact=True)` —
        mirrors its QSS padding/border so callers can reserve layout space
        for it without needing a shown widget (critic #2026-07-10 v2:
        BUILT-IN chip was crowding out the template name)."""
        return (
            metrics.horizontalAdvance(text)
            + cockpit_theme.COMPACT_CHIP_HPAD
            + cockpit_theme.COMPACT_CHIP_BORDER
        )

    @staticmethod
    def _elide_template_name(metrics: QFontMetrics, name: str, avail_width: int) -> str:
        """Ellipsize `name` to fit `avail_width` px — critic #2026-07-10 v2:
        the raw QLabel used to hard-clip mid-glyph ("Feature (UI+API)" -> "Feature (UI+AP")
        instead of showing "…"."""
        return metrics.elidedText(name, Qt.TextElideMode.ElideRight, max(avail_width, 0))

    def _reload_templates_list(self) -> None:
        self._tpl_list.blockSignals(True)
        self._tpl_list.clear()
        metrics = QFontMetrics(self._tpl_list.font())
        row_spacing = 6
        for t in self._pipeline_payload.get("templates", []):
            item = QListWidgetItem(self._tpl_list)
            item.setData(Qt.ItemDataRole.UserRole, t["id"])
            item.setSizeHint(QSize(0, self._TPL_ROW_HEIGHT))
            row = QWidget(self._tpl_list)
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(6, 0, 6, 0)
            row_lay.setSpacing(row_spacing)

            builtin = bool(t.get("builtin"))
            chip_width = (
                self._compact_chip_width(metrics, "BUILT-IN") + row_spacing if builtin else 0
            )
            avail = self._TPL_LIST_PANEL_WIDTH - self._TPL_LIST_ROW_PADDING - chip_width
            name_label = QLabel(self._elide_template_name(metrics, t["name"], avail), row)
            name_label.setToolTip(t["name"])
            row_lay.addWidget(name_label, 1)
            if builtin:
                row_lay.addWidget(cockpit_theme.gold_soft_chip("BUILT-IN", row, compact=True))
            self._tpl_list.setItemWidget(item, row)
        self._tpl_list.blockSignals(False)
        # Cap the list's height to fit its item count instead of stretching
        # to the row's full height (critic #2026-07-10: 600px panel with 3
        # items) — the +8 covers the panel's 6px top/bottom content margins.
        count = self._tpl_list.count()
        self._tpl_list.setMaximumHeight(max(count, 1) * self._TPL_ROW_HEIGHT + 8)
        if count:
            self._tpl_list.setCurrentRow(0)
        else:
            self._on_template_selected(None, None)

    def _on_template_selected(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            self._tpl_selected_id = None
            self._tpl_title.setText("")
            self._tpl_hops_summary.setText("")
            self._tpl_delete_btn.setEnabled(False)
            return
        tid = current.data(Qt.ItemDataRole.UserRole)
        tpl = next((t for t in self._pipeline_payload["templates"] if t["id"] == tid), None)
        if tpl is None:
            return
        self._tpl_selected_id = tid
        self._tpl_title.setText(tpl["name"])
        lines = [
            f"HOP {i}: " + (", ".join(e["role"] for e in hop) or "(ว่าง)")
            for i, hop in enumerate(tpl["hops"], start=1)
        ]
        self._tpl_hops_summary.setText("\n↓ wait for all\n".join(lines) or "(ไม่มี hop)")
        self._tpl_delete_btn.setEnabled(not tpl.get("builtin"))

    def _on_template_edit_hops_clicked(self) -> None:
        tid = getattr(self, "_tpl_selected_id", None)
        if not tid:
            return
        self._load_pb_hops(tid)
        idx = self._pb_template_combo.findData(tid)
        if idx >= 0:
            self._pb_template_combo.blockSignals(True)
            self._pb_template_combo.setCurrentIndex(idx)
            self._pb_template_combo.blockSignals(False)
        self._goto_view(VIEW_PIPELINE_BUILDER)

    def _on_template_duplicate_clicked(self) -> None:
        tid = getattr(self, "_tpl_selected_id", None)
        tpl = next((t for t in self._pipeline_payload["templates"] if t["id"] == tid), None)
        if tpl is None:
            return
        base = f"{tpl['id']}-copy"
        existing_ids = {t["id"] for t in self._pipeline_payload["templates"]}
        new_id = base
        n = 2
        while new_id in existing_ids:
            new_id = f"{base}{n}"
            n += 1
        new_tpl = {
            "id": new_id,
            "name": f"{tpl['name']} (copy)",
            "builtin": False,
            "hops": [[dict(e) for e in hop] for hop in tpl["hops"]],
        }
        self._pipeline_payload["templates"].append(new_tpl)
        if not self._persist_pipeline_payload():
            return
        self._reload_templates_list()
        self._reload_pb_template_combo()

    def _on_template_delete_clicked(self) -> None:
        tid = getattr(self, "_tpl_selected_id", None)
        tpl = next((t for t in self._pipeline_payload["templates"] if t["id"] == tid), None)
        if tpl is None or tpl.get("builtin"):
            return
        confirm = QMessageBox.question(self, "Delete template", f"ลบ template '{tpl['name']}'?")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._pipeline_payload["templates"] = [
            t for t in self._pipeline_payload["templates"] if t["id"] != tid
        ]
        if self._pipeline_payload.get("activeTemplate") == tid:
            self._pipeline_payload["activeTemplate"] = self._pipeline_payload["templates"][0]["id"]
        if not self._persist_pipeline_payload():
            return
        self._reload_templates_list()
        self._reload_pb_template_combo()

    def _persist_pipeline_payload(self) -> bool:
        """Write ``self._pipeline_payload`` (Duplicate/Delete's immediate-commit
        path — same "writes right away" pattern as Add/Remove MCP) then
        re-read it back so built-in-hop-normalization stays in sync."""
        try:
            pipeline_config.save(self._pipeline_payload, self._project)
        except OSError as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return False
        self._pipeline_payload = pipeline_config.load(self._project)
        return True
