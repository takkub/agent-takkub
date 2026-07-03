"""_RulesGeneratorThread + ProjectWizardMixin — new/import project wizard (refactor round 3, step B).

Extracted from ``MainWindow`` as a mixin. Methods access ``self.*``
attributes (``_btn_add_project``, ``_status``, ``tabs``, ``orch``, etc.)
initialised in ``MainWindow.__init__``.

**Import constraint:** this module MUST NOT import ``app`` or ``cli``.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from .config import _write_json_atomic
from .project_tab import ProjectTab


class _RulesGeneratorThread(QThread):
    """Background thread that runs claude headless to generate project rules.

    Signals
    -------
    finished(str)  — emits the generated markdown on success
    failed(str)    — emits an error message on failure (incl. cancel)
    """

    rulesReady: pyqtSignal = pyqtSignal(str)
    failed: pyqtSignal = pyqtSignal(str)

    def __init__(self, prompt: str, project_name: str, parent=None) -> None:
        super().__init__(parent)
        self._prompt = prompt
        self._project_name = project_name
        self._proc = None  # subprocess.Popen — set in run(), cleared after

    def run(self) -> None:
        from .project_rules import collect_result, generate_project_rules_proc

        try:
            proc = generate_project_rules_proc(self._prompt, self._project_name)
            self._proc = proc
            content = collect_result(proc, self._project_name)
            self._proc = None
            self.rulesReady.emit(content)
        except Exception as exc:
            self._proc = None
            self.failed.emit(str(exc))

    def cancel(self) -> None:
        """Kill the claude subprocess if it's still running."""
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass


class ProjectWizardMixin:
    """Mixin for new/import project wizard and project editing."""

    def _on_add_project_clicked(self) -> None:
        """Show a choice dialog: New project (AI-generated rules) vs Import existing."""
        from PyQt6.QtWidgets import QMessageBox

        msg = QMessageBox(self)
        msg.setWindowTitle("Add project")
        msg.setText("How do you want to add this project?")
        btn_new = msg.addButton(
            "✨ New project (AI-generated rules)", QMessageBox.ButtonRole.AcceptRole
        )
        btn_import = msg.addButton("📂 Import existing", QMessageBox.ButtonRole.ActionRole)
        msg.addButton(QMessageBox.StandardButton.Cancel)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked is btn_new:
            self._new_project_with_rules()
        elif clicked is btn_import:
            self._import_existing_project()
        # Cancel → do nothing

    def _import_existing_project(self) -> None:
        """Original add-project flow: select folder → map paths → save."""
        from pathlib import Path

        from PyQt6.QtWidgets import (
            QFileDialog,
        )

        dir_path = QFileDialog.getExistingDirectory(self, "Select Project Root Folder")
        if not dir_path:
            return

        p = Path(dir_path)
        name = p.name

        paths = self._run_map_paths_dialog(p)
        if paths is None:
            return

        self._save_and_open_project(name, p, paths, rules_content=None)

    def _new_project_with_rules(self) -> None:
        """New project flow: select folder → prompt → generate rules → preview/edit → map paths → save."""
        from pathlib import Path

        from PyQt6.QtWidgets import (
            QFileDialog,
            QMessageBox,
        )

        from .config import load_projects

        dir_path = QFileDialog.getExistingDirectory(self, "Select New Project Root Folder")
        if not dir_path:
            return

        p = Path(dir_path)
        name = p.name

        # Warn if same project name already exists from a different path
        data = load_projects()
        existing = (data.get("projects") or {}).get(name)
        if existing:
            existing_paths = list((existing.get("paths") or {}).values())
            if existing_paths:
                p_posix = p.resolve().as_posix()
                if not any(ep == p_posix or ep.startswith(p_posix + "/") for ep in existing_paths):
                    ans = QMessageBox.question(
                        self,
                        "Duplicate project name",
                        f"A project named '{name}' already exists (different folder).\n"
                        "Continuing will overwrite its configuration. Proceed?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    )
                    if ans != QMessageBox.StandardButton.Yes:
                        return

        # Step 1: prompt dialog
        prompt_text = self._ask_project_description(name)
        if prompt_text is None:
            return  # user cancelled

        # Step 2: generate rules in background
        rules_content = self._generate_rules_with_ui(prompt_text, name)
        if rules_content is None:
            return  # cancelled or failed

        # Step 3: allow re-generation if needed (loop)
        while True:
            result = self._show_rules_editor_dialog(rules_content, name, allow_regenerate=True)
            if result is None:
                return  # Cancel
            if isinstance(result, str):
                rules_content = result
                break  # Save
            # result is True → Regenerate: ask for new prompt and re-gen
            prompt_text = self._ask_project_description(name, prefill=prompt_text)
            if prompt_text is None:
                return
            rules_content = self._generate_rules_with_ui(prompt_text, name)
            if rules_content is None:
                return

        # Step 4: map paths
        paths = self._run_map_paths_dialog(p)
        if paths is None:
            return

        # Step 5: handle existing CLAUDE.md in target folder
        if (p / "CLAUDE.md").exists():
            ans = QMessageBox.question(
                self,
                "CLAUDE.md exists",
                f"'{name}/CLAUDE.md' already exists.\nReplace it with the generated rules?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
            )
            if ans == QMessageBox.StandardButton.Cancel:
                return
            if ans == QMessageBox.StandardButton.No:
                rules_content = None  # keep existing, skip write

        self._save_and_open_project(name, p, paths, rules_content=rules_content)

    def _ask_project_description(self, project_name: str, prefill: str = "") -> str | None:
        """Show a multiline prompt dialog. Returns the text or None on cancel."""
        from PyQt6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QLabel,
            QPlainTextEdit,
            QVBoxLayout,
        )

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Describe project: {project_name}")
        dlg.resize(500, 260)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("อธิบายระบบนี้ (stack, deploy, constraints, conventions):"))
        txt = QPlainTextEdit(dlg)
        txt.setPlaceholderText(
            "e.g. Next.js 14 frontend + FastAPI backend, deploy to Vercel + Fly.io, "
            "TypeScript strict, ห้ามใช้ any, test coverage ≥80%…"
        )
        if prefill:
            txt.setPlainText(prefill)
        lay.addWidget(txt)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Generate")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return txt.toPlainText().strip() or None

    def _generate_rules_with_ui(self, prompt: str, project_name: str) -> str | None:
        """Run the generator thread and show a busy dialog.  Returns markdown or None."""
        from PyQt6.QtWidgets import (
            QDialog,
            QLabel,
            QPushButton,
            QVBoxLayout,
        )

        busy = QDialog(self)
        busy.setWindowTitle("Generating project rules…")
        busy.setModal(True)
        busy.resize(360, 120)
        lay = QVBoxLayout(busy)
        lay.addWidget(
            QLabel(f"Running claude headless for '{project_name}'…\nThis may take up to 2 minutes.")
        )
        btn_cancel = QPushButton("Cancel")
        lay.addWidget(btn_cancel)

        thread = _RulesGeneratorThread(prompt, project_name, parent=self)
        result_holder: list[str | None] = [None]
        error_holder: list[str | None] = [None]

        def on_finished(content: str) -> None:
            result_holder[0] = content
            busy.accept()

        def on_failed(msg: str) -> None:
            error_holder[0] = msg
            busy.reject()

        def on_cancel() -> None:
            thread.cancel()
            thread.wait(3000)
            busy.reject()

        thread.rulesReady.connect(on_finished)
        thread.failed.connect(on_failed)
        btn_cancel.clicked.connect(on_cancel)

        # The 📁 add-project button was removed (its flow now lives under the
        # "+" new-tab menu); guard the enable/disable so the rules-gen path
        # doesn't AttributeError when the button no longer exists.
        _add_btn = getattr(self, "_btn_add_project", None)
        if _add_btn is not None:
            _add_btn.setEnabled(False)
        try:
            thread.start()
            busy.exec()
            thread.wait(5000)
            thread.deleteLater()
        finally:
            if _add_btn is not None:
                _add_btn.setEnabled(True)

        if result_holder[0] is not None:
            return result_holder[0]

        if error_holder[0]:
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.warning(self, "Generation failed", error_holder[0])
        return None

    def _run_map_paths_dialog(self, p: Path) -> dict | None:
        """Show the subdirectory → role-key mapping dialog.

        Returns a dict of {key: posix_path} on accept, or None on cancel.
        """
        from PyQt6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QFormLayout,
            QLabel,
            QLineEdit,
            QVBoxLayout,
        )

        from .config import load_projects

        name = p.name
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Configure Project Paths: {name}")
        dialog.resize(400, 300)
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel(
                "Map subdirectories to role keys (e.g., 'web', 'api').\nLeave blank to ignore a directory."
            )
        )

        form = QFormLayout()
        layout.addLayout(form)

        data = load_projects()
        existing_paths = {}
        existing_paths_rev: dict[str, str] = {}
        if "projects" in data and name in data["projects"]:
            existing_paths = data["projects"][name].get("paths", {})
            existing_paths_rev = {v: k for k, v in existing_paths.items()}

        inputs: dict[str, tuple[Path, QLineEdit]] = {}
        try:
            subs = sorted(p.iterdir(), key=lambda x: x.name)
        except PermissionError:
            subs = []
        for sub in subs:
            if sub.is_dir() and not sub.name.startswith("."):
                le = QLineEdit()
                le.setPlaceholderText("key (e.g. web, api)")
                sub_posix = str(sub.resolve().as_posix())
                if sub_posix in existing_paths_rev:
                    le.setText(existing_paths_rev[sub_posix])
                form.addRow(sub.name, le)
                inputs[sub.name] = (sub, le)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None

        paths: dict[str, str] = {}
        for _sub_name, (sub_path, le) in inputs.items():
            key = le.text().strip()
            if key:
                paths[key] = str(sub_path.resolve().as_posix())

        if not paths:
            paths["main"] = str(p.resolve().as_posix())

        return paths

    def _save_and_open_project(
        self,
        name: str,
        p: Path,
        paths: dict,
        rules_content: str | None,
        presets: list[str] | None = None,
    ) -> None:
        """Write CLAUDE.md (if rules_content given), save projects.json, open tab.

        *presets* overrides the stored preset list when provided; otherwise the
        existing list is preserved (import / edit flows that don't touch presets).
        """
        from .config import PROJECTS_JSON, load_projects

        if rules_content is not None:
            from .project_rules import write_project_rules

            write_project_rules(p, rules_content)

        data = load_projects()
        if "projects" not in data:
            data["projects"] = {}

        existing = (data.get("projects") or {}).get(name, {})
        data["projects"][name] = {
            "description": existing.get("description", name),
            "paths": paths,
            "presets": presets if presets is not None else existing.get("presets", []),
        }
        data["active"] = name

        PROJECTS_JSON.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(PROJECTS_JSON, data)

        self._refresh_project_list()
        if name in self._open_projects():
            for i in range(self.tabs.count()):
                if (
                    isinstance(self.tabs.widget(i), ProjectTab)
                    and self.tabs.widget(i).project_name == name
                ):
                    self.tabs.setCurrentIndex(i)
                    break
            self._status.showMessage(f"Updated project: {name}", 4_000)
        else:
            self._status.showMessage(f"Added project: {name} (opening tab...)", 4_000)
            self._open_project_tab(name)

    def _on_edit_project_rules_clicked(self, project_name: str | None = None) -> None:
        """Open the rules editor for the given project (defaults to active)."""
        from pathlib import Path

        from PyQt6.QtWidgets import QMessageBox

        from .config import active_project, lead_cwd, load_projects
        from .project_rules import read_project_rules, write_project_rules

        if project_name:
            data = load_projects()
            proj = (data.get("projects") or {}).get(project_name, {})
            name = project_name
        else:
            name, proj = active_project()

        if not name or not proj:
            QMessageBox.information(self, "No active project", "No project is currently active.")
            return

        # Resolve the project root via the SAME logic Lead uses to find its
        # cwd (lead path key → common parent of all paths → first path).
        # CLAUDE.md lives where Lead spawns — e.g. `app/`, not the `app-web/`
        # subfolder. The old `paths.get("main")` lookup never matched (keys
        # are web/api/mobile, never "main") and fell back to the first
        # subfolder, so the editor opened empty / saved to the wrong place.
        root_str = lead_cwd(project_name)
        if not root_str:
            QMessageBox.information(self, "No paths", f"Project '{name}' has no configured paths.")
            return

        project_root = Path(root_str)
        existing = read_project_rules(project_root)
        content = existing or ""

        while True:
            result = self._show_rules_editor_dialog(content, name, allow_regenerate=True)
            if result is None:
                return  # Cancel
            if result is True:
                # Regenerate: ask for description, then generate
                prompt_text = self._ask_project_description(name)
                if prompt_text is None:
                    return
                new_content = self._generate_rules_with_ui(prompt_text, name)
                if new_content is None:
                    return
                content = new_content
                continue
            # Save
            write_project_rules(project_root, result)
            self._status.showMessage(f"Saved project rules for '{name}'", 4_000)
            return

    def _show_rules_editor_dialog(
        self, content: str, project_name: str, allow_regenerate: bool = False
    ):
        """Editable rules dialog (used by both preview and edit flows).

        Returns str (save), True (regenerate), or None (cancel).
        """
        from PyQt6.QtWidgets import (
            QDialog,
            QHBoxLayout,
            QLabel,
            QPlainTextEdit,
            QPushButton,
            QVBoxLayout,
        )

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Project rules — {project_name}/CLAUDE.md")
        dlg.resize(680, 500)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"Edit {project_name}/CLAUDE.md:"))
        editor = QPlainTextEdit(dlg)
        editor.setPlainText(content)
        lay.addWidget(editor)

        btn_row = QHBoxLayout()
        btn_save = QPushButton("💾 Save")
        btn_cancel = QPushButton("Cancel")
        outcome: list = [None]

        if allow_regenerate:
            btn_regen = QPushButton("🔄 Regenerate from new prompt")
            btn_row.addWidget(btn_regen)

            def do_regen() -> None:
                outcome[0] = True
                dlg.accept()

            btn_regen.clicked.connect(do_regen)

        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_save)
        lay.addLayout(btn_row)

        def do_save() -> None:
            text = editor.toPlainText()
            if not text.strip():
                from PyQt6.QtWidgets import QMessageBox

                QMessageBox.warning(
                    dlg,
                    "Cannot save empty rules",
                    "The editor is empty. Add content or cancel to discard.",
                )
                return
            outcome[0] = text
            dlg.accept()

        btn_save.clicked.connect(do_save)
        btn_cancel.clicked.connect(dlg.reject)

        dlg.exec()
        return outcome[0]

    def _on_edit_project_clicked(self, proj_name: str) -> None:
        """Edit an existing project's description and paths in-place (no restart needed)."""
        from pathlib import Path

        from PyQt6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QFileDialog,
            QFormLayout,
            QLineEdit,
            QMessageBox,
            QVBoxLayout,
        )

        from .config import PROJECTS_JSON, load_projects

        data = load_projects()
        existing = (data.get("projects") or {}).get(proj_name, {})
        if not existing:
            QMessageBox.warning(self, "Project not found", f"Project '{proj_name}' not found.")
            return

        existing_paths: dict[str, str] = existing.get("paths", {})
        existing_desc: str = existing.get("description", proj_name)
        existing_presets: list = existing.get("presets", [])

        # Infer project root from configured paths
        non_main = {k: v for k, v in existing_paths.items() if k != "main"}
        if non_main:
            p = Path(next(iter(non_main.values()))).parent
        elif "main" in existing_paths:
            p = Path(existing_paths["main"])
        else:
            p = None

        if p is None or not p.exists():
            dir_path = QFileDialog.getExistingDirectory(
                self, f"Select root folder for '{proj_name}'"
            )
            if not dir_path:
                return
            p = Path(dir_path)

        # Step 1: description dialog
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Edit project: {proj_name}")
        dlg.resize(420, 130)
        lay = QVBoxLayout(dlg)
        form = QFormLayout()
        desc_edit = QLineEdit(existing_desc)
        form.addRow("Description:", desc_edit)
        lay.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        new_desc = desc_edit.text().strip() or existing_desc

        # Step 2: paths mapping dialog (pre-fills existing mapping automatically)
        paths = self._run_map_paths_dialog(p)
        if paths is None:
            return

        # Step 3: validate all configured paths exist on disk
        missing = [v for v in paths.values() if not Path(v).exists()]
        if missing:
            QMessageBox.warning(
                self,
                "Invalid paths",
                "These paths do not exist:\n" + "\n".join(missing),
            )
            return

        # Step 4: write atomically, preserving presets; reload without restart
        data = load_projects()
        if "projects" not in data:
            data["projects"] = {}
        data["projects"][proj_name] = {
            "description": new_desc,
            "paths": paths,
            "presets": existing_presets,
        }

        PROJECTS_JSON.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(PROJECTS_JSON, data)

        self._refresh_project_list()
        self._status.showMessage(f"Updated project '{proj_name}'", 4_000)
