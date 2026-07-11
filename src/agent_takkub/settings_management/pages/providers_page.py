"""Providers vertical slice (SPEC.md §Providers, Phase 4).

List: Claude (REQUIRED) / Codex / Gemini with installed status (binary
discovery via ``ProviderSpec.custom_discovery_fn``) · detail: General
(binary/status/context strategy — BUILT-IN read-only value rows) +
Capabilities (spec flags) + Assigned roles (read-only summary — editing
happens from the Role's Access tab, per SPEC.md "Relationships").

Custom provider spec CRUD stays hidden in this phase (SPEC.md "ซ่อนหลัง flag
จนมี registry service e2e") — there is no "+ New Provider Spec" button and
built-in providers can never be deleted, so this page has no create flow and
no Danger zone, unlike the other four entity pages."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QVBoxLayout,
    QWidget,
)

from ... import cockpit_theme as theme
from ..commands import UpdateProviderCommand
from ..models import ProviderDetail
from ..repositories import providers as providers_repo
from ..widgets.detail_footer import DetailFooter
from ..widgets.detail_header import DetailHeader
from ..widgets.management_page import ManagementPage


def _flag_row(label: str, on: bool) -> QLabel:
    row = QLabel(f"{'✓' if on else '✗'}  {label}")
    row.setStyleSheet(f"color: {theme.TEXT_PRIMARY if on else theme.TEXT_MUTED};")
    return row


class ProvidersPage(ManagementPage):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "Providers",
            filters=("All",),
            new_button_label="+ New Provider Spec",
            parent=parent,
        )
        # Custom provider spec CRUD is hidden behind a capability flag until
        # the registry service exists (SPEC.md Phase 4) — this page never
        # shows a working "New" button.
        self.list._new_btn.setVisible(False)

        self._detail = QWidget(self)
        detail_layout = QVBoxLayout(self._detail)
        self.header = DetailHeader(self._detail)
        detail_layout.addWidget(self.header)

        general_row = QHBoxLayout()
        self.enabled_toggle = theme.ToggleSwitch(self._detail)
        self.enabled_toggle.toggled.connect(self._on_toggle_changed)
        general_row.addWidget(QLabel("Your override — Enabled"))
        general_row.addWidget(self.enabled_toggle)
        general_row.addStretch(1)
        detail_layout.addLayout(general_row)

        self.required_note = QLabel("")
        self.required_note.setWordWrap(True)
        self.required_note.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        detail_layout.addWidget(self.required_note)

        self._build_general_form(detail_layout)
        self._build_capabilities(detail_layout)
        self._build_roles_section(detail_layout)

        self.footer = DetailFooter(self._detail)
        self.footer.save_clicked.connect(self._on_save_clicked)
        self.footer.discard_clicked.connect(self._on_discard_clicked)
        detail_layout.addWidget(self.footer)

        self.detail_stack.addWidget(self._detail)

        self._current: ProviderDetail | None = None
        self._dirty = False

        self.load_rows = self._load_rows
        self.on_select = self._load_detail
        # No create flow for providers — `on_new` stays the no-op default.
        self.is_dirty = lambda: self._dirty
        self.save = self._save
        self.discard = self._discard

    # ── layout ────────────────────────────────────────────────────

    def _build_general_form(self, parent_layout: QVBoxLayout) -> None:
        form = QFormLayout()
        self.binary_label = QLabel("")
        self.status_label = QLabel("")
        self.context_label = QLabel("")
        form.addRow("Binary", self.binary_label)
        form.addRow("Status", self.status_label)
        form.addRow("Context strategy", self.context_label)
        parent_layout.addLayout(form)

        self.install_note = QLabel("")
        self.install_note.setWordWrap(True)
        self.install_note.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        parent_layout.addWidget(self.install_note)

    def _build_capabilities(self, parent_layout: QVBoxLayout) -> None:
        title = QLabel("Capabilities")
        title.setObjectName("panelTitle")
        parent_layout.addWidget(title)
        self.capabilities_box = QVBoxLayout()
        parent_layout.addLayout(self.capabilities_box)

    def _build_roles_section(self, parent_layout: QVBoxLayout) -> None:
        title = QLabel("Assigned roles")
        title.setObjectName("panelTitle")
        parent_layout.addWidget(title)
        parent_layout.addWidget(
            QLabel("Roles currently backed by this provider (edit from the role's Access tab):")
        )
        self.roles_list = QListWidget()
        parent_layout.addWidget(self.roles_list)
        self.manage_roles_btn = theme.secondary_button("Manage roles", self._detail)
        self.manage_roles_btn.clicked.connect(lambda: self.manage_roles_requested())
        parent_layout.addWidget(self.manage_roles_btn)

    def manage_roles_requested(self) -> None:
        """Reassigned by the window shell to jump to the Roles page — a
        hook-attribute like `load_rows`/`on_select`, not a Qt signal, so the
        page stays importable/testable without a shell present."""

    # ── data plumbing ─────────────────────────────────────────────

    def _load_rows(self) -> list[tuple[str, str]]:
        rows = []
        for p in providers_repo.list():
            status = "REQUIRED" if p.required else ("enabled" if p.enabled else "disabled")
            installed = "installed" if p.installed else "not installed"
            rows.append((p.name, f"{p.label}  ·  {status}  ·  {installed}"))
        return rows

    def _clear_capabilities_box(self) -> None:
        while self.capabilities_box.count():
            item = self.capabilities_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _load_detail(self, entity_id: str) -> None:
        self._current = providers_repo.get(entity_id)
        d = self._current

        self.header.set_entity(d.label, d.name, d.ownership)

        self.enabled_toggle.blockSignals(True)
        self.enabled_toggle.setChecked(d.enabled)
        self.enabled_toggle.setEnabled(not d.required)
        self.enabled_toggle.blockSignals(False)
        self.required_note.setText(
            "Provider นี้เป็น cockpit infrastructure ที่บังคับใช้ — ปิดใช้งานไม่ได้" if d.required else ""
        )

        self.binary_label.setText(" / ".join(d.binary_names) or "(none)")
        status = "✓ installed" if d.installed else "✗ ไม่พบบน PATH"
        if d.installed and d.binary_path:
            status += f" ({d.binary_path})"
        self.status_label.setText(status)
        self.context_label.setText(d.spec_capabilities.context_strategy)
        self.install_note.setText("" if d.installed else d.install_instructions)

        self._clear_capabilities_box()
        sc = d.spec_capabilities
        for label, on in (
            ("Mirror", sc.supports_mirror),
            ("Resume", sc.supports_resume),
            ("Slash commands", sc.supports_slash_commands),
            ("Hooks", sc.supports_hooks),
            ("Browser profiles", sc.supports_browser_profiles),
        ):
            self.capabilities_box.addWidget(_flag_row(label, on))

        self.roles_list.clear()
        self.roles_list.addItems(list(d.assigned_roles) or ["(ไม่มี role ไหนใช้ provider นี้)"])

        self.footer.set_create_mode(False)
        self._dirty = False
        self.footer.set_dirty(False)
        self.detail_stack.setCurrentWidget(self._detail)

    def _on_toggle_changed(self, _checked: bool) -> None:
        if self._current is not None:
            self._dirty = True
            self.footer.set_dirty(True)

    def _save(self) -> bool:
        if self._current is None:
            return False
        result = providers_repo.update(
            self._current.name, UpdateProviderCommand(enabled=self.enabled_toggle.isChecked())
        )
        if not result.ok:
            self._show_error(result.message)
            return False

        self._dirty = False
        self.footer.set_dirty(False)
        self.refresh()
        self.select(self._current.name)
        return True

    def _discard(self) -> None:
        if self._current is not None:
            self._load_detail(self._current.name)
        else:
            self.show_empty()
        self._dirty = False
        self.footer.set_dirty(False)

    def _on_save_clicked(self) -> None:
        self._save()

    def _on_discard_clicked(self) -> None:
        self._discard()

    def _show_error(self, message: str) -> None:
        from PyQt6.QtWidgets import QMessageBox

        box = theme.themed_message_box(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.entity_label)
        box.setText(message or "เกิดข้อผิดพลาด")
        box.exec()
