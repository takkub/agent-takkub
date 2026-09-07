"""Core V2 Settings views — epic #309 Phase 9 (`ADVANCED` sidebar section,
folded by default: Routing / Brain / Scheduler — Overview and Migration were
removed in the settings-nav declutter: Overview duplicated `takkub doctor`'s
own status view and its flags default-on since 1.0.84 per
`core_v2_settings._DEFAULT_FLAGS`; Migration's inspect/plan/dry-run duplicated
the `takkub migrate` CLI and boot already runs `auto_migrate_boot` (#361) —
apply on prod completed 2026-08-23. `v2_authority`'s default flip (#362
Phase 10, 2.0.0) was an env-flag decision, not something driven by a
Settings page — `TAKKUB_V2_AUTHORITY=0` remains the escape hatch. The
Accounts & Pools page merged into the unified **Accounts** page (#505,
`settings_accounts.py` + `accounts_adapter.py`); its `VIEW_CORE_V2_ACCOUNTS`
route redirects there. Pool CRUD has no UI for now — nothing populates pools
today (`core.accounts.facade`'s own docstring) and the routing preview below
still reads them).

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
already spans 7+ unrelated stores and entangling more (with very different
shapes: append-only JSONL registries, thread-driven read-only reports) would
make an already-large rollback surface harder to reason about for no real
benefit. Instead each view has its own dedicated Save button scoped to just
that view's fields (Scheduler's SlotPolicy) — never routed through the
shared footer.
"""

from __future__ import annotations

import psutil
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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
from .core.models.account import SelectionStrategy
from .core.models.memory import Scope
from .core.routing.facade import effective_provider_for_v2
from .core.routing.flag import v2_router_enabled
from .core.scheduling.backpressure import BackpressureSignal, classify
from .core.scheduling.backpressure import admits as backpressure_admits
from .core.scheduling.flag import v2_scheduler_enabled
from .core.scheduling.models import Priority

_PRIORITY_NAMES: tuple[str, ...] = tuple(p.name for p in Priority)


# ──────────────────────────────────────────────────────────────
# worker threads — every genuinely blocking Core V2 call (Brain
# recall/reindex scans the whole store + runs BM25 over it) runs off the Qt
# main thread, same `QThread` + `resultReady` signal shape as
# settings_window's own `_AutoskillsPreviewThread`.
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


class CoreV2SettingsMixin:
    """Mixed into `SettingsWindow` — every method assumes `self` has the
    attributes `SettingsWindow.__init__` sets (`_project`, `_fonts`, …) plus
    the QSS-driven helpers (`_build_card_header`) that class already
    defines."""

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
