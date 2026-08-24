"""Knowledge & Design Settings views (final closeout pack 2 — `docs/plans/
final-closeout-after-1.3.0/04_SETTINGS_UI_FINAL.md` + `05_UI_EXAMPLES.md`):
`KNOWLEDGE & DESIGN` sidebar section (Knowledge / OpenViking / Design Tools /
Context Debug).

A mixin (`KnowledgeDesignSettingsMixin`) mixed into `settings_window.
SettingsWindow`, same shape as `settings_core_v2.CoreV2SettingsMixin` (see
that module's own docstring) — kept in its own file rather than growing
`settings_window.py`/`settings_core_v2.py` further. **This module must never
import from `settings_window`** (that direction already goes the other way).

Every view here is read-mostly and, like Core V2, deliberately NOT wired
into `SettingsWindow`'s footer Save & Apply / dirty-tracking transaction —
OpenViking has its own dedicated "Save settings" button, Design Tools writes
each credential through immediately on its own "Save credential" button
(mirrors Core V2 Accounts & Pools' add/edit/remove-writes-immediately
precedent), and Knowledge/Context Debug are pure read-only status panels.

Every health/subprocess/network call (OpenViking `/health`, `graft
--version`, a design-tool connectivity probe) runs on a background `QThread`
— same "run() emits result-or-Exception, one `resultReady` signal" shape
`settings_core_v2.py` already established — never on the Qt main thread.
Nothing here fetches eagerly at view-construction time (same reasoning as
Core V2 Brain/Migration's own comments: every `SettingsWindow()` build
already constructs all views up front, so an eager fetch would cost a
network/subprocess round-trip on every Settings open even when this section
is never visited) — every panel starts as a "press Refresh/Test to load"
placeholder.

Secrets are never displayed once stored (`04_SETTINGS_UI_FINAL.md`: "Never
reveal saved secrets") — the credential field only ever accepts a NEW value
to write; `SecretManager.status()`/`get_secret()` is used to confirm
presence, never to populate the field.
"""

from __future__ import annotations

import json

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme, openviking_settings
from . import pane_tools_policy as pt_policy

_DESIGN_MCPS: tuple[tuple[str, str], ...] = (
    ("reference-21st", "21st.dev"),
    ("figma", "Figma"),
    ("penpot", "Penpot"),
)


# ──────────────────────────────────────────────────────────────
# generic worker thread — every call here is a callable with no args that
# either returns a plain value or raises; the signal always carries either
# the return value or the caught exception, never both.
# ──────────────────────────────────────────────────────────────


class _CallableThread(QThread):
    resultReady: pyqtSignal = pyqtSignal(object)

    def __init__(self, fn, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.resultReady.emit(self._fn())
        except Exception as e:  # pragma: no cover - fail-open, surfaced in the UI
            self.resultReady.emit(e)


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
    # view: Knowledge (status overview)
    # ──────────────────────────────────────────────────────────

    def _build_knowledge_view(self) -> QWidget:
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
        header_row.addWidget(self._build_card_header("KNOWLEDGE", "Sources", "", panel), 1)
        refresh_btn = cockpit_theme.secondary_button("Refresh", panel)
        refresh_btn.clicked.connect(self._on_kd_knowledge_refresh_clicked)
        header_row.addWidget(refresh_btn)
        p_lay.addLayout(header_row)

        self._kd_knowledge_rows: dict[str, tuple[QWidget, QLabel]] = {}
        for name in ("Brain", "Obsidian", "Graft", "OpenViking"):
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
    # view: OpenViking
    # ──────────────────────────────────────────────────────────

    def _build_openviking_view(self) -> QWidget:
        from .core.context_sources import openviking_adapter as adapter

        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(14)

        banner = QLabel(
            "ค่า ENV (TAKKUB_OPENVIKING_MODE ฯลฯ) ชนะค่าที่บันทึกไว้ที่นี่เสมอ — "
            f"mode ที่ใช้จริงตอนนี้ (effective): {adapter.mode()}"
            + ("" if adapter.enabled() else "  (TAKKUB_OPENVIKING_ENABLED=0 — sidecar ปิดอยู่)"),
            view,
        )
        banner.setObjectName("infoBanner")
        banner.setWordWrap(True)
        lay.addWidget(banner)

        status_panel = QWidget(view)
        status_panel.setObjectName("panel")
        sp_lay = QVBoxLayout(status_panel)
        sp_lay.setContentsMargins(14, 12, 14, 12)
        sp_lay.setSpacing(8)
        sp_lay.addWidget(self._build_card_header("OPENVIKING", "Status", "", status_panel))
        status_row = QHBoxLayout()
        self._kd_ov_status_dot = cockpit_theme.color_dot(
            cockpit_theme.TEXT_FAINT, status_panel, size=8
        )
        status_row.addWidget(self._kd_ov_status_dot)
        self._kd_ov_status_lbl = QLabel("กด “Test” เพื่อตรวจสอบ health", status_panel)
        status_row.addWidget(self._kd_ov_status_lbl, 1)
        sp_lay.addLayout(status_row)
        lay.addWidget(status_panel)

        cfg = openviking_settings.load()
        form_panel = QWidget(view)
        form_panel.setObjectName("panel")
        fp_lay = QVBoxLayout(form_panel)
        fp_lay.setContentsMargins(14, 12, 14, 12)
        fp_lay.setSpacing(8)
        fp_lay.addWidget(self._build_card_header("OPENVIKING", "Config", "", form_panel))

        form = QFormLayout()
        self._kd_ov_mode_combo = QComboBox(form_panel)
        for m in adapter.MODES:
            self._kd_ov_mode_combo.addItem(m)
        idx = self._kd_ov_mode_combo.findText(cfg.mode)
        self._kd_ov_mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("Mode", self._kd_ov_mode_combo)

        self._kd_ov_strict_check = QCheckBox(form_panel)
        self._kd_ov_strict_check.setChecked(cfg.strict_project)
        form.addRow("Strict Project", self._kd_ov_strict_check)

        self._kd_ov_include_global_check = QCheckBox(form_panel)
        self._kd_ov_include_global_check.setChecked(cfg.include_global)
        form.addRow("Include Global", self._kd_ov_include_global_check)

        self._kd_ov_limit_spin = QSpinBox(form_panel)
        self._kd_ov_limit_spin.setRange(1, 100)
        self._kd_ov_limit_spin.setValue(cfg.result_limit)
        form.addRow("Result Limit", self._kd_ov_limit_spin)

        self._kd_ov_timeout_spin = QDoubleSpinBox(form_panel)
        self._kd_ov_timeout_spin.setRange(0.5, 30.0)
        self._kd_ov_timeout_spin.setSingleStep(0.5)
        self._kd_ov_timeout_spin.setSuffix("s")
        self._kd_ov_timeout_spin.setValue(cfg.timeout)
        form.addRow("Timeout", self._kd_ov_timeout_spin)
        fp_lay.addLayout(form)

        save_row = QHBoxLayout()
        save_btn = cockpit_theme.gold_button("Save settings", form_panel)
        save_btn.clicked.connect(self._on_kd_ov_save_clicked)
        save_row.addWidget(save_btn)
        self._kd_ov_save_status = QLabel("", form_panel)
        self._kd_ov_save_status.setObjectName("panelHint")
        save_row.addWidget(self._kd_ov_save_status)
        save_row.addStretch(1)
        fp_lay.addLayout(save_row)
        lay.addWidget(form_panel)

        action_row = QHBoxLayout()
        self._kd_ov_test_btn = cockpit_theme.secondary_button("Test", view)
        self._kd_ov_test_btn.clicked.connect(self._on_kd_ov_test_clicked)
        action_row.addWidget(self._kd_ov_test_btn)
        self._kd_ov_sync_btn = cockpit_theme.secondary_button("Sync Active Project", view)
        self._kd_ov_sync_btn.clicked.connect(self._on_kd_ov_sync_clicked)
        action_row.addWidget(self._kd_ov_sync_btn)
        self._kd_ov_reindex_btn = cockpit_theme.secondary_button("Re-index", view)
        self._kd_ov_reindex_btn.clicked.connect(self._on_kd_ov_reindex_clicked)
        action_row.addWidget(self._kd_ov_reindex_btn)
        action_row.addStretch(1)
        lay.addLayout(action_row)

        self._kd_ov_result = QPlainTextEdit(view)
        self._kd_ov_result.setReadOnly(True)
        self._kd_ov_result.setStyleSheet(f'font-family: "{self._fonts["mono"]}"; font-size: 12px;')
        self._kd_ov_result.setFixedHeight(120)
        lay.addWidget(self._kd_ov_result)
        lay.addStretch(1)

        self._kd_ov_thread: _CallableThread | None = None
        return view

    def _on_kd_ov_save_clicked(self) -> None:
        cfg = openviking_settings.OpenVikingUiConfig(
            mode=self._kd_ov_mode_combo.currentText(),
            strict_project=self._kd_ov_strict_check.isChecked(),
            include_global=self._kd_ov_include_global_check.isChecked(),
            result_limit=self._kd_ov_limit_spin.value(),
            timeout=self._kd_ov_timeout_spin.value(),
        )
        if openviking_settings.save(cfg):
            self._kd_ov_save_status.setText("บันทึกแล้ว — env var (ถ้าตั้งไว้) ยังชนะค่านี้เสมอ")
        else:
            self._kd_ov_save_status.setText("บันทึกไม่สำเร็จ")

    def _kd_ov_set_buttons_enabled(self, enabled: bool) -> None:
        self._kd_ov_test_btn.setEnabled(enabled)
        self._kd_ov_sync_btn.setEnabled(enabled)
        self._kd_ov_reindex_btn.setEnabled(enabled)

    def _on_kd_ov_test_clicked(self) -> None:
        self._kd_ov_set_buttons_enabled(False)
        self._kd_ov_result.setPlainText("กำลังตรวจสอบ…")
        timeout = self._kd_ov_timeout_spin.value()

        def _do():
            from .core.context_sources import openviking_adapter as adapter

            return adapter.health(timeout=timeout)

        thread = _CallableThread(_do, self)
        thread.resultReady.connect(self._on_kd_ov_test_ready)
        self._kd_ov_thread = thread
        thread.start()

    def _on_kd_ov_test_ready(self, result: object) -> None:
        self._kd_ov_set_buttons_enabled(True)
        from .core.context_sources.openviking_adapter import HealthStatus

        if isinstance(result, Exception) or not isinstance(result, HealthStatus):
            self._kd_ov_status_dot.setStyleSheet(
                f"background: {cockpit_theme.STATE_ERROR}; border-radius: 4px;"
            )
            self._kd_ov_status_lbl.setText(f"ตรวจสอบไม่สำเร็จ: {result}")
            self._kd_ov_result.setPlainText(str(result))
            return
        ok = result.ok and result.healthy
        self._kd_ov_status_dot.setStyleSheet(
            f"background: {_status_dot_color(ok)}; border-radius: 4px;"
        )
        self._kd_ov_status_lbl.setText(
            f"{'Connected' if result.ok else 'Unreachable'}"
            f"  healthy={result.healthy}  version={result.version or '?'}"
            f"  known_version={result.known_version}"
        )
        self._kd_ov_result.setPlainText(
            f"ok={result.ok} healthy={result.healthy} version={result.version} "
            f"known_version={result.known_version} error={result.error or '-'}"
        )

    def _kd_ov_run_index(self, *, force: bool, label: str) -> None:
        self._kd_ov_set_buttons_enabled(False)
        self._kd_ov_result.setPlainText(f"{label}…")
        project = self._project

        def _do():
            from .core.context_sources import indexing

            if force:
                indexing.reset_state(project)
            return indexing.index_vault(project)

        thread = _CallableThread(_do, self)
        thread.resultReady.connect(self._on_kd_ov_index_ready)
        self._kd_ov_thread = thread
        thread.start()

    def _on_kd_ov_sync_clicked(self) -> None:
        self._kd_ov_run_index(force=False, label="กำลัง sync project ปัจจุบัน")

    def _on_kd_ov_reindex_clicked(self) -> None:
        self._kd_ov_run_index(force=True, label="กำลัง re-index ทั้งหมด")

    def _on_kd_ov_index_ready(self, result: object) -> None:
        self._kd_ov_set_buttons_enabled(True)
        if isinstance(result, Exception):
            self._kd_ov_result.setPlainText(f"ไม่สำเร็จ: {result}")
            return
        if not result.ok:
            self._kd_ov_result.setPlainText(f"ไม่สำเร็จ: {result.reason}")
            return
        self._kd_ov_result.setPlainText(
            f"added={result.added} skipped={result.skipped} failed={result.failed} "
            f"total_indexed={result.total}"
        )

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
    # view: Context Debug
    # ──────────────────────────────────────────────────────────

    def _build_context_debug_view(self) -> QWidget:
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
        header_row.addWidget(self._build_card_header("CONTEXT", "Last build trace", "", panel), 1)
        refresh_btn = cockpit_theme.secondary_button("Refresh", panel)
        refresh_btn.clicked.connect(self._reload_kd_context_debug)
        header_row.addWidget(refresh_btn)
        p_lay.addLayout(header_row)

        self._kd_ctx_header_lbl = QLabel("", panel)
        self._kd_ctx_header_lbl.setStyleSheet(f'font-family: "{self._fonts["mono"]}";')
        p_lay.addWidget(self._kd_ctx_header_lbl)

        self._kd_ctx_grid_host = QWidget(panel)
        self._kd_ctx_grid = QGridLayout(self._kd_ctx_grid_host)
        self._kd_ctx_grid.setContentsMargins(0, 8, 0, 8)
        self._kd_ctx_grid.setHorizontalSpacing(18)
        self._kd_ctx_grid.setVerticalSpacing(4)
        p_lay.addWidget(self._kd_ctx_grid_host)

        self._kd_ctx_totals_lbl = QLabel("", panel)
        self._kd_ctx_totals_lbl.setObjectName("panelHint")
        self._kd_ctx_totals_lbl.setWordWrap(True)
        p_lay.addWidget(self._kd_ctx_totals_lbl)
        lay.addWidget(panel)

        action_row = QHBoxLayout()
        self._kd_ctx_view_btn = cockpit_theme.secondary_button("View Context", view)
        self._kd_ctx_view_btn.clicked.connect(self._on_kd_ctx_view_context_clicked)
        action_row.addWidget(self._kd_ctx_view_btn)
        self._kd_ctx_trace_btn = cockpit_theme.secondary_button("Retrieval Trace", view)
        self._kd_ctx_trace_btn.clicked.connect(self._on_kd_ctx_retrieval_trace_clicked)
        action_row.addWidget(self._kd_ctx_trace_btn)
        self._kd_ctx_copy_btn = cockpit_theme.secondary_button("Copy Report", view)
        self._kd_ctx_copy_btn.clicked.connect(self._on_kd_ctx_copy_report_clicked)
        action_row.addWidget(self._kd_ctx_copy_btn)
        action_row.addStretch(1)
        lay.addLayout(action_row)
        lay.addStretch(1)

        self._kd_ctx_trace: dict | None = None
        self._reload_kd_context_debug()
        return view

    def _reload_kd_context_debug(self) -> None:
        from .core.context_sources.trace_store import load_last_trace

        trace = load_last_trace()
        self._kd_ctx_trace = trace

        while self._kd_ctx_grid.count():
            item = self._kd_ctx_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        has_trace = trace is not None
        for btn in (self._kd_ctx_view_btn, self._kd_ctx_trace_btn, self._kd_ctx_copy_btn):
            btn.setEnabled(has_trace)

        if not has_trace:
            self._kd_ctx_header_lbl.setText(f"Project: {self._project or '(no project)'}")
            self._kd_ctx_totals_lbl.setText(
                "ยังไม่มี context build record — ยังไม่เคยรัน assign ที่เปิด OpenViking"
            )
            return

        self._kd_ctx_header_lbl.setText(
            f"Project: {trace.get('project') or self._project or '(no project)'}"
            f"   Role: {trace.get('role') or '—'}   Mode: {trace.get('mode') or '—'}"
        )

        headers = ("SOURCE", "ITEMS", "TOKENS", "TIME")
        for col, h in enumerate(headers):
            lbl = QLabel(h, self._kd_ctx_grid_host)
            lbl.setStyleSheet(
                f'font-family: "{self._fonts["mono"]}"; font-size: 10px; font-weight: 600; '
                f"letter-spacing: 1px; color: {cockpit_theme.TEXT_FAINT};"
            )
            self._kd_ctx_grid.addWidget(lbl, 0, col)
        for row, source in enumerate(trace.get("sources", []), start=1):
            # `time_ms` doesn't exist on ContextTrace/SourceTrace yet
            # (`08_OBSERVABILITY_FINAL.md` "latency per source" is a future
            # backend addition) — read optionally so this table degrades to
            # "—" instead of crashing once that field exists but isn't
            # populated for an older trace file, or vice versa.
            time_val = source.get("time_ms")
            cells = (
                str(source.get("name", "?")),
                str(source.get("count", 0)),
                str(source.get("tokens", 0)),
                f"{time_val:.0f}ms" if isinstance(time_val, (int, float)) else "—",
            )
            for col, text in enumerate(cells):
                self._kd_ctx_grid.addWidget(QLabel(text, self._kd_ctx_grid_host), row, col)

        scope_rejected = trace.get("scope_rejects", "—")
        trust_rejected = trace.get("trust_rejects", "—")
        task_size = trace.get("task_size", "—")
        self._kd_ctx_totals_lbl.setText(
            f"Total: {trace.get('total_tokens', 0)} / {trace.get('budget_tokens', 0)}"
            f"   Dedup: {trace.get('dedup_count', 0)}"
            f"   Scope rejected: {scope_rejected}"
            f"   Trust rejected: {trust_rejected}"
            f"   Task size: {task_size}"
            f"   Latency: {trace.get('latency_ms', 0):.0f}ms"
        )

    def _kd_ctx_report_text(self) -> str:
        trace = self._kd_ctx_trace
        if not trace:
            return "(no context build recorded yet)"
        lines = [
            f"Project: {trace.get('project') or self._project or '(no project)'}",
            f"Role: {trace.get('role') or '—'}",
            f"Mode: {trace.get('mode') or '—'}",
            "",
            f"{'SOURCE':<16}{'ITEMS':>8}{'TOKENS':>8}{'TIME':>8}",
        ]
        for source in trace.get("sources", []):
            time_val = source.get("time_ms")
            time_text = f"{time_val:.0f}ms" if isinstance(time_val, (int, float)) else "—"
            lines.append(
                f"{source.get('name', '?')!s:<16}{source.get('count', 0):>8}"
                f"{source.get('tokens', 0):>8}{time_text:>8}"
            )
        lines += [
            "",
            f"Total: {trace.get('total_tokens', 0)} / {trace.get('budget_tokens', 0)}",
            f"Dedup: {trace.get('dedup_count', 0)}",
            f"Scope rejected: {trace.get('scope_rejects', '—')}",
            f"Trust rejected: {trace.get('trust_rejects', '—')}",
            f"Task size: {trace.get('task_size', '—')}",
            f"Latency: {trace.get('latency_ms', 0):.0f}ms",
        ]
        return "\n".join(lines)

    def _kd_ctx_show_text_dialog(self, title: str, text: str) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setStyleSheet(self.styleSheet())
        dlg.resize(560, 420)
        lay = QVBoxLayout(dlg)
        body = QPlainTextEdit(dlg)
        body.setReadOnly(True)
        body.setPlainText(text)
        body.setStyleSheet(f'font-family: "{self._fonts["mono"]}"; font-size: 12px;')
        lay.addWidget(body)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        lay.addWidget(buttons)
        dlg.exec()

    def _on_kd_ctx_view_context_clicked(self) -> None:
        self._kd_ctx_show_text_dialog("Context Debug — View Context", self._kd_ctx_report_text())

    def _on_kd_ctx_retrieval_trace_clicked(self) -> None:
        trace = self._kd_ctx_trace or {}
        self._kd_ctx_show_text_dialog(
            "Context Debug — Retrieval Trace", json.dumps(trace, indent=2, ensure_ascii=False)
        )

    def _on_kd_ctx_copy_report_clicked(self) -> None:
        QGuiApplication.clipboard().setText(self._kd_ctx_report_text())


# ──────────────────────────────────────────────────────────────
# module-level helpers — run entirely off the Qt thread inside
# `_CallableThread`, so they must not touch any Qt object.
# ──────────────────────────────────────────────────────────────


def _collect_knowledge_status(project: str | None) -> dict[str, tuple[bool | None, str]]:
    from .core.brain.store import BrainStore
    from .core.context_sources import openviking_adapter as ov_adapter
    from .core.context_sources.indexing import index_status
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

    try:
        if not ov_adapter.enabled():
            out["OpenViking"] = (None, "disabled (TAKKUB_OPENVIKING_ENABLED=0)")
        else:
            status = index_status(project)
            out["OpenViking"] = (
                bool(status["healthy"]),
                f"mode={status['mode']} healthy={status['healthy']} "
                f"version={status['version'] or '?'} indexed={status['indexed_count']}",
            )
    except Exception as e:
        out["OpenViking"] = (False, f"ตรวจสอบไม่สำเร็จ: {e}")

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
