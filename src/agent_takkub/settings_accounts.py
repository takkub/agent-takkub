"""Settings — the unified **Accounts** page (#505 stage 1).

One page for "ใคร login อยู่ / ใช้บัญชีไหนกับโปรเจคไหน" across every provider,
replacing both `ACCOUNT → Users` (Profiles tab) and `ADVANCED → Accounts &
Pools`. Providers render as big rows, accounts as cards inside — readable in
5 seconds, no `CLAUDE_CONFIG_DIR` on the main surface (paths live in
tooltips/advanced only). All data flows through `accounts_adapter` (the one
storage layer #504 will later swap underneath).

`AccountsSettingsMixin` is mixed into `settings_window.SettingsWindow` — the
same UI-layer-mixin shape as `CoreV2SettingsMixin`, and the same rule: this
module must never import `settings_window` back (cycle).

Writes are immediate (add/remove), never routed through the footer
Save & Apply transaction — mirroring the Core V2 pages' precedent.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import accounts_adapter, cockpit_theme


class _AccountsRefreshSignals(QObject):
    finished = pyqtSignal(object)  # list[accounts_adapter.ProviderRow]


class _AccountsRefreshWorker(QRunnable):
    """#505 review finding M4: `accounts_adapter.provider_rows()` can shell
    out — the default claude account's macOS Keychain probe
    (`limit_status._read_keychain_credentials`, `security ... -w`) carries
    a 5s subprocess timeout. The status-bar plan badge already moved this
    exact class of read off the Qt main thread (`status_header.
    _PlanProbeWorker`); the Accounts page never had until now, even though
    it reads every provider's every account on every page build."""

    def __init__(self) -> None:
        super().__init__()
        self.signals = _AccountsRefreshSignals()

    def run(self) -> None:
        try:
            rows = accounts_adapter.provider_rows()
        except Exception:
            rows = []
        try:
            self.signals.finished.emit(rows)
        except RuntimeError:
            pass  # window deleted while the pool job was running


def _provider_color(provider: str) -> str:
    if provider == "claude":
        return cockpit_theme.PROVIDER_CLAUDE
    return cockpit_theme.ROLE_COLORS.get(provider, cockpit_theme.ROLE_COLOR_FALLBACK)


def _login_dot_and_text(login: accounts_adapter.LoginStatus) -> tuple[str, str]:
    """(dot color, human sentence) for one account's login state."""
    if login.state == accounts_adapter.LOGGED_IN:
        text = "เข้าสู่ระบบแล้ว"
        if login.detail:
            text += f" ({login.detail})"
        return cockpit_theme.STATE_OK, text
    if login.state == accounts_adapter.LOGGED_OUT:
        return cockpit_theme.TEXT_FAINT, login.detail or "ยังไม่ได้เข้าสู่ระบบ"
    detail = f" — {login.detail}" if login.detail else ""
    return cockpit_theme.STATE_WARN, f"ไม่ทราบสถานะ{detail}"


class _AddAccountDialog(QDialog):
    """เพิ่มบัญชี = พิมพ์ชื่ออย่างเดียว. Advanced options (ใช้โฟลเดอร์ที่มีอยู่ /
    แชร์ session กับ default) fold away under one toggle."""

    def __init__(self, parent: QWidget | None, provider: str) -> None:
        super().__init__(parent)
        self._provider = provider
        self.setWindowTitle(f"เพิ่มบัญชี {provider}")
        self.setMinimumWidth(460)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        form = QFormLayout()
        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("เช่น work, office, personal")
        form.addRow("ชื่อบัญชี:", self.name_edit)
        lay.addLayout(form)

        self._home_preview = QLabel("", self)
        self._home_preview.setObjectName("panelHint")
        self._home_preview.setWordWrap(True)
        lay.addWidget(self._home_preview)
        self.name_edit.textChanged.connect(self._refresh_home_preview)

        # Plain text-link toggle (no glyph — IBM Plex has no ▸/▾ code points,
        # they tofu; see settings_window's nav-icon note) and deliberately NOT
        # button-styled so the one gold CTA below stays the obvious action.
        self._adv_toggle = QPushButton("แสดงตัวเลือกขั้นสูง…", self)
        self._adv_toggle.setCheckable(True)
        self._adv_toggle.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; text-align: left; "
            f"padding: 2px 0; color: {cockpit_theme.TEXT_MUTED}; }}"
            f"QPushButton:hover {{ color: {cockpit_theme.TEXT_SECONDARY}; }}"
        )
        self._adv_toggle.toggled.connect(self._on_adv_toggled)
        lay.addWidget(self._adv_toggle)

        self._adv_body = QWidget(self)
        adv_lay = QVBoxLayout(self._adv_body)
        adv_lay.setContentsMargins(0, 0, 0, 0)
        adv_lay.setSpacing(8)
        adv_form = QFormLayout()
        dir_row = QWidget(self._adv_body)
        dir_row_lay = QHBoxLayout(dir_row)
        dir_row_lay.setContentsMargins(0, 0, 0, 0)
        self.dir_edit = QLineEdit(dir_row)
        self.dir_edit.setPlaceholderText("ว่างไว้ = สร้างโฟลเดอร์ใหม่ให้อัตโนมัติ")
        self.dir_edit.setToolTip(
            "ใช้โฟลเดอร์ config ที่มีอยู่แล้ว (เช่น ~/.claude-work ที่เคย login ไว้)\n"
            "ค่านี้จะกลายเป็น CLAUDE_CONFIG_DIR ของ pane ที่ใช้บัญชีนี้"
        )
        dir_row_lay.addWidget(self.dir_edit, 1)
        browse_btn = cockpit_theme.secondary_button("เลือก…", dir_row)
        browse_btn.setFixedWidth(72)
        browse_btn.clicked.connect(self._on_browse)
        dir_row_lay.addWidget(browse_btn)
        adv_form.addRow("ใช้โฟลเดอร์ที่มีอยู่:", dir_row)
        adv_lay.addLayout(adv_form)

        self.share_chk = QCheckBox(
            "แชร์ session/plugins กับบัญชี default (สลับเฉพาะการ login)", self._adv_body
        )
        self.share_chk.setChecked(True)
        self.share_chk.setToolTip(
            "แนะนำ: บัญชีใหม่ใช้ประวัติงาน/plugins ร่วมกับ default —\n"
            "สลับบัญชีแล้วทุกอย่างอยู่ครบ เปลี่ยนแค่การเข้าสู่ระบบ\n"
            "เอาติ๊กออก = แยกทุกอย่างเป็นอิสระ (แบบเดิม)"
        )
        adv_lay.addWidget(self.share_chk)
        self._adv_body.hide()
        lay.addWidget(self._adv_body)

        buttons = QDialogButtonBox(self)
        cancel_btn = buttons.addButton("ยกเลิก", QDialogButtonBox.ButtonRole.RejectRole)
        create_btn = cockpit_theme.gold_button("สร้างบัญชี", self)
        buttons.addButton(create_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        cancel_btn.setObjectName("secondaryButton")
        lay.addWidget(buttons)

        self._refresh_home_preview()

    def _on_adv_toggled(self, checked: bool) -> None:
        self._adv_toggle.setText("ซ่อนตัวเลือกขั้นสูง" if checked else "แสดงตัวเลือกขั้นสูง…")
        self._adv_body.setVisible(checked)

    def _on_browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "เลือกโฟลเดอร์ config ของบัญชี")
        if d:
            self.dir_edit.setText(d)

    def _refresh_home_preview(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self._home_preview.setText("บ้านของบัญชีจะถูกสร้างให้อัตโนมัติเมื่อตั้งชื่อ")
            return
        try:
            home = accounts_adapter.default_account_home(self._provider, name)
            self._home_preview.setText(f"บ้านของบัญชี: {home}")
        except ValueError:
            self._home_preview.setText("")

    def values(self) -> tuple[str, str, bool]:
        return (
            self.name_edit.text().strip(),
            self.dir_edit.text().strip(),
            self.share_chk.isChecked(),
        )


class _LoginPaneDialog(QDialog):
    """A real provider pane (TerminalWidget + PtySession) scoped to ONE
    account's home, so `claude` / `codex login` writes the credential exactly
    where that account's panes will read it. Non-modal child of the Settings
    dialog. The PTY is torn down on close — never left running."""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        hint: str,
        argv: list[str],
        extra_env: dict[str, str],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        if hint:
            hint_lbl = QLabel(hint, self)
            hint_lbl.setObjectName("panelHint")
            hint_lbl.setWordWrap(True)
            lay.addWidget(hint_lbl)

        # Heavy imports (QWebEngine) stay lazy — only paid when a login pane
        # actually opens, never on Settings construction.
        from .pty_session import PtySession
        from .terminal_widget import TerminalWidget

        self._term = TerminalWidget(self)
        self._session = PtySession(parent=self)
        self._session.bytesIn.connect(self._term.write_bytes)
        self._term.inputBytes.connect(self._session.write)
        self._term.resized.connect(self._session.resize)
        self._session.processExited.connect(self._on_exited)
        lay.addWidget(self._term, 1)
        self._session.spawn(argv, cwd=str(Path.home()), env={**os.environ, **extra_env})

    def _on_exited(self, _code: int) -> None:
        # The CLI ended (user typed exit / login flow closed it) — nothing
        # left to interact with, close the window.
        self.close()

    def closeEvent(self, event) -> None:
        try:
            self._session.terminate()
        except Exception:
            pass
        super().closeEvent(event)


class AccountsSettingsMixin:
    """Mixed into `SettingsWindow` — assumes its attributes (`_fonts`,
    `_up_profiles`, `_users_status`, `_reload_users_auth_combo`) exist."""

    # ──────────────────────────────────────────────────────────
    # view: Accounts (provider rows + account cards)
    # ──────────────────────────────────────────────────────────

    def _build_accounts_tab(self, parent: QWidget) -> QWidget:
        tab = QWidget(parent)
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        intro = QLabel(
            "บัญชีของแต่ละ provider — ใครเข้าสู่ระบบอยู่ และโปรเจคไหนใช้บัญชีไหน "
            "· เพิ่มบัญชีใหม่แค่ตั้งชื่อ แล้วกด “เข้าสู่ระบบ” บนการ์ด",
            tab,
        )
        intro.setObjectName("panelHint")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self._accounts_rows_box = QVBoxLayout()
        self._accounts_rows_box.setSpacing(10)
        lay.addLayout(self._accounts_rows_box)
        lay.addStretch(1)

        self._accounts_login_dialogs: list[_LoginPaneDialog] = []
        self._accounts_rows_cache: list[accounts_adapter.ProviderRow] | None = None
        self._accounts_refresh_busy = False
        self._accounts_refresh()
        return tab

    def _accounts_refresh(self) -> None:
        """Re-read everything through the adapter and rebuild the rows.

        #505 review M4: `provider_rows()` must never run on the Qt main
        thread (see `_AccountsRefreshWorker`'s own docstring). Renders
        whatever's cached from the last refresh (or a bare loading row on
        the very first build) immediately, then kicks a background job and
        rebuilds for real when it lands — never blocks page construction or
        any other call site (add/remove/login-close) waiting on a probe.
        """
        self._render_accounts_rows(self._accounts_rows_cache)
        if self._accounts_refresh_busy:
            return
        self._accounts_refresh_busy = True
        worker = _AccountsRefreshWorker()
        worker.signals.finished.connect(self._on_accounts_refreshed)
        QThreadPool.globalInstance().start(worker)

    def _on_accounts_refreshed(self, rows: list) -> None:
        self._accounts_refresh_busy = False
        try:
            self._accounts_rows_cache = rows
            self._render_accounts_rows(rows)
        except RuntimeError:
            pass  # this Settings window was closed/deleted while the job ran

    def _render_accounts_rows(self, rows: list[accounts_adapter.ProviderRow] | None) -> None:
        while self._accounts_rows_box.count():
            item = self._accounts_rows_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if rows is None:
            loading = QLabel("กำลังโหลดบัญชี…", self)
            loading.setObjectName("panelHint")
            self._accounts_rows_box.addWidget(loading)
            return
        for row in rows:
            self._accounts_rows_box.addWidget(self._build_provider_panel(row))

    def _build_provider_panel(self, row: accounts_adapter.ProviderRow) -> QWidget:
        panel = QWidget(self)
        panel.setObjectName("panel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        dot_color = (
            _provider_color(row.provider) if not row.gap_reason else cockpit_theme.TEXT_FAINT
        )
        header.addWidget(cockpit_theme.color_dot(dot_color, panel, size=10))
        name_lbl = QLabel(row.display_name, panel)
        title_color = cockpit_theme.TEXT_MUTED if row.gap_reason else cockpit_theme.TEXT_PRIMARY_ALT
        name_lbl.setStyleSheet(
            f'font-family: "{self._fonts["mono"]}"; font-size: 15px; font-weight: 700; '
            f"color: {title_color};"
        )
        header.addWidget(name_lbl)
        count_chip = cockpit_theme.gold_soft_chip(f"{len(row.accounts)} บัญชี", panel, compact=True)
        header.addWidget(count_chip)
        header.addStretch(1)
        if not row.gap_reason:
            add_btn = cockpit_theme.secondary_button("+ เพิ่มบัญชี", panel)
            add_btn.setEnabled(row.can_add)
            if not row.can_add and row.add_hint:
                add_btn.setToolTip(row.add_hint)
            add_btn.clicked.connect(
                lambda _=False, p=row.provider: self._on_accounts_add_clicked(p)
            )
            header.addWidget(add_btn)
        lay.addLayout(header)

        if row.gap_reason:
            # #505 review M7: a gap only ever blocks ADD/LOGIN for a NEW
            # account (`login_launch` already returns None for every
            # provider but claude/codex, so an existing account's own
            # "เข้าสู่ระบบ" button never renders below either) — an existing
            # account must still be listed, never hidden by this notice.
            gap_lbl = QLabel("ยังแยกบัญชีใหม่ไม่ได้ — ใช้บัญชีของเครื่องทั้งเครื่อง", panel)
            gap_lbl.setStyleSheet(f"color: {cockpit_theme.TEXT_MUTED};")
            gap_lbl.setToolTip(row.gap_reason)
            lay.addWidget(gap_lbl)
            why_lbl = QLabel(row.gap_reason, panel)
            why_lbl.setObjectName("panelHint")
            why_lbl.setWordWrap(True)
            lay.addWidget(why_lbl)

        for account in row.accounts:
            lay.addWidget(self._build_account_card(account, panel))
        return panel

    def _build_account_card(
        self, account: accounts_adapter.AccountInfo, parent: QWidget
    ) -> QWidget:
        card = QWidget(parent)
        card.setObjectName("panelAlt")  # themed by build_stylesheet — no inline QSS
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)

        if account.config_dir:
            card.setToolTip(f"โฟลเดอร์ของบัญชีนี้: {account.config_dir}")

        top = QHBoxLayout()
        top.setSpacing(8)
        name_lbl = QLabel(account.name, card)
        name_lbl.setStyleSheet(
            f"font-weight: 700; color: {cockpit_theme.TEXT_PRIMARY}; border: none;"
        )
        top.addWidget(name_lbl)
        if account.is_default:
            top.addWidget(cockpit_theme.gold_soft_chip("บัญชีหลัก", card, compact=True))
        if account.login.plan:
            plan_chip = QLabel(account.login.plan, card)
            plan_chip.setStyleSheet(
                f'font-family: "{self._fonts["mono"]}"; font-size: 11px; '
                f"color: {cockpit_theme.NEUTRAL_CHIP_TEXT}; "
                f"background: {cockpit_theme.NEUTRAL_CHIP_BG}; "
                f"border: 1px solid {cockpit_theme.NEUTRAL_CHIP_BORDER}; "
                "border-radius: 999px; padding: 1px 8px;"
            )
            plan_chip.setToolTip(
                account.login.plan_note or "แผนการใช้งานของบัญชีนี้ (อ่านจาก credential ของ provider)"
            )
            top.addWidget(plan_chip)
        if account.origin == "v2":
            top.addWidget(cockpit_theme.gold_soft_chip("V2 registry", card, compact=True))
        top.addStretch(1)
        launch = accounts_adapter.login_launch(account.provider, account.config_dir)
        if launch is not None and account.origin == "profile":
            login_btn = cockpit_theme.secondary_button("เข้าสู่ระบบ", card)
            login_btn.setToolTip(f"เปิดหน้าต่าง {account.provider} ของบัญชีนี้เพื่อ login ตามปกติ")
            login_btn.clicked.connect(lambda _=False, a=account: self._on_accounts_login_clicked(a))
            top.addWidget(login_btn)
        if not account.is_default and account.origin == "profile":
            if account.provider == "claude":
                share_btn = cockpit_theme.secondary_button("แชร์ session", card)
                share_btn.setToolTip(
                    "แปลงบัญชีนี้เป็นโหมดแชร์ session/plugins กับ default —\n"
                    "ประวัติงานเดิมถูกรวมเข้า default (ไม่มีอะไรถูกเขียนทับ,\n"
                    "ของเดิมเก็บเป็น *.pre-share-backup) แล้วลิงก์ให้ใช้ร่วมกัน\n"
                    "ต่อจากนี้สลับบัญชี = เปลี่ยนแค่การเข้าสู่ระบบ"
                )
                share_btn.clicked.connect(
                    lambda _=False, a=account: self._on_accounts_share_clicked(a)
                )
                top.addWidget(share_btn)
            remove_btn = cockpit_theme.secondary_button("ลบ", card)
            remove_btn.clicked.connect(
                lambda _=False, a=account: self._on_accounts_remove_clicked(a)
            )
            top.addWidget(remove_btn)
        lay.addLayout(top)

        dot_color, status_text = _login_dot_and_text(account.login)
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        status_row.addWidget(cockpit_theme.color_dot(dot_color, card, size=8))
        status_lbl = QLabel(status_text, card)
        status_lbl.setStyleSheet(f"color: {cockpit_theme.TEXT_SECONDARY}; border: none;")
        status_lbl.setWordWrap(True)
        status_row.addWidget(status_lbl, 1)
        lay.addLayout(status_row)

        usage_lbl = QLabel(self._accounts_usage_text(account), card)
        usage_lbl.setStyleSheet(
            f"color: {cockpit_theme.TEXT_MUTED}; font-size: 12px; border: none;"
        )
        usage_lbl.setWordWrap(True)
        lay.addWidget(usage_lbl)
        return card

    @staticmethod
    def _accounts_usage_text(account: accounts_adapter.AccountInfo) -> str:
        picked = ", ".join(account.projects)
        if account.is_default:
            base = "ใช้กับทุกโปรเจคที่ไม่ได้เลือกบัญชีเอง"
            return f"{base} · เลือกตรง: {picked}" if picked else base
        return f"ใช้กับ: {picked}" if picked else "ยังไม่มีโปรเจคไหนเลือกบัญชีนี้"

    # ──────────────────────────────────────────────────────────
    # handlers
    # ──────────────────────────────────────────────────────────

    def _on_accounts_add_clicked(self, provider: str) -> None:
        dlg = _AddAccountDialog(self, provider)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, config_dir, share = dlg.values()
        if not name:
            return
        try:
            home, linked = accounts_adapter.add_account(
                provider, name, config_dir, share_sessions=share
            )
        except ValueError as exc:
            QMessageBox.warning(self, "เพิ่มบัญชีไม่สำเร็จ", str(exc))
            return
        note = f"บัญชี '{name}' สร้างแล้ว ({home})"
        if linked:
            note += f" — แชร์ {', '.join(linked)} กับ default"
        note += " · กด “เข้าสู่ระบบ” บนการ์ดเพื่อ login"
        self._users_status(note)
        self._accounts_reload_shared_state()

    def _on_accounts_remove_clicked(self, account: accounts_adapter.AccountInfo) -> None:
        box = cockpit_theme.themed_message_box(self)
        box.setWindowTitle("ลบบัญชี")
        detail = f"\nโฟลเดอร์ข้อมูล ({account.config_dir}) จะยังอยู่ ไม่ถูกลบ" if account.config_dir else ""
        box.setText(f"เอาบัญชี '{account.name}' ออกจากรายการ?{detail}")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            accounts_adapter.remove_account(account)
        except ValueError as exc:
            QMessageBox.warning(self, "ลบไม่ได้", str(exc))
            return
        self._users_status(f"ลบบัญชี '{account.name}' แล้ว")
        self._accounts_reload_shared_state()

    def _on_accounts_share_clicked(self, account: accounts_adapter.AccountInfo) -> None:
        """Convert an EXISTING claude account to shared-session mode — the
        old Users page's "Share sessions with default" button, unchanged
        mechanics (`user_profile.convert_profile_to_shared`)."""
        from . import user_profile

        confirm = QMessageBox.question(
            self,
            "แชร์ session กับ default?",
            f"แปลงบัญชี '{account.name}' ({account.config_dir}) เป็นโหมดแชร์ session?\n\n"
            "• session/todos/plugins/skills เดิมถูกรวมเข้า default —\n"
            "  ไม่มีอะไรถูกเขียนทับ ของเดิมเก็บเป็น *.pre-share-backup\n"
            "• การเข้าสู่ระบบยังแยกกัน — ต่างกันแค่บัญชี\n"
            "• pane ที่เปิดอยู่จะเห็นผลเมื่อ respawn",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if confirm != QMessageBox.StandardButton.Ok:
            return
        results = user_profile.convert_profile_to_shared(account.config_dir)
        QMessageBox.information(
            self,
            "ผลการแชร์ session",
            "\n".join(f"{k}: {v}" for k, v in results.items()),
        )
        self._accounts_refresh()

    def _on_accounts_login_clicked(self, account: accounts_adapter.AccountInfo) -> None:
        launch = accounts_adapter.login_launch(account.provider, account.config_dir)
        if launch is None:
            QMessageBox.information(
                self, "เข้าสู่ระบบ", f"ยังเปิดหน้าต่าง login ของ {account.provider} จากตรงนี้ไม่ได้"
            )
            return
        argv, extra_env = launch
        hint = (
            "ถ้ายังไม่ได้เข้าสู่ระบบ พิมพ์ /login ในหน้าต่างนี้แล้วทำตามขั้นตอน · ปิดหน้าต่างเมื่อเสร็จ"
            if account.provider == "claude"
            else "ทำตามขั้นตอน login ของ provider ในหน้าต่างนี้ · ปิดหน้าต่างเมื่อเสร็จ"
        )
        try:
            dlg = _LoginPaneDialog(
                self,
                f"เข้าสู่ระบบ {account.provider} — บัญชี {account.name}",
                hint,
                argv,
                extra_env,
            )
        except Exception as exc:
            QMessageBox.warning(self, "เปิดหน้าต่าง login ไม่สำเร็จ", str(exc))
            return
        # Refresh the page when the login window closes so the new state shows.
        dlg.finished.connect(lambda _r: self._accounts_refresh())
        self._accounts_login_dialogs.append(dlg)
        dlg.show()

    def _accounts_reload_shared_state(self) -> None:
        """After an add/remove: refresh this page AND the pieces of the old
        Users view that still read the same registry (the per-profile API
        override tab's profile combo)."""
        from . import user_profile

        self._up_profiles = user_profile.list_profiles()
        self._accounts_refresh()
        try:
            self._reload_users_auth_combo()
        except Exception:
            pass
