"""Claude auth override dialog."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .claude_auth_config import ClaudeAuthConfig, load_claude_auth, save_claude_auth


class ClaudeAuthDialog(QDialog):
    """Edit optional Claude Code proxy/API env overrides + arbitrary env vars."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Claude Auth")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            "Point Claude Code panes at a different backend — DeepSeek,\n"
            "OpenRouter, a local model — instead of Anthropic. Leave everything\n"
            "blank to keep your normal Claude login. Applies to the next pane\n"
            "you spawn (restart open panes to pick it up)."
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
        self._base_url.setPlaceholderText(
            "blank = Anthropic  ·  e.g. https://api.deepseek.com/anthropic"
        )
        form.addRow("Base URL:", self._base_url)

        self._api_key = QLineEdit(cfg.api_key)
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("your provider's API key  ·  blank = none")
        form.addRow("API key:", self._api_key)

        self._auth_token = QLineEdit(cfg.auth_token)
        self._auth_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._auth_token.setPlaceholderText(
            "usually blank — the API key above is reused as the bearer token"
        )
        form.addRow("Auth token:", self._auth_token)

        note = QLabel(
            "Examples:\n"
            "• DeepSeek — Base URL: https://api.deepseek.com/anthropic + API key: your DeepSeek key\n"
            "• OpenRouter — Base URL: https://openrouter.ai/api + Auth token: your OpenRouter key\n"
            "  (then add ANTHROPIC_DEFAULT_SONNET_MODEL below to choose the model)"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #a1a1aa;")
        layout.addWidget(note)

        # ── Extra environment variables ───────────────────────────────
        # Arbitrary NAME=value pairs injected into every spawned pane (same
        # injection point as the auth fields). Click "+ Add variable" to add a
        # row; the "✕" button removes one.
        env_label = QLabel(
            "Extra environment variables — sent to every pane. Use for a provider key,\n"
            "or to pick a model (e.g. ANTHROPIC_DEFAULT_SONNET_MODEL = qwen/qwen3-coder:free):"
        )
        env_label.setWordWrap(True)
        env_label.setStyleSheet("color: #d4d4d8; padding-top: 4px;")
        layout.addWidget(env_label)

        self._env_rows: list[tuple[QLineEdit, QLineEdit, QWidget]] = []
        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(4)
        layout.addLayout(self._rows_box)

        add_btn = QPushButton("+ Add variable", self)
        add_btn.clicked.connect(lambda: self._add_env_row())
        layout.addWidget(add_btn)

        # Seed from saved config; leave one empty row so there's always a
        # blank pair ready to fill in.
        for name, value in cfg.extra_env.items():
            self._add_env_row(name, value)
        if not self._env_rows:
            self._add_env_row()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_env_row(self, name: str = "", value: str = "") -> None:
        row = QWidget(self)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        name_edit = QLineEdit(name)
        name_edit.setPlaceholderText("NAME — e.g. ANTHROPIC_DEFAULT_SONNET_MODEL")
        value_edit = QLineEdit(value)
        value_edit.setPlaceholderText("value — e.g. qwen/qwen3-coder:free")
        remove_btn = QPushButton("✕", row)
        remove_btn.setFixedWidth(28)
        remove_btn.setToolTip("Remove this variable")

        h.addWidget(name_edit, 2)
        h.addWidget(value_edit, 3)
        h.addWidget(remove_btn, 0)

        entry = (name_edit, value_edit, row)
        self._env_rows.append(entry)
        self._rows_box.addWidget(row)
        remove_btn.clicked.connect(lambda: self._remove_env_row(entry))

    def _remove_env_row(self, entry: tuple[QLineEdit, QLineEdit, QWidget]) -> None:
        if entry in self._env_rows:
            self._env_rows.remove(entry)
        row = entry[2]
        self._rows_box.removeWidget(row)
        row.deleteLater()

    def _collect_env(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name_edit, value_edit, _row in self._env_rows:
            name = name_edit.text().strip()
            if name:
                out[name] = value_edit.text()
        return out

    def _on_save(self) -> None:
        try:
            save_claude_auth(
                ClaudeAuthConfig(
                    base_url=self._base_url.text(),
                    api_key=self._api_key.text(),
                    auth_token=self._auth_token.text(),
                    extra_env=self._collect_env(),
                )
            )
        except OSError as e:
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.critical(self, "Save failed", f"Couldn't write claude-auth.json:\n{e}")
            return
        self.accept()
