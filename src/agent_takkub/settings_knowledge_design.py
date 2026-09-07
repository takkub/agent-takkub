"""Knowledge & Design Settings views: the tabbed "Knowledge" page (Knowledge /
Design Tools tabs — originally `docs/plans/final-closeout-after-1.3.0/
04_SETTINGS_UI_FINAL.md` + `05_UI_EXAMPLES.md`'s `KNOWLEDGE & DESIGN` sidebar
section, collapsed into one tabbed nav entry, stripped of its OpenViking tab
during the settings-nav declutter (OpenViking was withdrawn from the product
entirely, not just hidden), and stripped of its Context Debug tab in the
#515 settings diet — that tab duplicated `takkub doctor`'s own context-trace
section; its one still-live control, Context Strategy, moved onto the
Knowledge tab instead, see `_build_knowledge_view`).

A mixin (`KnowledgeDesignSettingsMixin`) mixed into `settings_window.
SettingsWindow` — kept in its own file rather than growing
`settings_window.py` further. **This module must never import from
`settings_window`** (that direction already goes the other way).

Every view here is read-mostly and deliberately NOT wired into
`SettingsWindow`'s footer Save & Apply / dirty-tracking transaction —
Design Tools writes each credential through immediately on its own "Save
credential" button, and Knowledge is a pure read-only status panel (plus the
Context Strategy control, which write-throughs on click — see
`_on_kd_ctx_strategy_clicked`).

Every health/subprocess/network call (`graft --version`, a design-tool
connectivity probe) runs on a background `QThread` — a "run() emits
result-or-Exception, one `resultReady` signal" shape — never on the Qt main
thread. Nothing here fetches eagerly at view-construction time — even
lazily built (`settings_window._lazy_view_builders`), an eager fetch here
would still cost a network/subprocess round-trip the moment this section is
first opened; every panel starts as a "press Refresh/Test to load"
placeholder instead.

Secrets are never displayed once stored (`04_SETTINGS_UI_FINAL.md`: "Never
reveal saved secrets") — the credential field only ever accepts a NEW value
to write; `SecretManager.status()`/`get_secret()` is used to confirm
presence, never to populate the field.
"""

from __future__ import annotations

import json
import os

from PyQt6.QtCore import QCoreApplication, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme, core_v2_settings
from . import pane_tools_policy as pt_policy

_DESIGN_MCPS: tuple[tuple[str, str], ...] = (
    ("reference-21st", "21st.dev"),
    ("figma", "Figma"),
    ("penpot", "Penpot"),
)

# Context Strategy (v2-hardening C, `13_SIMPLE_UX.md`/`23_UI_EXAMPLES.md`) —
# value -> (button label, one-line description). Order is display order.
_CONTEXT_STRATEGY_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("fast", "Fast", "ประหยัดสุด งานเล็ก"),
    ("automatic", "Automatic", "ระบบประเมินเอง (แนะนำ)"),
    ("deep", "Deep", "ค้นเยอะสุด งานใหญ่/ซับซ้อน"),
)


# ──────────────────────────────────────────────────────────────
# generic worker thread — every call here is a callable with no args that
# either returns a plain value or raises; the signal always carries either
# the return value or the caught exception, never both.
# ──────────────────────────────────────────────────────────────


_ACTIVE_THREADS: set[_CallableThread] = set()
_shutdown_hooked = False


def _wait_for_active_threads() -> None:
    """`aboutToQuit` hook (registered lazily the first time a
    `_CallableThread` runs) — gives any still-in-flight worker a bounded
    window to finish before the process exits, instead of leaving it to be
    killed mid-write."""
    for thread in list(_ACTIVE_THREADS):
        thread.wait(3000)


class _CallableThread(QThread):
    """Generic background-callable worker (H3, 2026-09-07 hardening):
    deliberately unparented (`QThread.__init__(None)`, ignoring `parent`
    for Qt object-tree purposes) and kept alive by `_ACTIVE_THREADS`
    instead of Qt parent/child ownership. A `QThread` parented to a
    transient Settings dialog/window gets swept up in Qt's child-cleanup
    the moment that dialog is destroyed — and destroying a `QThread` while
    it `isRunning()` is a fatal abort (`QThread: Destroyed while thread is
    still running`, reproduced as exit 0xC0000409 with `settings_usage
    .py`'s own Refresh thread when its owning dialog was force-deleted
    mid-import). `finished` removes this instance from the registry and
    schedules `deleteLater()` once the run actually completes, so a closed
    dialog no longer takes an in-flight import/probe down with it.
    """

    resultReady: pyqtSignal = pyqtSignal(object)

    def __init__(self, fn, parent: QWidget | None = None) -> None:
        super().__init__(None)  # never Qt-parented — see class docstring
        self._fn = fn
        _ACTIVE_THREADS.add(self)
        self.finished.connect(self._cleanup)
        global _shutdown_hooked
        if not _shutdown_hooked:
            app = QCoreApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(_wait_for_active_threads)
                _shutdown_hooked = True

    def run(self) -> None:
        try:
            self.resultReady.emit(self._fn())
        except Exception as e:  # pragma: no cover - fail-open, surfaced in the UI
            self.resultReady.emit(e)

    def _cleanup(self) -> None:
        _ACTIVE_THREADS.discard(self)
        self.deleteLater()


def _status_dot_color(ok: bool | None) -> str:
    if ok is None:
        return cockpit_theme.TEXT_FAINT
    return cockpit_theme.STATE_OK if ok else cockpit_theme.STATE_WARN


class _RolePermissionsDialog(QDialog):
    """role x design-MCP grant matrix (`allow_item`/`deny_item`, kind
    "mcps") — the same on-disk policy the real MCP Matrix view and every
    pane-spawn permission check already read (`pane_tools_policy.
    effective_mcps`), so a grant made here takes effect identically."""

    def __init__(self, parent: QWidget, *, fonts: dict) -> None:
        super().__init__(parent)
        self.setWindowTitle("Design Tools — Permissions")
        self.setStyleSheet(parent.styleSheet())
        self.resize(420, 420)
        self._fonts = fonts

        lay = QVBoxLayout(self)
        hint = QLabel("role ที่ติ๊ก = อนุญาตให้ pane ของ role นั้นใช้ design MCP นี้ได้ (เขียนทันที)", self)
        hint.setObjectName("panelHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        from .settings_window import _matrix_roles

        self._checks: dict[tuple[str, str], QCheckBox] = {}
        grid = QGridLayout()
        for col, (_mcp_id, label) in enumerate(_DESIGN_MCPS, start=1):
            head = QLabel(label, self)
            head.setStyleSheet(f'font-family: "{fonts["mono"]}"; font-weight: 600;')
            grid.addWidget(head, 0, col)
        roles = _matrix_roles()
        for row, role in enumerate(roles, start=1):
            grid.addWidget(QLabel(role, self), row, 0)
            granted = pt_policy.effective_mcps(role, frozenset()) or frozenset()
            for col, (mcp_id, _label) in enumerate(_DESIGN_MCPS, start=1):
                cb = QCheckBox(self)
                cb.setChecked(mcp_id in granted)
                cb.toggled.connect(
                    lambda checked, r=role, m=mcp_id: self._on_toggled(r, m, checked)
                )
                grid.addWidget(cb, row, col, alignment=Qt.AlignmentFlag.AlignCenter)
                self._checks[(role, mcp_id)] = cb
        lay.addLayout(grid)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        lay.addWidget(buttons)

    def _on_toggled(self, role: str, mcp_id: str, checked: bool) -> None:
        if checked:
            pt_policy.allow_item(role, "mcps", mcp_id)
        else:
            pt_policy.deny_item(role, "mcps", mcp_id)


class KnowledgeDesignSettingsMixin:
    """Mixed into `SettingsWindow` — every method assumes `self` has the
    attributes `SettingsWindow.__init__` sets (`_project`, `_fonts`, …) plus
    the QSS-driven helpers (`_build_card_header`) that class already
    defines."""

    # ──────────────────────────────────────────────────────────
    # single sidebar entry ("Knowledge") — folded the former 4-page
    # KNOWLEDGE & DESIGN section (Knowledge/OpenViking/Design Tools/Context
    # Debug) into one page with tabs (OpenViking's tab was dropped outright
    # — the product withdrew OpenViking entirely, not just this UI). Context
    # Debug's own tab was dropped in the #515 settings diet — it duplicated
    # `takkub doctor`'s context-trace section (`core.context_sources.
    # doctor_section`, already wired to the same `load_last_trace()` this
    # tab used to read) — but its Context Strategy control was a real,
    # still-live pick (Fast/Automatic/Deep), not debug, so it moved onto
    # this Knowledge tab instead of disappearing with the rest of the page.
    # Each remaining tab keeps its own view-builder below unchanged; this
    # just composes them so `settings_window._build_content` only needs one
    # `_stack.addWidget` call for the whole section.
    # ──────────────────────────────────────────────────────────

    def _build_knowledge_tabbed_view(self) -> QWidget:
        tabs = QTabWidget(self)
        tabs.addTab(self._build_knowledge_view(), "Knowledge")
        tabs.addTab(self._build_design_tools_view(), "Design Tools")
        return tabs

    # ──────────────────────────────────────────────────────────
    # view: Knowledge (status overview + Context Strategy, #515)
    # ──────────────────────────────────────────────────────────

    def _build_knowledge_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(14)

        lay.addWidget(self._build_context_strategy_panel(view))

        panel = QWidget(view)
        panel.setObjectName("panel")
        p_lay = QVBoxLayout(panel)
        p_lay.setContentsMargins(14, 12, 14, 12)
        p_lay.setSpacing(8)
        header_row = QHBoxLayout()
        header_row.addWidget(self._build_card_header("KNOWLEDGE", "Sources", "", panel), 1)
        refresh_btn = cockpit_theme.secondary_button("Refresh", panel)
        refresh_btn.clicked.connect(self._on_kd_knowledge_refresh_clicked)
        header_row.addWidget(refresh_btn)
        p_lay.addLayout(header_row)

        self._kd_knowledge_rows: dict[str, tuple[QWidget, QLabel]] = {}
        for name in ("Brain", "Obsidian", "Graft"):
            row = QWidget(panel)
            row.setObjectName("providerRow")
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(10, 8, 10, 8)
            row_lay.setSpacing(10)
            dot = cockpit_theme.color_dot(cockpit_theme.TEXT_FAINT, row, size=8)
            row_lay.addWidget(dot)
            label = QLabel(name, row)
            label.setFixedWidth(110)
            label.setStyleSheet(f'font-family: "{self._fonts["mono"]}"; font-weight: 600;')
            row_lay.addWidget(label)
            detail = QLabel("กด “Refresh” เพื่อโหลดสถานะ", row)
            detail.setObjectName("panelHint")
            detail.setWordWrap(True)
            row_lay.addWidget(detail, 1)
            p_lay.addWidget(row)
            self._kd_knowledge_rows[name] = (dot, detail)
        lay.addWidget(panel)
        lay.addStretch(1)

        self._kd_knowledge_thread: _CallableThread | None = None
        return view

    def _on_kd_knowledge_refresh_clicked(self) -> None:
        if self._kd_knowledge_thread is not None and self._kd_knowledge_thread.isRunning():
            return
        for dot, detail in self._kd_knowledge_rows.values():
            dot.setStyleSheet(f"background: {cockpit_theme.TEXT_FAINT}; border-radius: 4px;")
            detail.setText("กำลังตรวจสอบ…")
        project = self._project
        thread = _CallableThread(lambda: _collect_knowledge_status(project), self)
        thread.resultReady.connect(self._on_kd_knowledge_ready)
        self._kd_knowledge_thread = thread
        thread.start()

    def _on_kd_knowledge_ready(self, result: object) -> None:
        if isinstance(result, Exception):
            for _dot, detail in self._kd_knowledge_rows.values():
                detail.setText(f"ตรวจสอบไม่สำเร็จ: {result}")
            return
        for name, (ok, detail_text) in result.items():
            dot, detail = self._kd_knowledge_rows[name]
            dot.setStyleSheet(f"background: {_status_dot_color(ok)}; border-radius: 4px;")
            detail.setText(detail_text)

    # ──────────────────────────────────────────────────────────
    # view: Design Tools
    # ──────────────────────────────────────────────────────────

    def _build_design_tools_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(14)

        panel = QWidget(view)
        panel.setObjectName("panel")
        p_lay = QVBoxLayout(panel)
        p_lay.setContentsMargins(14, 12, 14, 12)
        p_lay.setSpacing(8)
        header_row = QHBoxLayout()
        header_row.addWidget(self._build_card_header("DESIGN", "Integrations", "", panel), 1)
        refresh_btn = cockpit_theme.secondary_button("Refresh", panel)
        refresh_btn.clicked.connect(self._on_kd_design_refresh_clicked)
        header_row.addWidget(refresh_btn)
        p_lay.addLayout(header_row)

        self._kd_design_rows: dict[str, tuple[QWidget, QLabel]] = {}
        for name in ("Storybook", "21st.dev", "Figma", "Penpot"):
            row = QWidget(panel)
            row.setObjectName("providerRow")
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(10, 8, 10, 8)
            row_lay.setSpacing(10)
            dot = cockpit_theme.color_dot(cockpit_theme.TEXT_FAINT, row, size=8)
            row_lay.addWidget(dot)
            label = QLabel(name, row)
            label.setFixedWidth(110)
            label.setStyleSheet(f'font-family: "{self._fonts["mono"]}"; font-weight: 600;')
            row_lay.addWidget(label)
            detail = QLabel("กด “Refresh” เพื่อโหลดสถานะ", row)
            detail.setObjectName("panelHint")
            detail.setWordWrap(True)
            row_lay.addWidget(detail, 1)
            p_lay.addWidget(row)
            self._kd_design_rows[name] = (dot, detail)
        lay.addWidget(panel)

        cred_panel = QWidget(view)
        cred_panel.setObjectName("panel")
        cp_lay = QVBoxLayout(cred_panel)
        cp_lay.setContentsMargins(14, 12, 14, 12)
        cp_lay.setSpacing(8)
        cp_lay.addWidget(self._build_card_header("DESIGN", "Set credential", "", cred_panel))
        cred_note = QLabel(
            "ไม่แสดง credential ที่บันทึกไว้แล้ว — กรอกเพื่อบันทึกค่าใหม่ทับเท่านั้น "
            "(Base URL ต้องกรอกสำหรับ Penpot, เว้นว่างได้สำหรับ 21st.dev/Figma)",
            cred_panel,
        )
        cred_note.setObjectName("panelHint")
        cred_note.setWordWrap(True)
        cp_lay.addWidget(cred_note)

        cred_form = QFormLayout()
        self._kd_design_target_combo = QComboBox(cred_panel)
        for mcp_id, label in _DESIGN_MCPS:
            self._kd_design_target_combo.addItem(label, mcp_id)
        cred_form.addRow("Tool", self._kd_design_target_combo)
        self._kd_design_token_edit = QLineEdit(cred_panel)
        self._kd_design_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._kd_design_token_edit.setPlaceholderText("token / API key ใหม่")
        cred_form.addRow("Token", self._kd_design_token_edit)
        self._kd_design_base_url_edit = QLineEdit(cred_panel)
        self._kd_design_base_url_edit.setPlaceholderText("https://... (Penpot ต้องกรอก)")
        cred_form.addRow("Base URL", self._kd_design_base_url_edit)
        cp_lay.addLayout(cred_form)

        cred_btn_row = QHBoxLayout()
        save_cred_btn = cockpit_theme.gold_button("Save credential", cred_panel)
        save_cred_btn.clicked.connect(self._on_kd_design_save_credential_clicked)
        cred_btn_row.addWidget(save_cred_btn)
        self._kd_design_cred_status = QLabel("", cred_panel)
        self._kd_design_cred_status.setObjectName("panelHint")
        cred_btn_row.addWidget(self._kd_design_cred_status)
        cred_btn_row.addStretch(1)
        cp_lay.addLayout(cred_btn_row)
        lay.addWidget(cred_panel)

        action_row = QHBoxLayout()
        self._kd_design_test_btn = cockpit_theme.secondary_button("Test", view)
        self._kd_design_test_btn.clicked.connect(self._on_kd_design_test_clicked)
        action_row.addWidget(self._kd_design_test_btn)
        permissions_btn = cockpit_theme.secondary_button("Permissions", view)
        permissions_btn.clicked.connect(self._on_kd_design_permissions_clicked)
        action_row.addWidget(permissions_btn)
        action_row.addStretch(1)
        lay.addLayout(action_row)

        self._kd_design_result = QPlainTextEdit(view)
        self._kd_design_result.setReadOnly(True)
        self._kd_design_result.setStyleSheet(
            f'font-family: "{self._fonts["mono"]}"; font-size: 12px;'
        )
        self._kd_design_result.setFixedHeight(100)
        lay.addWidget(self._kd_design_result)
        lay.addStretch(1)

        self._kd_design_thread: _CallableThread | None = None
        return view

    def _on_kd_design_refresh_clicked(self) -> None:
        if self._kd_design_thread is not None and self._kd_design_thread.isRunning():
            return
        for dot, detail in self._kd_design_rows.values():
            dot.setStyleSheet(f"background: {cockpit_theme.TEXT_FAINT}; border-radius: 4px;")
            detail.setText("กำลังตรวจสอบ…")
        project = self._project
        thread = _CallableThread(lambda: _collect_design_tools_status(project), self)
        thread.resultReady.connect(self._on_kd_design_status_ready)
        self._kd_design_thread = thread
        thread.start()

    def _on_kd_design_status_ready(self, result: object) -> None:
        if isinstance(result, Exception):
            for _dot, detail in self._kd_design_rows.values():
                detail.setText(f"ตรวจสอบไม่สำเร็จ: {result}")
            return
        for name, (ok, detail_text) in result.items():
            dot, detail = self._kd_design_rows[name]
            dot.setStyleSheet(f"background: {_status_dot_color(ok)}; border-radius: 4px;")
            detail.setText(detail_text)

    def _on_kd_design_save_credential_clicked(self) -> None:
        mcp_id = self._kd_design_target_combo.currentData()
        token = self._kd_design_token_edit.text().strip()
        base_url = self._kd_design_base_url_edit.text().strip()
        if not token:
            self._kd_design_cred_status.setText("กรอก token ก่อนบันทึก")
            return
        if mcp_id == "penpot" and not base_url:
            self._kd_design_cred_status.setText("Penpot ต้องกรอก Base URL ด้วย")
            return

        if mcp_id == "figma":
            value = token
        elif mcp_id == "penpot":
            value = json.dumps({"token": token, "base_url": base_url})
        else:  # reference-21st
            value = json.dumps({"api_key": token, "base_url": base_url}) if base_url else token

        from .core.secrets.manager import SecretManager

        try:
            SecretManager().set_secret(f"secret://{mcp_id}/default", value)
        except Exception as e:
            self._kd_design_cred_status.setText(f"บันทึกไม่สำเร็จ: {e}")
            return
        self._kd_design_token_edit.clear()
        self._kd_design_base_url_edit.clear()
        self._kd_design_cred_status.setText(f"บันทึก credential ของ '{mcp_id}' แล้ว")

    def _on_kd_design_test_clicked(self) -> None:
        if self._kd_design_thread is not None and self._kd_design_thread.isRunning():
            return
        self._kd_design_test_btn.setEnabled(False)
        self._kd_design_result.setPlainText("กำลังทดสอบ…")
        thread = _CallableThread(_run_design_tools_test, self)
        thread.resultReady.connect(self._on_kd_design_test_ready)
        self._kd_design_thread = thread
        thread.start()

    def _on_kd_design_test_ready(self, result: object) -> None:
        self._kd_design_test_btn.setEnabled(True)
        if isinstance(result, Exception):
            self._kd_design_result.setPlainText(f"ทดสอบไม่สำเร็จ: {result}")
            return
        self._kd_design_result.setPlainText("\n".join(result))

    def _on_kd_design_permissions_clicked(self) -> None:
        dlg = _RolePermissionsDialog(self, fonts=self._fonts)
        dlg.exec()

    # ──────────────────────────────────────────────────────────
    # panel: Context Strategy (v2-hardening C, `13_SIMPLE_UX.md`) — Fast/
    # Automatic/Deep switch, read/write through `core_v2_settings.
    # load_context_strategy`/`save_context_strategy`. `TAKKUB_CONTEXT_
    # STRATEGY` env still wins at build time (`core.brain.flag.
    # context_strategy`) — this panel just mirrors that precedence: when the
    # env var is set to a valid value the buttons show it and lock — same
    # "env wins, Settings UI just informs" precedent the old Core V2 pages'
    # `TAKKUB_V2_*` banners used before that section was removed (#515).
    # ──────────────────────────────────────────────────────────

    def _build_context_strategy_panel(self, parent: QWidget) -> QWidget:
        panel = QWidget(parent)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        env_value = os.environ.get("TAKKUB_CONTEXT_STRATEGY")
        env_active = env_value in {v for v, _label, _desc in _CONTEXT_STRATEGY_CHOICES}
        self._kd_ctx_strategy_banner: QLabel | None = None
        if env_active:
            banner = QLabel(
                f"TAKKUB_CONTEXT_STRATEGY={env_value} (env) — ตัวเลือกด้านล่างถูกปิดไว้ชั่วคราว",
                panel,
            )
            banner.setObjectName("infoBanner")
            lay.addWidget(banner)
            self._kd_ctx_strategy_banner = banner

        row = QHBoxLayout()
        row.addWidget(QLabel("Context Strategy:", panel))
        current = env_value if env_active else core_v2_settings.load_context_strategy()
        self._kd_ctx_strategy_group = QButtonGroup(panel)
        self._kd_ctx_strategy_group.setExclusive(True)
        self._kd_ctx_strategy_buttons: dict[str, QPushButton] = {}
        for value, label, _desc in _CONTEXT_STRATEGY_CHOICES:
            btn = QPushButton(label, panel)
            btn.setObjectName("secondaryButton")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setCheckable(True)
            btn.setChecked(value == current)
            btn.setEnabled(not env_active)
            btn.clicked.connect(lambda _checked, v=value: self._on_kd_ctx_strategy_clicked(v))
            self._kd_ctx_strategy_group.addButton(btn)
            self._kd_ctx_strategy_buttons[value] = btn
            row.addWidget(btn)
        row.addStretch(1)
        lay.addLayout(row)

        hint = QLabel(
            "  ·  ".join(f"{label} = {desc}" for _v, label, desc in _CONTEXT_STRATEGY_CHOICES),
            panel,
        )
        hint.setObjectName("panelHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        return panel

    def _on_kd_ctx_strategy_clicked(self, value: str) -> None:
        core_v2_settings.save_context_strategy(value)
        for v, btn in self._kd_ctx_strategy_buttons.items():
            btn.setChecked(v == value)

    # view: Context Debug — REMOVED in the #515 settings diet: it duplicated
    # `takkub doctor`'s own context-trace section (`core.context_sources.
    # doctor_section`, wired to the same `load_last_trace()` this tab used
    # to read directly) — debug output belongs in doctor, not a Settings
    # page. Its Context Strategy control (the one real, still-live pick on
    # that tab) moved onto the "Knowledge" tab instead — see
    # `_build_knowledge_view`/`_build_context_strategy_panel` above.

    # ──────────────────────────────────────────────────────────
    # module-level helpers
    # ──────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────
# module-level status helpers — run entirely off the Qt thread inside
# `_CallableThread`, so they must not touch any Qt object.
# ──────────────────────────────────────────────────────────────


def _collect_knowledge_status(project: str | None) -> dict[str, tuple[bool | None, str]]:
    from .core.brain.store import BrainStore
    from .doctor import Status as DoctorStatus
    from .doctor import check_graft, check_obsidian

    out: dict[str, tuple[bool | None, str]] = {}

    try:
        total = sum(1 for _ in BrainStore(project).load_active())
        out["Brain"] = (True, f"{total} record(s)")
    except Exception as e:
        out["Brain"] = (False, f"อ่านไม่สำเร็จ: {e}")

    try:
        findings = check_obsidian()
        worst_ok = all(f.status != DoctorStatus.FAIL for f in findings)
        out["Obsidian"] = (
            worst_ok,
            "; ".join(f"{f.name}={f.detail}" for f in findings[:3]) or "no data",
        )
    except Exception as e:
        out["Obsidian"] = (False, f"ตรวจสอบไม่สำเร็จ: {e}")

    try:
        findings = check_graft()
        cli_finding = next((f for f in findings if f.name == "cli"), None)
        if cli_finding is None:
            out["Graft"] = (None, "graft CLI ไม่พบใน PATH")
        else:
            size_finding = next((f for f in findings if f.name == "store-size"), None)
            detail = (
                f"{cli_finding.detail}  —  {size_finding.detail}"
                if size_finding is not None
                else cli_finding.detail
            )
            out["Graft"] = (cli_finding.status != DoctorStatus.FAIL, detail)
    except Exception as e:
        out["Graft"] = (False, f"ตรวจสอบไม่สำเร็จ: {e}")

    return out


def _collect_design_tools_status(project: str | None) -> dict[str, tuple[bool | None, str]]:
    from .core.capabilities.design_integrations import detect_storybook, integration_config_status
    from .lead_context import _allowed_project_roots

    out: dict[str, tuple[bool | None, str]] = {}

    try:
        roots = _allowed_project_roots(project) if project else []
        sb = detect_storybook(roots)
        out["Storybook"] = (
            sb.detected,
            f"{sb.root} (port {sb.port})"
            if sb.detected
            else "ไม่พบ .storybook/ หรือ storybook script",
        )
    except Exception as e:
        out["Storybook"] = (False, f"ตรวจสอบไม่สำเร็จ: {e}")

    for mcp_id, label in _DESIGN_MCPS:
        try:
            configured, msg = integration_config_status(mcp_id)
            out[label] = (configured, msg)
        except Exception as e:
            out[label] = (False, f"ตรวจสอบไม่สำเร็จ: {e}")

    return out


def _run_design_tools_test() -> list[str]:
    from .core.capabilities.design_integrations import integration_config_status

    lines: list[str] = []
    for mcp_id, label in _DESIGN_MCPS:
        configured, msg = integration_config_status(mcp_id)
        if not configured:
            lines.append(f"{label}: ○ not configured ({msg})")
            continue
        if mcp_id == "penpot":
            lines.append(f"{label}: {_test_penpot()}")
        else:
            lines.append(f"{label}: ● credential configured (ยังไม่มี generic connectivity probe)")
    return lines


def _test_penpot() -> str:
    """Penpot is the one design integration with a documented, parameterless
    connectivity probe (`PenpotClient.get_profile`) — see `design_clients.
    py`'s own docstring for why Figma/21st need a file/base_url this Settings
    view doesn't collect. Bypasses `design_integrations.build_client`'s
    per-role permission gate deliberately: a Settings-initiated credential
    test is an administrative action, not a pane acting as a role."""
    from .core.capabilities.design_clients import PenpotClient
    from .core.secrets.manager import SecretManager

    try:
        raw = SecretManager().get_secret("secret://penpot/default")
        cfg = json.loads(raw) if raw.strip().startswith("{") else {}
        base_url, token = cfg.get("base_url"), cfg.get("token")
        if not base_url or not token:
            return "✗ credential ไม่มี base_url/token ครบ"
        profile = PenpotClient(base_url=base_url, token=token).get_profile()
    except Exception as e:
        return f"✗ ทดสอบไม่สำเร็จ: {e}"
    if profile is None:
        return "✗ เชื่อมต่อไม่สำเร็จ (ดู log ระดับ WARNING)"
    return f"● connected — {profile.fullname} <{profile.email}>"
