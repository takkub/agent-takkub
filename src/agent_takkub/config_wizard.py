"""Config Wizard — guided multi-step project setup for projects.json."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

_PRESET_ROLE_CHOICES: list[tuple[str, str]] = [
    ("frontend", "Frontend"),
    ("backend", "Backend"),
    ("mobile", "Mobile"),
    ("devops", "DevOps"),
    ("gemini", "Gemini"),
    ("qa", "QA"),
    ("reviewer", "Reviewer"),
    ("codex", "Codex"),
    ("critic", "Design Critic"),
]

# Common subdirectory names that map to a role key automatically
_AUTO_KEY: dict[str, str] = {
    "web": "web",
    "frontend": "web",
    "client": "web",
    "app": "web",
    "api": "api",
    "backend": "api",
    "server": "api",
    "mobile": "mobile",
    "infra": "infra",
    "infrastructure": "infra",
    "devops": "infra",
    "deploy": "infra",
}


# ──────────────────────────────────────────────
# Page 1 — Project Name & Root Folder
# ──────────────────────────────────────────────
class _NamePage(QWizardPage):
    def __init__(self, existing_names: set[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._existing = existing_names
        self.setTitle("Project Name & Root Folder")
        self.setSubTitle("Set a unique project name and select its root directory.")

        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Project name:"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. my-app")
        name_row.addWidget(self._name_edit)
        lay.addLayout(name_row)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Root folder:  "))
        self._folder_edit = QLineEdit()
        self._folder_edit.setPlaceholderText("Click Browse to select…")
        self._folder_edit.setReadOnly(True)
        btn_browse = QPushButton("Browse…")
        btn_browse.setFixedWidth(72)
        btn_browse.clicked.connect(self._browse)
        folder_row.addWidget(self._folder_edit)
        folder_row.addWidget(btn_browse)
        lay.addLayout(folder_row)

        self._err_label = QLabel()
        self._err_label.setStyleSheet("color:#ef4444;font-size:11px;")
        lay.addWidget(self._err_label)
        lay.addStretch()

        # registerField with * = required (wizard won't advance until isComplete)
        self.registerField("projectName*", self._name_edit)
        self.registerField("rootFolder*", self._folder_edit)
        self._name_edit.textChanged.connect(self.completeChanged)
        self._folder_edit.textChanged.connect(self.completeChanged)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select Project Root Folder")
        if d:
            self._folder_edit.setText(d)
            if not self._name_edit.text().strip():
                self._name_edit.setText(Path(d).name)

    def isComplete(self) -> bool:  # type: ignore[override]
        name = self._name_edit.text().strip()
        folder = self._folder_edit.text().strip()
        if not name or not folder:
            self._err_label.setText("")
            return False
        if name in self._existing:
            self._err_label.setText(f"⚠ '{name}' already exists — choose a different name.")
            return False
        if not Path(folder).is_dir():
            self._err_label.setText("⚠ Folder does not exist.")
            return False
        self._err_label.setText("")
        return True


# ──────────────────────────────────────────────
# Page 2 — Role Path Mapping
# ──────────────────────────────────────────────
class _PathsPage(QWizardPage):
    """Key → folder mapping rows (dynamic, with file browser per row)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTitle("Role Paths")
        self.setSubTitle(
            "Map subdirectories to role keys (web, api, mobile, infra…). "
            "Leave all empty to use the root folder as the sole 'main' path."
        )

        outer = QVBoxLayout(self)
        outer.setSpacing(4)

        self._rows_widget = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)
        self._rows: list[tuple[QWidget, QLineEdit, QLineEdit]] = []

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._rows_widget)
        outer.addWidget(scroll)

        btn_add = QPushButton("+ Add row")
        btn_add.clicked.connect(lambda: self._add_row())
        outer.addWidget(btn_add)

        # Start with one empty row so the page isn't blank
        self._add_row()

    def _add_row(self, key: str = "", path: str = "") -> None:
        row_w = QWidget()
        row_lay = QHBoxLayout(row_w)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(4)

        key_edit = QLineEdit(key)
        key_edit.setPlaceholderText("key (web/api/…)")
        key_edit.setFixedWidth(100)

        path_edit = QLineEdit(path)
        path_edit.setPlaceholderText("folder path…")
        path_edit.setReadOnly(True)

        btn_browse = QPushButton("…")
        btn_browse.setFixedWidth(28)
        btn_browse.setToolTip("Browse folder")
        btn_browse.clicked.connect(lambda: self._browse_path(path_edit))

        btn_del = QPushButton("✕")
        btn_del.setFixedWidth(28)
        btn_del.setStyleSheet("color:#ef4444;")
        btn_del.setToolTip("Remove row")

        pair = (row_w, key_edit, path_edit)
        btn_del.clicked.connect(lambda: self._remove_row(pair))

        row_lay.addWidget(key_edit)
        row_lay.addWidget(path_edit)
        row_lay.addWidget(btn_browse)
        row_lay.addWidget(btn_del)

        self._rows_layout.addWidget(row_w)
        self._rows.append(pair)

    def _remove_row(self, pair: tuple[QWidget, QLineEdit, QLineEdit]) -> None:
        if pair not in self._rows:
            return
        self._rows.remove(pair)
        row_w = pair[0]
        self._rows_layout.removeWidget(row_w)
        row_w.deleteLater()

    def _browse_path(self, path_edit: QLineEdit) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select Folder")
        if d:
            path_edit.setText(d)

    def populate_from_root(self, root: str) -> None:
        """Clear existing rows and pre-populate from root subdirectories."""
        # Remove all current rows
        for pair in list(self._rows):
            row_w = pair[0]
            self._rows_layout.removeWidget(row_w)
            row_w.deleteLater()
        self._rows.clear()

        p = Path(root)
        try:
            subs = [s for s in sorted(p.iterdir()) if s.is_dir() and not s.name.startswith(".")]
        except PermissionError:
            subs = []

        for sub in subs[:8]:
            guessed_key = _AUTO_KEY.get(sub.name.lower(), "")
            self._add_row(key=guessed_key, path=str(sub))

        if not subs:
            # No subdirs — add one empty row
            self._add_row()

    def get_paths(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for _, key_edit, path_edit in self._rows:
            key = key_edit.text().strip()
            path = path_edit.text().strip()
            if key and path and Path(path).is_dir():
                result[key] = str(Path(path).resolve().as_posix())
        return result


# ──────────────────────────────────────────────
# Page 3 — Presets, Profile & Options
# ──────────────────────────────────────────────
class _PresetsProfilePage(QWizardPage):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTitle("Presets, Profile & Options")
        self.setSubTitle(
            "Choose roles to auto-spawn on startup, "
            "assign a Claude user profile, and optionally generate a CLAUDE.md."
        )

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        lay.addWidget(QLabel("Auto-spawn preset roles on startup:"))
        self._checkboxes: dict[str, QCheckBox] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(4)
        for i, (role_name, role_label) in enumerate(_PRESET_ROLE_CHOICES):
            cb = QCheckBox(role_label)
            self._checkboxes[role_name] = cb
            grid.addWidget(cb, i // 3, i % 3)
        lay.addLayout(grid)

        lay.addSpacing(4)

        profile_row = QHBoxLayout()
        profile_label = QLabel("Claude user profile:")
        profile_label.setFixedWidth(140)
        profile_row.addWidget(profile_label)
        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(130)
        self._profile_combo.setToolTip(
            "Sets CLAUDE_CONFIG_DIR for this project so it can log in as a different account."
        )
        profile_row.addWidget(self._profile_combo)
        profile_row.addStretch()
        lay.addLayout(profile_row)

        lay.addSpacing(4)

        self._gen_cb = QCheckBox(
            "Generate CLAUDE.md with AI  (opens description dialog after Finish)"
        )
        lay.addWidget(self._gen_cb)

        lay.addStretch()

    def init_profiles(self) -> None:
        from . import user_profile as _up

        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for p in _up.list_profiles():
            self._profile_combo.addItem(p["name"])
        self._profile_combo.blockSignals(False)

    def selected_presets(self) -> list[str]:
        return [name for name, cb in self._checkboxes.items() if cb.isChecked()]

    def selected_profile(self) -> str:
        return self._profile_combo.currentText() or "default"

    def generate_claude_md(self) -> bool:
        return self._gen_cb.isChecked()


# ──────────────────────────────────────────────
# Public wizard class
# ──────────────────────────────────────────────
class ConfigWizard(QWizard):
    """Multi-step wizard for setting up a new project in projects.json.

    Usage::

        wizard = ConfigWizard(existing_names={"proj-a", "proj-b"}, parent=self)
        if wizard.exec() == QWizard.DialogCode.Accepted:
            data = wizard.result_data()
            # data keys: name, root, paths, presets, profile, generate_claude_md
    """

    def __init__(self, existing_names: set[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Config Wizard — New Project Setup")
        self.resize(560, 480)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setButtonText(QWizard.WizardButton.FinishButton, "Finish & Save")

        self._page1 = _NamePage(existing_names)
        self._page2 = _PathsPage()
        self._page3 = _PresetsProfilePage()

        self.addPage(self._page1)
        self.addPage(self._page2)
        self.addPage(self._page3)

    def initializePage(self, page_id: int) -> None:  # type: ignore[override]
        super().initializePage(page_id)
        if page_id == self.indexOf(self._page2):
            root = self.field("rootFolder")
            if root:
                self._page2.populate_from_root(str(root))
        elif page_id == self.indexOf(self._page3):
            self._page3.init_profiles()

    def result_data(self) -> dict:
        """Return collected wizard data after exec() == Accepted."""
        return {
            "name": self.field("projectName").strip(),
            "root": self.field("rootFolder").strip(),
            "paths": self._page2.get_paths(),
            "presets": self._page3.selected_presets(),
            "profile": self._page3.selected_profile(),
            "generate_claude_md": self._page3.generate_claude_md(),
        }
