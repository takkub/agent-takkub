"""Claude auth override dialog."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from .claude_auth_config import ClaudeAuthConfig, load_claude_auth, save_claude_auth


class ClaudeAuthDialog(QDialog):
    """Edit optional Claude Code proxy/API env overrides."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Claude Auth")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            "Optional Claude Code auth overrides. Leave fields blank to use\n"
            "Claude Code's default login/session exactly as-is. Saved values\n"
            "apply to the next Claude pane you spawn; restart existing panes\n"
            "to pick up changes."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #d4d4d8;")
        layout.addWidget(intro)

        cfg = load_claude_auth()
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        layout.addLayout(form)

        self._base_url = QLineEdit(cfg.base_url)
        self._base_url.setPlaceholderText("default Claude Code endpoint")
        form.addRow("Base URL:", self._base_url)

        self._api_key = QLineEdit(cfg.api_key)
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("blank = no ANTHROPIC_API_KEY override")
        form.addRow("API key:", self._api_key)

        self._auth_token = QLineEdit(cfg.auth_token)
        self._auth_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._auth_token.setPlaceholderText("blank = reuse API key for proxy bearer auth")
        form.addRow("Auth token:", self._auth_token)

        note = QLabel(
            "Proxy setups usually need Base URL + API key/token. When Base URL\n"
            "is set and Auth token is blank, the API key is also sent as a\n"
            "Bearer token for Claude-compatible proxy compatibility."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #a1a1aa;")
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_save(self) -> None:
        try:
            save_claude_auth(
                ClaudeAuthConfig(
                    base_url=self._base_url.text(),
                    api_key=self._api_key.text(),
                    auth_token=self._auth_token.text(),
                )
            )
        except OSError as e:
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.critical(self, "Save failed", f"Couldn't write claude-auth.json:\n{e}")
            return
        self.accept()
