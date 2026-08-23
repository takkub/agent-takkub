"""Core V2 Settings views — epic #309 Phase 9 (`CORE V2` sidebar section:
Overview / Accounts & Pools / Routing / Brain / Scheduler / Migration).

A mixin (`CoreV2SettingsMixin`) mixed into `settings_window.SettingsWindow`
— same "UI-layer mixin" shape as `user_actions.UserActionsMixin`/
`project_wizard.ProjectWizardMixin` (see those modules' import-linter
contracts), kept in a separate file rather than growing the 3875-line
`settings_window.py` further. **This module must never import from
`settings_window`** (that direction already goes the other way — `Settings
Window` imports this mixin — importing back would cycle).

Every view here is read-mostly and deliberately NOT wired into
`SettingsWindow`'s existing footer Save & Apply / dirty-tracking transaction
(`_on_save_apply_clicked`'s multi-store snapshot+rollback) — that transaction
already spans 7+ unrelated stores and entangling 6 more (with very different
shapes: append-only JSONL registries, thread-driven read-only reports, a
flag toggle set) would make an already-large rollback surface harder to
reason about for no real benefit. Instead each view either writes through
immediately on its own explicit action (Accounts & Pools' add/edit/remove,
mirroring Templates' Duplicate/Delete-writes-immediately precedent) or has
its own dedicated Save button scoped to just that view's fields (Overview's
flags, Scheduler's SlotPolicy) — never routed through the shared footer.

No UI here ever offers a migration **apply** — inspect/plan/dry-run only,
per the task spec ("ห้ามมีปุ่ม apply ใน UI รอบนี้").
"""

from __future__ import annotations

import json

import psutil
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme, core_v2_settings, performance_settings
from . import roles as roles_mod
from .core.accounts.registry import AccountPoolRegistry, AccountRegistry
from .core.accounts.selector import selector_for
from .core.brain.flag import v2_brain_enabled
from .core.brain.store import BrainStore
from .core.migration.engine import MigrationEngine
from .core.migration.report import StepReport
from .core.models.account import (
    AccountPool,
    AccountStatus,
    ProviderAccount,
    SelectionStrategy,
)
from .core.models.memory import Scope
from .core.routing.facade import effective_provider_for_v2
from .core.routing.flag import v2_router_enabled
from .core.scheduling.backpressure import BackpressureSignal, classify
from .core.scheduling.backpressure import admits as backpressure_admits
from .core.scheduling.flag import v2_scheduler_enabled
from .core.scheduling.models import Priority
from .core.versioning.store import read_version_doc

_FLAG_ROWS: tuple[tuple[str, str, str, bool], ...] = (
    # (config key, TAKKUB_V2_* env name, short description, wired-in-core)
    ("router", "TAKKUB_V2_ROUTER", "Router — quota/cooldown/health-aware provider routing", True),
    (
        "conversation",
        "TAKKUB_V2_CONVERSATION",
        "Conversation — checkpoint/resume/summary store",
        True,
    ),
    (
        "context",
        "TAKKUB_V2_CONTEXT",
        "Context Builder — inject memory เข้า task ตอน assign (ต้องเปิด BRAIN คู่กัน)",
        True,
    ),
    ("brain", "TAKKUB_V2_BRAIN", "Second Brain — memory candidate pipeline + retrieval", True),
    (
        "scheduler",
        "TAKKUB_V2_SCHEDULER",
        "Scheduler — provider/account/project slot limits + backpressure",
        True,
    ),
    (
        "auto_migrate",
        "TAKKUB_AUTO_MIGRATE",
        "Auto migrate storage layout ตอน boot — v1 -> mixed ครั้งเดียว, validate + auto-rollback ในตัว (#361)",
        True,
    ),
)

_PRIORITY_NAMES: tuple[str, ...] = tuple(p.name for p in Priority)


# ──────────────────────────────────────────────────────────────
# worker threads — every genuinely blocking Core V2 call (Brain
# recall/reindex scans the whole store + runs BM25 over it; Migration's
# engine calls do backup/journal disk I/O) runs off the Qt main thread,
# same `QThread` + `resultReady` signal shape as settings_window's own
# `_AutoskillsPreviewThread`.
# ──────────────────────────────────────────────────────────────


class _BrainRecallThread(QThread):
    resultReady: pyqtSignal = pyqtSignal(object)  # list[MemoryRecord] | Exception

    def __init__(self, project: str | None, query: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._project = project
        self._query = query

    def run(self) -> None:
        from .core.brain.facade import recall

        try:
            self.resultReady.emit(recall(self._query, scope=Scope.PROJECT, project=self._project))
        except Exception as e:  # pragma: no cover - facade itself is fail-open
            self.resultReady.emit(e)


class _BrainReindexThread(QThread):
    """ "Reindex" — `RetrievalEngine.recall()` has no persistent index to
    rebuild (it rescans `BrainStore.load_active()` fresh every call, see
    that module's own docstring); this thread's real effect is forcing a
    fresh disk read of every scope/trust bucket off the main thread, useful
    when the store changed on disk since the page was opened."""

    resultReady: pyqtSignal = pyqtSignal(object)  # dict counts | Exception

    def __init__(self, project: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._project = project

    def run(self) -> None:
        try:
            self.resultReady.emit(_brain_counts(self._project))
        except Exception as e:  # pragma: no cover
            self.resultReady.emit(e)


class _MigrationReportThread(QThread):
    resultReady: pyqtSignal = pyqtSignal(object)  # dict[str, list[StepReport]] | Exception

    def __init__(self, stages: tuple[str, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stages = stages

    def run(self) -> None:
        try:
            engine = MigrationEngine()
            dispatch = {"inspect": engine.inspect, "plan": engine.plan, "dry_run": engine.dry_run}
            self.resultReady.emit({stage: dispatch[stage]() for stage in self._stages})
        except Exception as e:  # pragma: no cover
            self.resultReady.emit(e)


def _brain_counts(project: str | None) -> dict[str, dict[str, int]]:
    """Active-record counts by scope + by trust, over the project's own
    store plus the `_global` bucket (`Scope.GLOBAL`/`Scope.USER` records —
    `BrainStore.brain_store_path`'s own docstring) when they differ."""
    stores = [BrainStore(project)]
    if project:
        stores.append(BrainStore(None))
    by_scope: dict[str, int] = {}
    by_trust: dict[str, int] = {}
    total = 0
    for store in stores:
        for record in store.load_active():
            by_scope[record.scope.value] = by_scope.get(record.scope.value, 0) + 1
            by_trust[record.trust.value] = by_trust.get(record.trust.value, 0) + 1
            total += 1
    return {"by_scope": by_scope, "by_trust": by_trust, "total": total}


def _step_report_text(stage: str, reports: list[StepReport]) -> str:
    if not reports:
        return f"[{stage}] (no steps registered)"
    lines = [f"[{stage}]"]
    for r in reports:
        status = "OK" if r.ok else "FAILED"
        lines.append(f"  {r.step_id}: {status} — {r.summary}")
        if r.unknown_fields:
            lines.append(f"    unknown_fields: {', '.join(r.unknown_fields)}")
        if r.detail:
            lines.append(f"    detail: {json.dumps(r.detail, ensure_ascii=False, default=str)}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# dialogs — Account / Pool create-or-edit
# ──────────────────────────────────────────────────────────────


class _AccountEditDialog(QDialog):
    """Create or edit one `ProviderAccount`. `secret_ref` is a pointer into
    the future SecretManager (`core.models.account.ProviderAccount`'s own
    docstring) — this form never has a raw-credential field, only the ref
    string."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        account: ProviderAccount | None = None,
        provider_ids: tuple[str, ...] = (),
    ) -> None:
        super().__init__(parent)
        self._editing = account is not None
        self.setWindowTitle("Edit account" if self._editing else "Add account")
        self.resize(420, 320)

        form = QFormLayout(self)

        self.id_edit = QLineEdit(account.id if account else "", self)
        self.id_edit.setEnabled(not self._editing)
        self.id_edit.setPlaceholderText("account id (unique)")
        form.addRow("ID", self.id_edit)

        self.provider_combo = QComboBox(self)
        self.provider_combo.setEditable(True)
        for pid in provider_ids:
            self.provider_combo.addItem(pid)
        if account:
            self.provider_combo.setCurrentText(account.provider_id)
        form.addRow("Provider", self.provider_combo)

        self.label_edit = QLineEdit(account.label if account else "", self)
        form.addRow("Label", self.label_edit)

        self.secret_ref_edit = QLineEdit(account.secret_ref or "" if account else "", self)
        self.secret_ref_edit.setPlaceholderText("secretRef — never a raw credential")
        form.addRow("Secret ref", self.secret_ref_edit)

        self.status_combo = QComboBox(self)
        for status in AccountStatus:
            self.status_combo.addItem(status.value, status)
        if account:
            idx = self.status_combo.findData(account.status)
            if idx >= 0:
                self.status_combo.setCurrentIndex(idx)
        form.addRow("Status", self.status_combo)

        self.priority_spin = QSpinBox(self)
        self.priority_spin.setRange(-1000, 1000)
        self.priority_spin.setValue(account.priority if account else 0)
        form.addRow("Priority", self.priority_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def result_account(self) -> ProviderAccount | None:
        account_id = self.id_edit.text().strip()
        provider_id = self.provider_combo.currentText().strip()
        if not account_id or not provider_id:
            return None
        return ProviderAccount(
            id=account_id,
            provider_id=provider_id,
            label=self.label_edit.text().strip(),
            secret_ref=self.secret_ref_edit.text().strip() or None,
            status=self.status_combo.currentData() or AccountStatus.ACTIVE,
            priority=self.priority_spin.value(),
        )


class _PoolEditDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        pool: AccountPool | None = None,
        provider_ids: tuple[str, ...] = (),
    ) -> None:
        super().__init__(parent)
        self._editing = pool is not None
        self.setWindowTitle("Edit pool" if self._editing else "Add pool")
        self.resize(440, 340)

        form = QFormLayout(self)

        self.id_edit = QLineEdit(pool.id if pool else "", self)
        self.id_edit.setEnabled(not self._editing)
        self.id_edit.setPlaceholderText("pool id (unique)")
        form.addRow("ID", self.id_edit)

        self.name_edit = QLineEdit(pool.name if pool else "", self)
        form.addRow("Name", self.name_edit)

        self.provider_combo = QComboBox(self)
        self.provider_combo.setEditable(True)
        for pid in provider_ids:
            self.provider_combo.addItem(pid)
        if pool:
            self.provider_combo.setCurrentText(pool.provider_id)
        form.addRow("Provider", self.provider_combo)

        self.account_ids_edit = QLineEdit(", ".join(pool.account_ids) if pool else "", self)
        self.account_ids_edit.setPlaceholderText("comma-separated account ids")
        form.addRow("Account IDs", self.account_ids_edit)

        self.strategy_combo = QComboBox(self)
        for strategy in SelectionStrategy:
            self.strategy_combo.addItem(strategy.value, strategy)
        if pool:
            idx = self.strategy_combo.findData(pool.strategy)
            if idx >= 0:
                self.strategy_combo.setCurrentIndex(idx)
        form.addRow("Strategy", self.strategy_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def result_pool(self) -> AccountPool | None:
        pool_id = self.id_edit.text().strip()
        name = self.name_edit.text().strip()
        provider_id = self.provider_combo.currentText().strip()
        if not pool_id or not provider_id:
            return None
        account_ids = tuple(
            aid.strip() for aid in self.account_ids_edit.text().split(",") if aid.strip()
        )
        return AccountPool(
            id=pool_id,
            name=name or pool_id,
            provider_id=provider_id,
            account_ids=account_ids,
            strategy=self.strategy_combo.currentData() or SelectionStrategy.PRIORITY,
        )


class CoreV2SettingsMixin:
    """Mixed into `SettingsWindow` — every method assumes `self` has the
    attributes `SettingsWindow.__init__` sets (`_project`, `_fonts`, …) plus
    the QSS-driven helpers (`_build_card_header`) that class already
    defines."""

    # ──────────────────────────────────────────────────────────
    # view: Core V2 Overview
    # ──────────────────────────────────────────────────────────

    def _build_core_v2_overview_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(14)

        banner = QLabel(
            "flag เหล่านี้คุมว่า core V2 subsystem แต่ละตัวถูกเรียกจริงหรือไม่ — ปิดทุกตัว = "
            "พฤติกรรมเดิมเป๊ะ (fail-open ทุกจุด) ค่า ENV var (TAKKUB_V2_*) ชนะค่าที่บันทึกไว้ที่นี่เสมอ",
            view,
        )
        banner.setObjectName("infoBanner")
        banner.setWordWrap(True)
        lay.addWidget(banner)

        flags_panel = QWidget(view)
        flags_panel.setObjectName("panel")
        fp_lay = QVBoxLayout(flags_panel)
        fp_lay.setContentsMargins(14, 12, 14, 12)
        fp_lay.setSpacing(10)
        stored = core_v2_settings.load()["flags"]
        fp_lay.addWidget(
            self._build_card_header(
                "CORE V2", "Feature flags", f"{sum(stored.values())}/{len(stored)} on", flags_panel
            )
        )

        self._cv2_flag_toggles: dict[str, cockpit_theme.ToggleSwitch] = {}
        for key, env_name, desc, wired in _FLAG_ROWS:
            row = QWidget(flags_panel)
            row.setObjectName("providerRow")
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(10, 8, 10, 8)
            row_lay.setSpacing(10)

            label = QLabel(env_name, row)
            label.setStyleSheet(
                f'font-family: "{self._fonts["mono"]}"; font-weight: 600; color: {cockpit_theme.TEXT_PRIMARY};'
            )
            label.setFixedWidth(180)
            row_lay.addWidget(label)

            desc_lbl = QLabel(desc, row)
            desc_lbl.setObjectName("panelHint")
            desc_lbl.setWordWrap(True)
            row_lay.addWidget(desc_lbl, 1)

            toggle = cockpit_theme.ToggleSwitch(row, checked=stored.get(key, False))
            toggle.setAccessibleName(f"{env_name} toggle")
            toggle.setEnabled(wired)
            if not wired:
                toggle.setToolTip("ยังไม่มี core module ให้ toggle นี้เชื่อมถึง")
            row_lay.addWidget(toggle)
            self._cv2_flag_toggles[key] = toggle
            fp_lay.addWidget(row)
        lay.addWidget(flags_panel)

        save_row = QHBoxLayout()
        self._cv2_flags_save_btn = cockpit_theme.gold_button("Save flags", flags_panel)
        self._cv2_flags_save_btn.clicked.connect(self._on_cv2_save_flags_clicked)
        save_row.addWidget(self._cv2_flags_save_btn)
        self._cv2_flags_status = QLabel("", flags_panel)
        self._cv2_flags_status.setObjectName("panelHint")
        save_row.addWidget(self._cv2_flags_status)
        save_row.addStretch(1)
        lay.addLayout(save_row)
        save_scope_hint = QLabel("บันทึกทันทีที่นี่ — แยกจากปุ่ม Save & Apply ด้านล่าง", flags_panel)
        save_scope_hint.setObjectName("panelHint")
        lay.addWidget(save_scope_hint)

        version_panel = QWidget(view)
        version_panel.setObjectName("panel")
        vp_lay = QVBoxLayout(version_panel)
        vp_lay.setContentsMargins(14, 12, 14, 12)
        vp_lay.setSpacing(8)
        vp_lay.addWidget(self._build_card_header("STORAGE", "version.json", "", version_panel))
        try:
            components = read_version_doc()
        except Exception:
            components = []
        if not components:
            empty = QLabel("ยังไม่มี version.json record — core store ยังไม่ถูกเขียนครั้งแรก", version_panel)
            empty.setObjectName("panelHint")
            vp_lay.addWidget(empty)
        else:
            for cv in components:
                row = QLabel(
                    f"{cv.component}: {cv.version}"
                    + (f" (id={cv.id})" if cv.id and cv.id != cv.component else ""),
                    version_panel,
                )
                row.setStyleSheet(f'font-family: "{self._fonts["mono"]}"; font-size: 12px;')
                vp_lay.addWidget(row)
        lay.addWidget(version_panel)
        lay.addStretch(1)
        return view

    def _on_cv2_save_flags_clicked(self) -> None:
        try:
            payload = core_v2_settings.load()
            for key, toggle in self._cv2_flag_toggles.items():
                payload["flags"][key] = toggle.isChecked()
            if not core_v2_settings.save(payload):
                raise OSError("write failed")
        except OSError as e:
            self._cv2_flags_status.setText(f"บันทึกไม่สำเร็จ: {e}")
            return
        self._cv2_flags_status.setText("บันทึกแล้ว — ใช้ผลตั้งแต่ pane ถัดไปที่ spawn")

    # ──────────────────────────────────────────────────────────
    # view: Accounts & Pools
    # ──────────────────────────────────────────────────────────

    def _build_core_v2_accounts_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(14)

        accounts_panel = QWidget(view)
        accounts_panel.setObjectName("panel")
        ap_lay = QVBoxLayout(accounts_panel)
        ap_lay.setContentsMargins(14, 12, 14, 12)
        ap_lay.setSpacing(8)
        header_row = QHBoxLayout()
        header_row.addWidget(
            self._build_card_header("ACCOUNTS", "Provider accounts", "", accounts_panel), 1
        )
        add_account_btn = cockpit_theme.secondary_button("+ Add account", accounts_panel)
        add_account_btn.clicked.connect(self._on_cv2_add_account_clicked)
        header_row.addWidget(add_account_btn)
        ap_lay.addLayout(header_row)
        self._cv2_accounts_list = QListWidget(accounts_panel)
        self._cv2_accounts_list.setFrameShape(QFrame.Shape.NoFrame)
        self._cv2_accounts_list.setUniformItemSizes(True)
        ap_lay.addWidget(self._cv2_accounts_list)
        acct_btn_row = QHBoxLayout()
        edit_account_btn = cockpit_theme.secondary_button("Edit", accounts_panel)
        edit_account_btn.clicked.connect(self._on_cv2_edit_account_clicked)
        acct_btn_row.addWidget(edit_account_btn)
        remove_account_btn = cockpit_theme.secondary_button("Remove", accounts_panel)
        remove_account_btn.clicked.connect(self._on_cv2_remove_account_clicked)
        acct_btn_row.addWidget(remove_account_btn)
        acct_btn_row.addStretch(1)
        ap_lay.addLayout(acct_btn_row)
        lay.addWidget(accounts_panel)

        pools_panel = QWidget(view)
        pools_panel.setObjectName("panel")
        pp_lay = QVBoxLayout(pools_panel)
        pp_lay.setContentsMargins(14, 12, 14, 12)
        pp_lay.setSpacing(8)
        pool_header_row = QHBoxLayout()
        pool_header_row.addWidget(
            self._build_card_header("ACCOUNTS", "Account pools", "", pools_panel), 1
        )
        add_pool_btn = cockpit_theme.secondary_button("+ Add pool", pools_panel)
        add_pool_btn.clicked.connect(self._on_cv2_add_pool_clicked)
        pool_header_row.addWidget(add_pool_btn)
        pp_lay.addLayout(pool_header_row)
        self._cv2_pools_list = QListWidget(pools_panel)
        self._cv2_pools_list.setFrameShape(QFrame.Shape.NoFrame)
        self._cv2_pools_list.setUniformItemSizes(True)
        pp_lay.addWidget(self._cv2_pools_list)
        pool_btn_row = QHBoxLayout()
        edit_pool_btn = cockpit_theme.secondary_button("Edit", pools_panel)
        edit_pool_btn.clicked.connect(self._on_cv2_edit_pool_clicked)
        pool_btn_row.addWidget(edit_pool_btn)
        remove_pool_btn = cockpit_theme.secondary_button("Remove", pools_panel)
        remove_pool_btn.clicked.connect(self._on_cv2_remove_pool_clicked)
        pool_btn_row.addWidget(remove_pool_btn)
        pool_btn_row.addStretch(1)
        pp_lay.addLayout(pool_btn_row)
        lay.addWidget(pools_panel)
        lay.addStretch(1)

        self._cv2_accounts_status = QLabel("", view)
        self._cv2_accounts_status.setObjectName("panelHint")
        lay.addWidget(self._cv2_accounts_status)

        self._reload_cv2_accounts()
        return view

    def _cv2_fit_list_height(self, list_widget: QListWidget, *, cap: int = 160) -> None:
        """Size-to-content up to `cap`, instead of always reserving a fixed
        160px box that leaves ~120px of empty panel below a single row
        (design critic nice #1, `docs/v2/phase9-critic-review.md`)."""
        row_height = list_widget.sizeHintForRow(0)
        if row_height <= 0:
            row_height = 24
        frame = 2 * list_widget.frameWidth()
        content_height = list_widget.count() * row_height + frame
        list_widget.setFixedHeight(min(cap, max(content_height, row_height + frame)))

    def _cv2_provider_ids(self) -> tuple[str, ...]:
        from . import provider_spec

        return tuple(sorted(provider_spec.PROVIDER_REGISTRY.keys()))

    def _reload_cv2_accounts(self) -> None:
        accounts = AccountRegistry().all()
        self._cv2_accounts_list.clear()
        if not accounts:
            item = QListWidgetItem("ยังไม่มี account — กด “+ Add account” เพื่อเริ่ม")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._cv2_accounts_list.addItem(item)
        for account in sorted(accounts, key=lambda a: (a.provider_id, -a.priority, a.id)):
            item = QListWidgetItem(
                f"[{account.provider_id}] {account.id}"
                f"  label={account.label or '-'}  priority={account.priority}"
                f"  status={account.status.value}"
                f"  secretRef={'set' if account.secret_ref else 'unset'}"
            )
            item.setData(Qt.ItemDataRole.UserRole, account.id)
            self._cv2_accounts_list.addItem(item)

        pools = AccountPoolRegistry().all()
        self._cv2_pools_list.clear()
        if not pools:
            item = QListWidgetItem("ยังไม่มี pool — กด “+ Add pool” เพื่อเริ่ม")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._cv2_pools_list.addItem(item)
        for pool in sorted(pools, key=lambda p: (p.provider_id, p.id)):
            item = QListWidgetItem(
                f'[{pool.provider_id}] {pool.id} "{pool.name}"'
                f"  strategy={pool.strategy.value}"
                f"  accounts={len(pool.account_ids)}"
            )
            item.setData(Qt.ItemDataRole.UserRole, pool.id)
            self._cv2_pools_list.addItem(item)

        self._cv2_fit_list_height(self._cv2_accounts_list)
        self._cv2_fit_list_height(self._cv2_pools_list)

    def _cv2_selected_account_id(self) -> str | None:
        item = self._cv2_accounts_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _cv2_selected_pool_id(self) -> str | None:
        item = self._cv2_pools_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_cv2_add_account_clicked(self) -> None:
        dlg = _AccountEditDialog(self, provider_ids=self._cv2_provider_ids())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        account = dlg.result_account()
        if account is None:
            self._cv2_accounts_status.setText("ต้องกรอก ID และ Provider")
            return
        if AccountRegistry().get(account.id) is not None:
            self._cv2_accounts_status.setText(f"account id '{account.id}' มีอยู่แล้ว")
            return
        AccountRegistry().upsert(account)
        self._reload_cv2_accounts()
        self._cv2_accounts_status.setText(f"เพิ่ม account '{account.id}' แล้ว")

    def _on_cv2_edit_account_clicked(self) -> None:
        account_id = self._cv2_selected_account_id()
        if account_id is None:
            self._cv2_accounts_status.setText("เลือก account ก่อน")
            return
        registry = AccountRegistry()
        existing = registry.get(account_id)
        if existing is None:
            self._cv2_accounts_status.setText("account นี้ถูกลบไปแล้ว")
            self._reload_cv2_accounts()
            return
        dlg = _AccountEditDialog(self, account=existing, provider_ids=self._cv2_provider_ids())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dlg.result_account()
        if updated is None:
            return
        registry.upsert(updated)
        self._reload_cv2_accounts()
        self._cv2_accounts_status.setText(f"แก้ไข account '{updated.id}' แล้ว")

    def _on_cv2_remove_account_clicked(self) -> None:
        account_id = self._cv2_selected_account_id()
        if account_id is None:
            self._cv2_accounts_status.setText("เลือก account ก่อน")
            return
        box = cockpit_theme.themed_message_box(self)
        box.setWindowTitle("Remove account")
        box.setText(f"ลบ account '{account_id}' ออกจาก registry?")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        AccountRegistry().delete(account_id)
        self._reload_cv2_accounts()
        self._cv2_accounts_status.setText(f"ลบ account '{account_id}' แล้ว")

    def _on_cv2_add_pool_clicked(self) -> None:
        dlg = _PoolEditDialog(self, provider_ids=self._cv2_provider_ids())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        pool = dlg.result_pool()
        if pool is None:
            self._cv2_accounts_status.setText("ต้องกรอก ID และ Provider")
            return
        if AccountPoolRegistry().get(pool.id) is not None:
            self._cv2_accounts_status.setText(f"pool id '{pool.id}' มีอยู่แล้ว")
            return
        AccountPoolRegistry().upsert(pool)
        self._reload_cv2_accounts()
        self._cv2_accounts_status.setText(f"เพิ่ม pool '{pool.id}' แล้ว")

    def _on_cv2_edit_pool_clicked(self) -> None:
        pool_id = self._cv2_selected_pool_id()
        if pool_id is None:
            self._cv2_accounts_status.setText("เลือก pool ก่อน")
            return
        registry = AccountPoolRegistry()
        existing = registry.get(pool_id)
        if existing is None:
            self._cv2_accounts_status.setText("pool นี้ถูกลบไปแล้ว")
            self._reload_cv2_accounts()
            return
        dlg = _PoolEditDialog(self, pool=existing, provider_ids=self._cv2_provider_ids())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dlg.result_pool()
        if updated is None:
            return
        registry.upsert(updated)
        self._reload_cv2_accounts()
        self._cv2_accounts_status.setText(f"แก้ไข pool '{updated.id}' แล้ว")

    def _on_cv2_remove_pool_clicked(self) -> None:
        pool_id = self._cv2_selected_pool_id()
        if pool_id is None:
            self._cv2_accounts_status.setText("เลือก pool ก่อน")
            return
        box = cockpit_theme.themed_message_box(self)
        box.setWindowTitle("Remove pool")
        box.setText(f"ลบ pool '{pool_id}' ออกจาก registry?")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        AccountPoolRegistry().delete(pool_id)
        self._reload_cv2_accounts()
        self._cv2_accounts_status.setText(f"ลบ pool '{pool_id}' แล้ว")

    # ──────────────────────────────────────────────────────────
    # view: Routing (read-only preview)
    # ──────────────────────────────────────────────────────────

    def _build_core_v2_routing_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(14)

        flag_state = "ON" if v2_router_enabled() else "OFF (ใช้ static map เดิม)"
        flag_lbl = QLabel(f"TAKKUB_V2_ROUTER: {flag_state}", view)
        flag_lbl.setObjectName("infoBanner")
        lay.addWidget(flag_lbl)

        panel = QWidget(view)
        panel.setObjectName("panel")
        p_lay = QVBoxLayout(panel)
        p_lay.setContentsMargins(14, 12, 14, 12)
        p_lay.setSpacing(10)
        p_lay.addWidget(self._build_card_header("ROUTING", "Preview", "", panel))

        role_row = QHBoxLayout()
        role_row.addWidget(QLabel("Role:", panel))
        self._cv2_routing_role_combo = QComboBox(panel)
        for role in roles_mod.all_role_names(include_lead=True):
            self._cv2_routing_role_combo.addItem(role)
        self._cv2_routing_role_combo.currentTextChanged.connect(self._on_cv2_routing_role_changed)
        role_row.addWidget(self._cv2_routing_role_combo)
        role_row.addStretch(1)
        p_lay.addLayout(role_row)

        self._cv2_routing_result = QPlainTextEdit(panel)
        self._cv2_routing_result.setReadOnly(True)
        self._cv2_routing_result.setStyleSheet(f'font-family: "{self._fonts["mono"]}";')
        self._cv2_routing_result.setFixedHeight(220)
        p_lay.addWidget(self._cv2_routing_result)
        lay.addWidget(panel)
        lay.addStretch(1)

        if self._cv2_routing_role_combo.count():
            self._on_cv2_routing_role_changed(self._cv2_routing_role_combo.currentText())
        return view

    def _on_cv2_routing_role_changed(self, role: str) -> None:
        if not role:
            return
        lines = [f"role: {role}", f"project: {self._project or '(no project)'}"]
        try:
            provider = effective_provider_for_v2(role, self._project)
            lines.append(f"resolved provider: {provider}")
        except Exception as e:  # fail-open display — never crash the dialog
            lines.append(f"resolve failed: {e}")
            provider = None

        pools = [p for p in AccountPoolRegistry().all() if p.provider_id == provider]
        if not pools:
            lines.append("account pools: (ไม่มี pool สำหรับ provider นี้)")
        else:
            accounts = AccountRegistry().all()
            for pool in pools:
                lines.append(f"pool '{pool.id}' (strategy={pool.strategy.value}):")
                if pool.strategy == SelectionStrategy.MANUAL:
                    lines.append("  (MANUAL strategy — ต้องระบุ account id, ไม่มี default preview)")
                    continue
                try:
                    selector = selector_for(pool.strategy)
                    picked = selector.select(pool, accounts)
                    lines.append(f"  would select: {picked.id if picked else '(none active)'}")
                except Exception as e:  # pragma: no cover - defensive, selector is pure
                    lines.append(f"  preview failed: {e}")
        self._cv2_routing_result.setPlainText("\n".join(lines))

    # ──────────────────────────────────────────────────────────
    # view: Brain
    # ──────────────────────────────────────────────────────────

    def _build_core_v2_brain_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(14)

        flag_state = "ON" if v2_brain_enabled() else "OFF (recall()/submit() คืนค่าว่างเสมอ)"
        flag_lbl = QLabel(f"TAKKUB_V2_BRAIN: {flag_state}", view)
        flag_lbl.setObjectName("infoBanner")
        lay.addWidget(flag_lbl)

        counts_panel = QWidget(view)
        counts_panel.setObjectName("panel")
        cp_lay = QVBoxLayout(counts_panel)
        cp_lay.setContentsMargins(14, 12, 14, 12)
        cp_lay.setSpacing(8)
        header_row = QHBoxLayout()
        header_row.addWidget(
            self._build_card_header("SECOND BRAIN", "Memory counts", "", counts_panel), 1
        )
        reindex_btn = cockpit_theme.secondary_button("Reindex", counts_panel)
        reindex_btn.clicked.connect(self._on_cv2_brain_reindex_clicked)
        header_row.addWidget(reindex_btn)
        cp_lay.addLayout(header_row)
        self._cv2_brain_counts_lbl = QLabel("", counts_panel)
        self._cv2_brain_counts_lbl.setObjectName("panelHint")
        self._cv2_brain_counts_lbl.setWordWrap(True)
        cp_lay.addWidget(self._cv2_brain_counts_lbl)
        lay.addWidget(counts_panel)

        search_panel = QWidget(view)
        search_panel.setObjectName("panel")
        sp_lay = QVBoxLayout(search_panel)
        sp_lay.setContentsMargins(14, 12, 14, 12)
        sp_lay.setSpacing(8)
        sp_lay.addWidget(
            self._build_card_header("SECOND BRAIN", "Search (recall)", "", search_panel)
        )
        search_row = QHBoxLayout()
        self._cv2_brain_query_edit = QLineEdit(search_panel)
        self._cv2_brain_query_edit.setPlaceholderText("ค้นหา memory…")
        search_row.addWidget(self._cv2_brain_query_edit, 1)
        self._cv2_brain_search_btn = cockpit_theme.gold_button("Search", search_panel)
        self._cv2_brain_search_btn.clicked.connect(self._on_cv2_brain_search_clicked)
        search_row.addWidget(self._cv2_brain_search_btn)
        sp_lay.addLayout(search_row)
        self._cv2_brain_results = QListWidget(search_panel)
        self._cv2_brain_results.setFrameShape(QFrame.Shape.NoFrame)
        placeholder = QListWidgetItem("พิมพ์คำค้นแล้วกด Search เพื่อดูผลลัพธ์")
        placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
        self._cv2_brain_results.addItem(placeholder)
        sp_lay.addWidget(self._cv2_brain_results)
        lay.addWidget(search_panel, 1)

        self._cv2_brain_reindex_thread: _BrainReindexThread | None = None
        self._cv2_brain_recall_thread: _BrainRecallThread | None = None
        # Not fetched eagerly at construction — every SettingsWindow() build
        # would otherwise spin up a background thread even when this view is
        # never opened (dialogs are constructed fresh per settings_window's
        # own class docstring). Loads on first "Reindex" press instead.
        self._cv2_brain_counts_lbl.setText("กด “Reindex” เพื่อโหลดจำนวน memory")
        return view

    def _refresh_cv2_brain_counts(self) -> None:
        self._cv2_brain_counts_lbl.setText("กำลังนับ…")
        thread = _BrainReindexThread(self._project, self)
        thread.resultReady.connect(self._on_cv2_brain_counts_ready)
        self._cv2_brain_reindex_thread = thread
        thread.start()

    def _on_cv2_brain_counts_ready(self, result: object) -> None:
        if isinstance(result, Exception):
            self._cv2_brain_counts_lbl.setText(f"นับไม่สำเร็จ: {result}")
            return
        if result["total"] == 0:
            self._cv2_brain_counts_lbl.setText("ยังไม่มี memory record — store ว่างเปล่า")
            return
        scope_text = ", ".join(f"{k}={v}" for k, v in sorted(result["by_scope"].items()))
        trust_text = ", ".join(f"{k}={v}" for k, v in sorted(result["by_trust"].items()))
        self._cv2_brain_counts_lbl.setText(
            f"total={result['total']}\nby scope: {scope_text}\nby trust: {trust_text}"
        )

    def _on_cv2_brain_reindex_clicked(self) -> None:
        self._refresh_cv2_brain_counts()

    def _on_cv2_brain_search_clicked(self) -> None:
        query = self._cv2_brain_query_edit.text().strip()
        self._cv2_brain_results.clear()
        if not query:
            return
        self._cv2_brain_search_btn.setEnabled(False)
        thread = _BrainRecallThread(self._project, query, self)
        thread.resultReady.connect(self._on_cv2_brain_search_ready)
        self._cv2_brain_recall_thread = thread
        thread.start()

    def _on_cv2_brain_search_ready(self, result: object) -> None:
        self._cv2_brain_search_btn.setEnabled(True)
        self._cv2_brain_results.clear()
        if isinstance(result, Exception):
            self._cv2_brain_results.addItem(f"ค้นหาไม่สำเร็จ: {result}")
            return
        if not result:
            self._cv2_brain_results.addItem("(ไม่พบผลลัพธ์ — หรือ TAKKUB_V2_BRAIN ปิดอยู่)")
            return
        for record in result:
            self._cv2_brain_results.addItem(
                f"[{record.scope.value}/{record.trust.value}] {record.content[:120]}"
            )

    # ──────────────────────────────────────────────────────────
    # view: Scheduler
    # ──────────────────────────────────────────────────────────

    def _build_core_v2_scheduler_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(14)

        flag_state = (
            "ON" if v2_scheduler_enabled() else "OFF (extended limits/backpressure ไม่ทำงาน)"
        )
        flag_lbl = QLabel(f"TAKKUB_V2_SCHEDULER: {flag_state}", view)
        flag_lbl.setObjectName("infoBanner")
        lay.addWidget(flag_lbl)

        policy = core_v2_settings.load_scheduler_policy()

        policy_panel = QWidget(view)
        policy_panel.setObjectName("panel")
        pol_lay = QVBoxLayout(policy_panel)
        pol_lay.setContentsMargins(14, 12, 14, 12)
        pol_lay.setSpacing(8)
        pol_lay.addWidget(self._build_card_header("SCHEDULER", "SlotPolicy", "", policy_panel))

        form = QFormLayout()
        self._cv2_max_agents_spin = QSpinBox(policy_panel)
        self._cv2_max_agents_spin.setRange(0, 999)
        self._cv2_max_agents_spin.setSpecialValueText("unlimited")
        self._cv2_max_agents_spin.setValue(policy.max_agents_global or 0)
        form.addRow("Max agents (global)", self._cv2_max_agents_spin)

        self._cv2_max_panes_spin = QSpinBox(policy_panel)
        self._cv2_max_panes_spin.setRange(0, 999)
        self._cv2_max_panes_spin.setSpecialValueText("unlimited")
        self._cv2_max_panes_spin.setValue(policy.max_panes_global or 0)
        form.addRow("Max panes (global)", self._cv2_max_panes_spin)

        self._cv2_priority_combo = QComboBox(policy_panel)
        for name in _PRIORITY_NAMES:
            self._cv2_priority_combo.addItem(name)
        default_idx = self._cv2_priority_combo.findText(policy.default_priority.upper())
        self._cv2_priority_combo.setCurrentIndex(default_idx if default_idx >= 0 else 2)
        form.addRow("Default priority", self._cv2_priority_combo)
        pol_lay.addLayout(form)

        dict_hint = QLabel(
            "provider / account / project limit — หนึ่งบรรทัดต่อรายการ รูปแบบ id=จำนวน (ว่าง = ไม่จำกัด)",
            policy_panel,
        )
        dict_hint.setObjectName("panelHint")
        dict_hint.setWordWrap(True)
        pol_lay.addWidget(dict_hint)

        self._cv2_provider_limits_edit = self._cv2_dict_editor(
            policy_panel, "Provider max concurrent", policy.provider_max_concurrent
        )
        pol_lay.addWidget(self._cv2_provider_limits_edit[0])
        self._cv2_account_limits_edit = self._cv2_dict_editor(
            policy_panel, "Account max concurrent", policy.account_max_concurrent
        )
        pol_lay.addWidget(self._cv2_account_limits_edit[0])
        self._cv2_project_agents_limits_edit = self._cv2_dict_editor(
            policy_panel, "Project max agents", policy.project_max_agents
        )
        pol_lay.addWidget(self._cv2_project_agents_limits_edit[0])
        self._cv2_project_panes_limits_edit = self._cv2_dict_editor(
            policy_panel, "Project max panes", policy.project_max_panes
        )
        pol_lay.addWidget(self._cv2_project_panes_limits_edit[0])

        save_row = QHBoxLayout()
        self._cv2_scheduler_save_btn = cockpit_theme.gold_button("Save policy", policy_panel)
        self._cv2_scheduler_save_btn.clicked.connect(self._on_cv2_save_scheduler_policy_clicked)
        save_row.addWidget(self._cv2_scheduler_save_btn)
        self._cv2_scheduler_status = QLabel("", policy_panel)
        self._cv2_scheduler_status.setObjectName("panelHint")
        save_row.addWidget(self._cv2_scheduler_status)
        save_row.addStretch(1)
        pol_lay.addLayout(save_row)
        save_scope_hint = QLabel("บันทึกทันทีที่นี่ — แยกจากปุ่ม Save & Apply ด้านล่าง", policy_panel)
        save_scope_hint.setObjectName("panelHint")
        pol_lay.addWidget(save_scope_hint)
        lay.addWidget(policy_panel)

        lay.addWidget(self._build_cv2_backpressure_panel(view))
        lay.addStretch(1)
        return view

    def _cv2_dict_editor(
        self, parent: QWidget, title: str, values: dict[str, int]
    ) -> tuple[QWidget, QPlainTextEdit]:
        wrap = QWidget(parent)
        w_lay = QVBoxLayout(wrap)
        w_lay.setContentsMargins(0, 4, 0, 4)
        w_lay.setSpacing(4)
        lbl = QLabel(title, wrap)
        lbl.setObjectName("panelHint")
        w_lay.addWidget(lbl)
        edit = QPlainTextEdit(wrap)
        edit.setPlainText("\n".join(f"{k}={v}" for k, v in sorted(values.items())))
        edit.setFixedHeight(60)
        edit.setStyleSheet(f'font-family: "{self._fonts["mono"]}"; font-size: 12px;')
        w_lay.addWidget(edit)
        return wrap, edit

    def _cv2_parse_dict_editor(self, editor: QPlainTextEdit) -> dict[str, int]:
        result: dict[str, int] = {}
        for line in editor.toPlainText().splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            try:
                result[key] = int(value.strip())
            except ValueError:
                continue
        return result

    def _build_cv2_backpressure_panel(self, parent: QWidget) -> QWidget:
        """Live CPU/RAM snapshot classified against `performance_settings`'
        thresholds — the closest honest read-only "backpressure state" this
        dialog can show. The real `overloaded` latch + queue depth live only
        on the running orchestrator's in-memory `ResourceGovernor`, which
        this module is architecturally forbidden from reaching into (its own
        header docstring: "MUST NOT import app or cli") — so this is
        explicitly labeled an estimate, never presented as that live state.
        """
        panel = QWidget(parent)
        panel.setObjectName("panel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        lay.addWidget(self._build_card_header("SCHEDULER", "Backpressure (estimate)", "", panel))
        note = QLabel(
            "ประมาณการจาก CPU/RAM ปัจจุบัน + threshold ใน Performance — ไม่ใช่สถานะ live ของ "
            "orchestrator ที่กำลังรันอยู่ (queue depth / overloaded latch อยู่ใน process นั้นเท่านั้น)",
            panel,
        )
        note.setObjectName("panelHint")
        note.setWordWrap(True)
        lay.addWidget(note)

        try:
            perf = performance_settings.load()
            cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            available_pct = (vm.available / vm.total * 100.0) if vm.total else 100.0
            signal = BackpressureSignal(
                cpu_percent=cpu,
                available_ram_percent=available_pct,
                overloaded=False,
                cpu_pause_percent=perf.cpu_pause_percent,
                cpu_resume_percent=perf.cpu_resume_percent,
                min_available_ram_percent=perf.min_available_ram_percent,
            )
            level = classify(signal)
            lines = [
                f"CPU: {cpu:.0f}%  (resume<{perf.cpu_resume_percent:.0f}  pause>{perf.cpu_pause_percent:.0f})",
                f"RAM available: {available_pct:.0f}%  (min {perf.min_available_ram_percent:.0f}%)",
                f"estimated level: {level.value}",
            ]
            admitted = [p.name for p in Priority if backpressure_admits(level, p)]
            lines.append("would admit priority: " + ", ".join(admitted))
        except Exception as e:  # pragma: no cover - psutil sampling is best-effort
            lines = [f"อ่านค่าไม่สำเร็จ: {e}"]
        result_lbl = QLabel("\n".join(lines), panel)
        result_lbl.setStyleSheet(f'font-family: "{self._fonts["mono"]}"; font-size: 12px;')
        lay.addWidget(result_lbl)
        return panel

    def _on_cv2_save_scheduler_policy_clicked(self) -> None:
        try:
            max_agents = self._cv2_max_agents_spin.value() or None
            max_panes = self._cv2_max_panes_spin.value() or None
            policy = core_v2_settings.SchedulerPolicyConfig(
                max_agents_global=max_agents,
                max_panes_global=max_panes,
                provider_max_concurrent=self._cv2_parse_dict_editor(
                    self._cv2_provider_limits_edit[1]
                ),
                account_max_concurrent=self._cv2_parse_dict_editor(
                    self._cv2_account_limits_edit[1]
                ),
                project_max_agents=self._cv2_parse_dict_editor(
                    self._cv2_project_agents_limits_edit[1]
                ),
                project_max_panes=self._cv2_parse_dict_editor(
                    self._cv2_project_panes_limits_edit[1]
                ),
                default_priority=self._cv2_priority_combo.currentText().lower(),
            )
            if not core_v2_settings.save_scheduler_policy(policy):
                raise OSError("write failed")
        except OSError as e:
            self._cv2_scheduler_status.setText(f"บันทึกไม่สำเร็จ: {e}")
            return
        self._cv2_scheduler_status.setText("บันทึกแล้ว")

    # ──────────────────────────────────────────────────────────
    # view: Migration
    # ──────────────────────────────────────────────────────────

    def _build_core_v2_migration_view(self) -> QWidget:
        view = QWidget(self)
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.setSpacing(14)

        banner = QLabel(
            "inspect/plan/dry-run เท่านั้น — apply ทำผ่าน CLI (`takkub migrate apply`) เท่านั้น ไม่มีปุ่มนี้ใน UI",
            view,
        )
        banner.setObjectName("infoBanner")
        banner.setWordWrap(True)
        lay.addWidget(banner)

        btn_row = QHBoxLayout()
        self._cv2_migration_refresh_btn = cockpit_theme.secondary_button(
            "Refresh (inspect + plan)", view
        )
        self._cv2_migration_refresh_btn.clicked.connect(self._on_cv2_migration_refresh_clicked)
        btn_row.addWidget(self._cv2_migration_refresh_btn)
        self._cv2_migration_dry_run_btn = cockpit_theme.secondary_button("Dry-run", view)
        self._cv2_migration_dry_run_btn.clicked.connect(self._on_cv2_migration_dry_run_clicked)
        btn_row.addWidget(self._cv2_migration_dry_run_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self._cv2_migration_report = QPlainTextEdit(view)
        self._cv2_migration_report.setReadOnly(True)
        self._cv2_migration_report.setStyleSheet(
            f'font-family: "{self._fonts["mono"]}"; font-size: 12px;'
        )
        lay.addWidget(self._cv2_migration_report, 1)

        self._cv2_migration_thread: _MigrationReportThread | None = None
        # Not fetched eagerly, same reasoning as Brain's counts above — loads
        # on first "Refresh" press instead of on every SettingsWindow() build.
        self._cv2_migration_report.setPlainText("กด “Refresh (inspect + plan)” เพื่อโหลด report")
        return view

    def _cv2_run_migration_stages(self, stages: tuple[str, ...]) -> None:
        self._cv2_migration_refresh_btn.setEnabled(False)
        self._cv2_migration_dry_run_btn.setEnabled(False)
        self._cv2_migration_report.setPlainText("กำลังรัน…")
        thread = _MigrationReportThread(stages, self)
        thread.resultReady.connect(self._on_cv2_migration_report_ready)
        self._cv2_migration_thread = thread
        thread.start()

    def _on_cv2_migration_refresh_clicked(self) -> None:
        self._cv2_run_migration_stages(("inspect", "plan"))

    def _on_cv2_migration_dry_run_clicked(self) -> None:
        self._cv2_run_migration_stages(("dry_run",))

    def _on_cv2_migration_report_ready(self, result: object) -> None:
        self._cv2_migration_refresh_btn.setEnabled(True)
        self._cv2_migration_dry_run_btn.setEnabled(True)
        if isinstance(result, Exception):
            self._cv2_migration_report.setPlainText(f"รันไม่สำเร็จ: {result}")
            return
        blocks = [_step_report_text(stage, reports) for stage, reports in result.items()]
        self._cv2_migration_report.setPlainText("\n\n".join(blocks))
