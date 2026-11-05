"""ProjectWizardMixin — new (template) / import / clone project wizard (refactor round 3, step B).

Extracted from ``MainWindow`` as a mixin. Methods access ``self.*``
attributes (``_btn_add_project``, ``_status``, ``tabs``, ``orch``, etc.)
initialised in ``MainWindow.__init__``.

**Import constraint:** this module MUST NOT import ``app`` or ``cli``.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from .project_tab import ProjectTab

_GIT_URL_PREFIXES = ("http://", "https://", "git@", "ssh://", "git://", "file://")


def looks_like_git_url(text: str) -> bool:
    """Cheap heuristic used to prefill the clone dialog from the clipboard.

    Accepts the usual remote forms (``https://…``, ``git@host:owner/repo``,
    ``ssh://``, ``git://``, ``file://``) plus anything ending in ``.git``.
    Deliberately loose — it only decides whether to *prefill*, never whether
    to run ``git clone`` (git itself is the authority on that).
    """
    t = (text or "").strip()
    if not t or any(ch.isspace() for ch in t):
        return False
    return t.startswith(_GIT_URL_PREFIXES) or t.endswith(".git")


def repo_dir_name_from_url(url: str) -> str:
    """Folder name ``git clone <url>`` would create — last path segment minus ``.git``.

    Handles ``https://host/owner/repo(.git)``, ``git@host:owner/repo.git``,
    trailing slashes and Windows ``file://`` / local paths. Returns ``""``
    when nothing usable is left so the caller can keep the field empty.
    """
    u = (url or "").strip().rstrip("/").rstrip("\\")
    if not u:
        return ""
    # scp-like `git@host:owner/repo.git` → everything after the last ':' that
    # isn't a drive letter / scheme separator.
    tail = u.replace("\\", "/")
    for sep in ("/", ":"):
        if sep in tail:
            tail = tail.rsplit(sep, 1)[1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    return tail.strip()


class _GitCloneThread(QThread):
    """Background ``git clone --progress`` so the UI stays responsive.

    Signals
    -------
    progress(str) — one progress/status line from git (stderr merged)
    cloned(str)   — the destination path on success
    failed(str)   — error text on failure (incl. cancel); a partial clone
                    directory this thread created is removed first.
    """

    progress: pyqtSignal = pyqtSignal(str)
    cloned: pyqtSignal = pyqtSignal(str)
    failed: pyqtSignal = pyqtSignal(str)

    def __init__(self, url: str, dest: Path, parent=None) -> None:
        super().__init__(parent)
        self._url = url
        self._dest = dest
        self._proc = None
        self._cancelled = False

    def run(self) -> None:
        import os
        import shutil
        import subprocess

        from ._win_console import SUBPROCESS_NO_WINDOW

        git = shutil.which("git")
        if not git:
            self.failed.emit("ไม่พบ git ใน PATH — ติดตั้ง git ก่อนแล้วลองใหม่")
            return
        existed_before = self._dest.exists()
        env = dict(os.environ)
        # Never hang on an interactive credential prompt — fail fast with a
        # readable error instead (the user can set up a credential helper).
        env["GIT_TERMINAL_PROMPT"] = "0"
        tail: list[str] = []
        try:
            proc = subprocess.Popen(
                [git, "clone", "--progress", self._url, str(self._dest)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=SUBPROCESS_NO_WINDOW,
            )
            self._proc = proc
            buf = ""
            assert proc.stdout is not None
            # git reports progress with '\r' (same line rewrite), so split on
            # both '\r' and '\n' instead of readline().
            while True:
                chunk = proc.stdout.read(256)
                if not chunk:
                    break
                buf += chunk
                while True:
                    cut = min([i for i in (buf.find("\r"), buf.find("\n")) if i >= 0], default=-1)
                    if cut < 0:
                        break
                    line, buf = buf[:cut].strip(), buf[cut + 1 :]
                    if line:
                        tail = ([*tail, line])[-8:]
                        self.progress.emit(line)
            if buf.strip():
                tail = ([*tail, buf.strip()])[-8:]
                self.progress.emit(buf.strip())
            rc = proc.wait()
        except Exception as exc:
            rc = -1
            tail.append(str(exc))
        finally:
            self._proc = None
        if rc == 0 and not self._cancelled:
            self.cloned.emit(str(self._dest))
            return
        if not existed_before:
            shutil.rmtree(self._dest, ignore_errors=True)
        if self._cancelled:
            self.failed.emit("ยกเลิกแล้ว")
        else:
            self.failed.emit("\n".join(tail) or f"git clone exited with {rc}")

    def cancel(self) -> None:
        """Kill the git subprocess if it's still running."""
        self._cancelled = True
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass


class ProjectWizardMixin:
    """Mixin for new/import project wizard and project editing."""

    def _clone_project_from_url(self) -> None:
        """Clone flow: repo URL + destination → ``git clone`` → map paths → save/open.

        Lands in the same place as *Import โฟลเดอร์* once the clone is on
        disk, so path mapping / registry / tab open are shared code.
        """
        from .config import slugify_project_name

        target = self._ask_clone_target()
        if target is None:
            return
        url, dest = target

        if not self._run_git_clone_with_ui(url, dest):
            return

        name = slugify_project_name(dest.name)
        paths = self._run_map_paths_dialog(dest, project_name=name)
        if paths is None:
            return
        self._save_and_open_project(name, dest, paths, description=dest.name)

    def _default_clone_parent(self) -> Path:
        """Parent folder to suggest: next to the active project, else home."""
        from .config import load_projects

        try:
            data = load_projects()
            active = (data.get("projects") or {}).get(data.get("active") or "", {})
            for v in (active.get("paths") or {}).values():
                parent = Path(v).parent
                if parent.is_dir():
                    return parent
        except Exception:
            pass
        return Path.home()

    def _ask_clone_target(self) -> tuple[str, Path] | None:
        """Dialog: repo URL / parent folder / folder name.  Returns (url, dest) or None."""
        from PyQt6.QtWidgets import (
            QApplication,
            QDialogButtonBox,
            QFileDialog,
            QFormLayout,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QMessageBox,
            QPushButton,
            QVBoxLayout,
        )

        from . import cockpit_theme

        dialog = cockpit_theme.CockpitDialog(self)
        dialog.setWindowTitle("Clone โปรเจคจาก repo")
        dialog.resize(560, 220)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("กรอก URL ของ repo (https / ssh) แล้วเลือกโฟลเดอร์ที่จะ clone ลง"))
        form = QFormLayout()
        layout.addLayout(form)

        url_edit = QLineEdit()
        url_edit.setPlaceholderText(
            "https://github.com/owner/repo.git  หรือ  git@github.com:owner/repo.git"
        )
        try:
            clip = QApplication.clipboard().text()
        except Exception:
            clip = ""
        if looks_like_git_url(clip):
            url_edit.setText(clip.strip())
        form.addRow("Repo URL", url_edit)

        parent_edit = QLineEdit(str(self._default_clone_parent()))
        btn_browse = QPushButton("เลือก…")
        row = QHBoxLayout()
        row.addWidget(parent_edit, 1)
        row.addWidget(btn_browse)
        form.addRow("Clone ลงใน", row)

        name_edit = QLineEdit(repo_dir_name_from_url(url_edit.text()))
        name_edit.setPlaceholderText("ชื่อโฟลเดอร์ (เติมให้จาก URL)")
        form.addRow("ชื่อโฟลเดอร์", name_edit)

        name_touched = {"v": False}

        def _on_url_changed(text: str) -> None:
            if not name_touched["v"]:
                name_edit.blockSignals(True)
                name_edit.setText(repo_dir_name_from_url(text))
                name_edit.blockSignals(False)

        def _on_name_edited(_text: str) -> None:
            name_touched["v"] = True

        url_edit.textChanged.connect(_on_url_changed)
        name_edit.textEdited.connect(_on_name_edited)

        def _browse() -> None:
            d = QFileDialog.getExistingDirectory(dialog, "เลือกโฟลเดอร์ปลายทาง", parent_edit.text())
            if d:
                parent_edit.setText(d)

        btn_browse.clicked.connect(_browse)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Clone")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def _validate() -> None:
            url = url_edit.text().strip()
            parent = Path(parent_edit.text().strip() or "")
            folder = name_edit.text().strip()
            if not url:
                QMessageBox.warning(dialog, "ขาด URL", "กรอก URL ของ repo ก่อน")
                return
            if not parent_edit.text().strip() or not parent.is_dir():
                QMessageBox.warning(dialog, "โฟลเดอร์ไม่ถูกต้อง", "โฟลเดอร์ปลายทางต้องมีอยู่จริง")
                return
            if not folder or folder in (".", "..") or "/" in folder or "\\" in folder:
                QMessageBox.warning(dialog, "ชื่อโฟลเดอร์ไม่ถูกต้อง", "ใส่ชื่อโฟลเดอร์ที่ไม่มี / หรือ \\")
                return
            if (parent / folder).exists():
                QMessageBox.warning(
                    dialog,
                    "โฟลเดอร์มีอยู่แล้ว",
                    f"'{parent / folder}' มีอยู่แล้ว — ใช้ Import โฟลเดอร์ หรือเปลี่ยนชื่อ",
                )
                return
            dialog.accept()

        buttons.accepted.connect(_validate)

        if dialog.exec() != cockpit_theme.CockpitDialog.DialogCode.Accepted:
            return None
        return url_edit.text().strip(), Path(parent_edit.text().strip()) / name_edit.text().strip()

    def _run_git_clone_with_ui(self, url: str, dest: Path) -> bool:
        """Run ``git clone`` on a worker thread behind a busy dialog.  True on success."""
        from PyQt6.QtWidgets import QDialog, QLabel, QMessageBox, QPushButton, QVBoxLayout

        busy = QDialog(self)
        busy.setWindowTitle("กำลัง clone…")
        busy.setModal(True)
        busy.resize(480, 140)
        lay = QVBoxLayout(busy)
        lay.addWidget(QLabel(f"git clone {url}\n→ {dest}"))
        prog = QLabel("เริ่ม clone…")
        prog.setWordWrap(True)
        lay.addWidget(prog)
        btn_cancel = QPushButton("ยกเลิก")
        lay.addWidget(btn_cancel)

        thread = _GitCloneThread(url, dest, parent=self)
        ok_holder: list[bool] = [False]
        error_holder: list[str | None] = [None]

        def on_cloned(_path: str) -> None:
            ok_holder[0] = True
            busy.accept()

        def on_failed(msg: str) -> None:
            error_holder[0] = msg
            busy.reject()

        def on_cancel() -> None:
            thread.cancel()
            thread.wait(3000)
            busy.reject()

        thread.progress.connect(prog.setText)
        thread.cloned.connect(on_cloned)
        thread.failed.connect(on_failed)
        btn_cancel.clicked.connect(on_cancel)

        thread.start()
        busy.exec()
        # Same hazard as the rules generator: Esc / window-close bypasses
        # on_cancel, so always cancel + join before deleteLater().
        if thread.isRunning():
            thread.cancel()
            if not thread.wait(10000):
                thread.terminate()
                thread.wait()
        thread.deleteLater()

        if ok_holder[0]:
            self._status.showMessage(f"Cloned: {dest.name}", 4_000)
            return True
        if error_holder[0] and error_holder[0] != "ยกเลิกแล้ว":
            QMessageBox.warning(self, "Clone ไม่สำเร็จ", error_holder[0])
        return False

    def _import_existing_project(self) -> None:
        """Original add-project flow: select folder → map paths → save."""
        from pathlib import Path

        from PyQt6.QtWidgets import (
            QFileDialog,
        )

        from .config import slugify_project_name

        dir_path = QFileDialog.getExistingDirectory(self, "Select Project Root Folder")
        if not dir_path:
            return

        p = Path(dir_path)
        name = slugify_project_name(p.name)

        paths = self._run_map_paths_dialog(p, project_name=name)
        if paths is None:
            return

        self._save_and_open_project(name, p, paths, description=p.name)

    def _new_project_from_template(self) -> None:
        """New project flow: template picker → write scaffold → register → open.

        Replaced the AI-rules flow (claude headless writing CLAUDE.md), which
        tied project setup to one provider. Every choice is a click and the
        files come from `project_template` — provider-neutral `AGENTS.md`
        plus a `CLAUDE.md` that imports it. Paths are known from the picked
        parts, so there is no map-paths step.
        """
        from PyQt6.QtWidgets import QMessageBox

        from . import project_template
        from .config import load_projects, slugify_project_name
        from .new_project_dialog import NewProjectDialog

        dlg = NewProjectDialog(self, default_parent=self._default_clone_parent())
        if dlg.exec() != NewProjectDialog.DialogCode.Accepted:
            return
        picked = dlg.result_value()
        if picked is None:
            return
        display_name, target, sel = picked
        name = slugify_project_name(display_name)

        existing = ((load_projects().get("projects") or {}).get(name)) or {}
        existing_paths = list((existing.get("paths") or {}).values())
        if existing_paths:
            t_posix = target.resolve().as_posix()
            if not any(ep == t_posix or ep.startswith(t_posix + "/") for ep in existing_paths):
                ans = QMessageBox.question(
                    self,
                    "ชื่อโปรเจคซ้ำ",
                    f"มีโปรเจคชื่อ '{name}' อยู่แล้ว (คนละ folder) ทำต่อจะเขียนทับค่าของโปรเจคเดิม ทำต่อไหม?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                )
                if ans != QMessageBox.StandardButton.Yes:
                    return

        try:
            result = project_template.write_scaffold(target, display_name, sel)
        except OSError as exc:
            QMessageBox.warning(self, "สร้างโปรเจคไม่สำเร็จ", f"เขียนไฟล์ใน '{target}' ไม่ได้: {exc}")
            return
        paths = project_template.project_paths(target, sel)
        self._save_and_open_project(name, target, paths, description=display_name)
        kept = f" · ข้าม {len(result.kept)} ไฟล์ที่มีอยู่แล้ว" if result.kept else ""
        self._status.showMessage(
            f"สร้างโปรเจค {display_name}: {len(result.created)} ไฟล์{kept}", 6_000
        )

    def _run_map_paths_dialog(self, p: Path, project_name: str | None = None) -> dict | None:
        """Show the subdirectory → role-key mapping dialog.

        *project_name* is the registry key to pre-fill existing path
        mappings from — pass the caller's already-resolved (slugified)
        project name, since it may differ from the folder's raw ``p.name``
        (#467). Defaults to ``p.name`` for callers that don't have one.

        Returns a dict of {key: posix_path} on accept, or None on cancel.
        """
        from PyQt6.QtWidgets import (
            QDialogButtonBox,
            QFormLayout,
            QLabel,
            QLineEdit,
            QVBoxLayout,
        )

        from . import cockpit_theme
        from .config import load_projects

        name = project_name if project_name is not None else p.name
        # 2026-09-08 design review: was a plain QDialog — never picked up the
        # app's theme (see cockpit_theme.CockpitDialog's own docstring), the
        # root cause of the "unstyled Windows dialog" finding for Map Paths.
        dialog = cockpit_theme.CockpitDialog(self)
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

        if not inputs:
            return {"main": str(p.resolve().as_posix())}

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != cockpit_theme.CockpitDialog.DialogCode.Accepted:
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
        presets: list[str] | None = None,
        description: str | None = None,
    ) -> None:
        """Save projects.json and open (or focus) the project tab.

        *presets* overrides the stored preset list when provided; otherwise the
        existing list is preserved (import / edit flows that don't touch presets).
        *description* seeds the description for a brand-new project (e.g. the
        folder's raw display name when *name* was slugified from it, #467);
        an existing project's stored description always takes precedence.
        """
        from .config import load_projects, save_projects_json

        data = load_projects()
        if "projects" not in data:
            data["projects"] = {}

        existing = (data.get("projects") or {}).get(name, {})
        data["projects"][name] = {
            "description": existing.get("description", description or name),
            "paths": paths,
            "presets": presets if presets is not None else existing.get("presets", []),
        }
        data["active"] = name

        save_projects_json(data)

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
        """Open the rules editor for the given project (defaults to active).

        Edits the project's own rules file — `AGENTS.md` (every provider)
        or a legacy `CLAUDE.md` (see `project_rules.rules_path`)."""
        from pathlib import Path

        from PyQt6.QtWidgets import QMessageBox

        from .config import active_project, lead_cwd, load_projects
        from .project_rules import read_project_rules, rules_path, write_project_rules

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
        # The rules file lives where Lead spawns — e.g. `app/`, not the
        # `app-web/` subfolder.
        root_str = lead_cwd(project_name)
        if not root_str:
            QMessageBox.information(self, "No paths", f"Project '{name}' has no configured paths.")
            return

        project_root = Path(root_str)
        target = rules_path(project_root)
        result = self._show_rules_editor_dialog(
            read_project_rules(project_root) or "", name, target.name
        )
        if result is None:
            return
        write_project_rules(project_root, result)
        self._status.showMessage(f"Saved project rules for '{name}' ({target.name})", 4_000)

    def _show_rules_editor_dialog(self, content: str, project_name: str, file_name: str):
        """Editable rules dialog. Returns the text to save, or None (cancel)."""
        from PyQt6.QtWidgets import (
            QHBoxLayout,
            QLabel,
            QPlainTextEdit,
            QVBoxLayout,
        )

        from . import cockpit_theme

        # 2026-09-08 design review: was a plain QDialog — never picked up the
        # app's theme (see cockpit_theme.CockpitDialog's own docstring), the
        # root cause of the "unstyled light dialog with dark text area"
        # finding for the Rules Editor.
        dlg = cockpit_theme.CockpitDialog(self)
        dlg.setWindowTitle(f"Project rules — {project_name}/{file_name}")
        dlg.resize(680, 500)
        lay = QVBoxLayout(dlg)
        hint = " (claude, codex, agy, opencode, cursor อ่านไฟล์นี้)" if file_name == "AGENTS.md" else ""
        lay.addWidget(QLabel(f"Edit {project_name}/{file_name}{hint}:"))
        editor = QPlainTextEdit(dlg)
        editor.setPlainText(content)
        lay.addWidget(editor)

        btn_row = QHBoxLayout()
        btn_save = cockpit_theme.gold_button("💾 Save", dlg)
        btn_cancel = cockpit_theme.secondary_button("Cancel", dlg)
        outcome: list = [None]

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

        from .config import load_projects, save_projects_json

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
        paths = self._run_map_paths_dialog(p, project_name=proj_name)
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

        save_projects_json(data)

        self._refresh_project_list()
        self._status.showMessage(f"Updated project '{proj_name}'", 4_000)
