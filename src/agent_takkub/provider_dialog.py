"""Role provider config dialog — UI for `~/.takkub/role-providers.json`.

Lets the user pick claude or codex for each non-forced teammate role
without hand-editing JSON. Changes are live: `orchestrator.spawn()`
re-reads the mapping file on every spawn (no in-memory cache), so
the new provider applies to the very next pane the user opens.
Already-running panes are NOT affected — close + respawn them to
flip their provider.

Hard-coded rows (locked, no dropdown):
- Lead  → always claude (claude-specific plumbing demands it)
- Codex → always codex  (the role's identity)
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .provider_config import CLAUDE, CODEX, provider_for, save_providers
from .roles import DEFAULT_TEAMMATES, LEAD


class RoleProviderDialog(QDialog):
    """Modal dialog with one dropdown per overridable teammate role.

    The accept-button is wired to `_on_save` which writes the current
    selections to the JSON config. Main window listens to the dialog's
    `accepted` signal and triggers a cockpit restart so the new
    provider mapping takes effect immediately.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Role Providers")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            "Choose which CLI backs each teammate role. Saving applies\n"
            "to the next pane you spawn — no restart needed. Already-\n"
            "running panes keep their original CLI; close + respawn to\n"
            "flip them. Lead is locked to Claude; Codex role is locked\n"
            "to Codex."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #d4d4d8;")
        layout.addWidget(intro)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        layout.addLayout(form)

        # Locked row: Lead. Shown so the user understands the rule,
        # not because they can change it. Greyed out + non-interactive.
        lead_locked = QLabel("claude   (locked — cockpit pipeline)")
        lead_locked.setStyleSheet("color: #71717a; font-style: italic;")
        form.addRow(f"{LEAD.label}:", lead_locked)

        self._combos: dict[str, QComboBox] = {}
        for role in DEFAULT_TEAMMATES:
            if role.name == "codex":
                locked = QLabel("codex   (locked — role identity)")
                locked.setStyleSheet("color: #71717a; font-style: italic;")
                form.addRow(f"{role.label}:", locked)
                continue
            combo = QComboBox()
            combo.addItems([CLAUDE, CODEX])
            combo.setCurrentText(provider_for(role.name))
            self._combos[role.name] = combo
            form.addRow(f"{role.label}:", combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_save(self) -> None:
        """Persist the current selections to `~/.takkub/role-providers.json`,
        then close the dialog with Accepted so the caller can trigger
        the restart. Claude entries are dropped from the output (default
        = claude, so storing them adds noise to a hand-editable file).
        """
        mapping = {
            role: combo.currentText()
            for role, combo in self._combos.items()
            if combo.currentText() != CLAUDE
        }
        try:
            save_providers(mapping)
        except OSError as e:
            # Surface the disk error inline so the user knows the save
            # didn't land. Keeping the dialog open lets them retry.
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.critical(
                self,
                "Save failed",
                f"Couldn't write role-providers.json:\n{e}",
            )
            return
        self.accept()
