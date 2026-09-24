"""Lead composer (#715): the cockpit's own input bar under the Lead pane.

Typing straight into a provider's TUI inherits every limit of that TUI —
no real text editing, no file picker, no drag-and-drop, and a different
set of quirks per CLI. The composer replaces it: the Lead terminal is
locked in input mode, with an explicit switch for direct CLI typing.

- multi-line editor: Enter sends, Shift+Enter adds a line
- attachments: 📎 file picker, drag-and-drop, or Ctrl+V an image — shown as
  removable chips; files are referenced by path, pasted images are saved to
  `RUNTIME_DIR/attachments/<date>/`. Every provider gets the same plain-path
  block (the same contract the Remote upload already relies on), so no
  provider-specific upload protocol is needed.
- question card: when the Lead is sitting at a multiple-choice question the
  bar turns into radio/checkbox choices; answering sends the picker keys.
- keyboard mode switch: input mode keeps the terminal locked and CLI mode
  unlocks it for direct typing.

This module is UI only. It emits signals; `AgentPane` turns text into PTY
writes and `MainWindow` supplies/answers question state.
"""

from __future__ import annotations

import pathlib
import time
import uuid
from datetime import datetime

from PyQt6.QtCore import QMimeData, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QKeyEvent
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_ATTACHMENT_KEEP_DAYS = 14
_EDITOR_MIN_LINES = 2
_EDITOR_MAX_LINES = 8
_INPUT_PLACEHOLDER = "พิมพ์ถึง Lead…  (Enter ส่ง · Shift+Enter ขึ้นบรรทัด · ลากไฟล์/Ctrl+V รูป มาวางได้)"
_CLI_PLACEHOLDER = "โหมด CLI — พิมพ์ใน terminal ได้เลย · กด ⌨ เพื่อกลับ"
_INPUT_MODE_TOOLTIP = "โหมด input — terminal ล็อกอยู่ · กดเพื่อสลับไปพิมพ์ใน terminal"
_CLI_MODE_TOOLTIP = "โหมด CLI — พิมพ์ใน terminal ได้ · กดเพื่อกลับโหมด input"


def attachments_dir(runtime_dir: pathlib.Path, *, now: datetime | None = None) -> pathlib.Path:
    return runtime_dir / "attachments" / (now or datetime.now()).strftime("%Y-%m-%d")


def prune_attachments(runtime_dir: pathlib.Path, *, keep_days: int = _ATTACHMENT_KEEP_DAYS) -> int:
    """Drop dated attachment folders older than *keep_days*. Returns count."""
    root = runtime_dir / "attachments"
    cutoff = time.time() - keep_days * 86400
    removed = 0
    try:
        folders = list(root.iterdir())
    except OSError:
        return 0
    for folder in folders:
        try:
            day = datetime.strptime(folder.name, "%Y-%m-%d").timestamp()
        except ValueError:
            continue
        if day >= cutoff:
            continue
        for f in folder.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        try:
            folder.rmdir()
            removed += 1
        except OSError:
            pass
    return removed


def save_pasted_image(image: QImage, runtime_dir: pathlib.Path) -> pathlib.Path:
    folder = attachments_dir(runtime_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"paste-{uuid.uuid4().hex[:12]}.png"
    if not image.save(str(path), "PNG"):
        raise OSError(f"could not save pasted image to {path}")
    return path


def compose_message(text: str, attachments: list[str]) -> str:
    """The text written into the Lead: the owner's words, then one quoted
    absolute path per attachment. Plain paths work for every provider —
    each CLI's own file/image tools open them (same contract as the Remote
    image upload)."""
    body = (text or "").strip()
    if not attachments:
        return body
    lines = ["ไฟล์แนบ (เปิดอ่านจาก path นี้):"]
    lines += [f'- "{p}"' for p in attachments]
    block = "\n".join(lines)
    return f"{body}\n\n{block}" if body else block


def answers_valid(questions: list[dict], answers: list[list[int]]) -> bool:
    if len(answers) != len(questions):
        return False
    for q, chosen in zip(questions, answers, strict=True):
        if not chosen:
            return False
        if not q.get("multiSelect") and len(chosen) != 1:
            return False
    return True


class ComposerEdit(QPlainTextEdit):
    """Editor with chat keys and attachment-aware paste/drop."""

    submitRequested = pyqtSignal()
    filesDropped = pyqtSignal(list)  # list[str] local paths
    imagePasted = pyqtSignal(QImage)

    def keyPressEvent(self, e: QKeyEvent | None) -> None:
        if e is not None and e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                # Qt's own Shift+Enter inserts U+2028 (a soft line break),
                # which would reach the Lead as a stray character.
                self.insertPlainText("\n")
            else:
                self.submitRequested.emit()
            return
        super().keyPressEvent(e)

    def canInsertFromMimeData(self, source: QMimeData | None) -> bool:
        if source is not None and (source.hasImage() or source.hasUrls()):
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData | None) -> None:
        if source is None:
            return
        if source.hasUrls():
            paths = [u.toLocalFile() for u in source.urls() if u.isLocalFile()]
            paths = [p for p in paths if p]
            if paths:
                self.filesDropped.emit(paths)
                return
        if source.hasImage():
            img = source.imageData()
            if isinstance(img, QImage) and not img.isNull():
                self.imagePasted.emit(img)
                return
        super().insertFromMimeData(source)


class _Chip(QFrame):
    removeRequested = pyqtSignal(str)

    def __init__(self, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self.setObjectName("composerChip")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 1, 2, 1)
        lay.setSpacing(4)
        name = pathlib.Path(path).name
        icon = "🖼" if pathlib.Path(path).suffix.lower() in IMAGE_SUFFIXES else "📄"
        label = QLabel(f"{icon} {name}")
        label.setToolTip(path)
        lay.addWidget(label)
        btn = QToolButton()
        btn.setText("✕")
        btn.setAutoRaise(True)
        btn.setToolTip("เอาไฟล์นี้ออก")
        btn.clicked.connect(lambda: self.removeRequested.emit(self.path))
        lay.addWidget(btn)


class QuestionCard(QFrame):
    """Radio/checkbox choices for a pending multiple-choice question."""

    answered = pyqtSignal(list)  # list[list[int]] per question
    answerInTerminal = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("questionCard")
        self._questions: list[dict] = []
        self._inputs: list[list[QRadioButton | QCheckBox]] = []
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(10, 8, 10, 8)
        self._root.setSpacing(6)
        self._body = QVBoxLayout()
        self._root.addLayout(self._body)
        foot = QHBoxLayout()
        self._error = QLabel("")
        self._error.setStyleSheet(f"color:{cockpit_theme.ERROR_CHIP_TEXT};")
        foot.addWidget(self._error, 1)
        in_term = QPushButton("ตอบใน terminal แทน")
        in_term.setObjectName("composerKey")
        in_term.setToolTip("ปลด lock terminal ชั่วคราว (เช่นต้องการพิมพ์คำตอบเอง)")
        in_term.clicked.connect(self.answerInTerminal)
        foot.addWidget(in_term)
        self._submit = QPushButton("ส่งคำตอบ")
        self._submit.setObjectName("composerPrimary")
        self._submit.clicked.connect(self._on_submit)
        foot.addWidget(self._submit)
        self._root.addLayout(foot)

    def questions(self) -> list[dict]:
        return self._questions

    def set_questions(self, questions: list[dict]) -> None:
        while self._body.count():
            item = self._body.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()
        self._questions = list(questions or [])
        self._inputs = []
        self._error.setText("")
        total = len(self._questions)
        for n, q in enumerate(self._questions, 1):
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 4)
            lay.setSpacing(2)
            multi = bool(q.get("multiSelect"))
            head = f"❓ Lead ถาม ({n}/{total})" if total > 1 else "❓ Lead ถาม"
            head += " · เลือกได้หลายข้อ" if multi else ""
            title = QLabel(head)
            title.setStyleSheet(f"color:{cockpit_theme.GOLD_CHIP_TEXT}; font-weight:600;")
            lay.addWidget(title)
            prompt = QLabel(str(q.get("prompt") or ""))
            prompt.setWordWrap(True)
            lay.addWidget(prompt)
            group = QButtonGroup(box)
            group.setExclusive(not multi)
            row: list[QRadioButton | QCheckBox] = []
            for opt in q.get("options") or []:
                text = f"{int(opt.get('index', 0)) + 1}. {opt.get('label', '')}"
                btn: QRadioButton | QCheckBox = QCheckBox(text) if multi else QRadioButton(text)
                group.addButton(btn)
                lay.addWidget(btn)
                row.append(btn)
            self._inputs.append(row)
            self._body.addWidget(box)

    def selected(self) -> list[list[int]]:
        out: list[list[int]] = []
        for q, row in zip(self._questions, self._inputs, strict=False):
            opts = q.get("options") or []
            out.append([int(opts[i].get("index", i)) for i, b in enumerate(row) if b.isChecked()])
        return out

    def _on_submit(self) -> None:
        answers = self.selected()
        if not answers_valid(self._questions, answers):
            self._error.setText("เลือกคำตอบให้ครบทุกข้อก่อน")
            return
        self._error.setText("")
        self.answered.emit(answers)


class LeadComposer(QFrame):
    """The input bar. See module docstring."""

    textSubmitted = pyqtSignal(str)
    terminalUnlockChanged = pyqtSignal(bool)
    questionAnswered = pyqtSignal(list)

    def __init__(self, runtime_dir: pathlib.Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runtime_dir = runtime_dir
        self._attachments: list[str] = []
        self.setObjectName("leadComposer")
        self.setStyleSheet(self._stylesheet())
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(4)

        self.card = QuestionCard(self)
        self.card.setVisible(False)
        self.card.answered.connect(self.questionAnswered)
        self.card.answerInTerminal.connect(lambda: self.set_terminal_unlocked(True))
        root.addWidget(self.card)

        self._chips_row = QWidget(self)
        self._chips = QHBoxLayout(self._chips_row)
        self._chips.setContentsMargins(0, 0, 0, 0)
        self._chips.setSpacing(4)
        self._chips.addStretch(1)
        self._chips_row.setVisible(False)
        root.addWidget(self._chips_row)

        self._input_row = QWidget(self)
        row = QHBoxLayout(self._input_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.editor = ComposerEdit(self)
        self.editor.setObjectName("composerEdit")
        self.editor.setPlaceholderText(_INPUT_PLACEHOLDER)
        self.editor.setTabChangesFocus(True)
        self.editor.submitRequested.connect(self.submit)
        self.editor.filesDropped.connect(self.add_attachments)
        self.editor.imagePasted.connect(self._on_image_pasted)
        self.editor.textChanged.connect(self._fit_editor)
        row.addWidget(self.editor, 1)
        self._btn_attach = QToolButton()
        self._btn_attach.setText("📎")
        self._btn_attach.setToolTip("แนบไฟล์/รูป")
        self._btn_attach.clicked.connect(self._on_browse)
        row.addWidget(self._btn_attach, 0, Qt.AlignmentFlag.AlignBottom)
        self._btn_keys = QToolButton()
        self._btn_keys.setText("⌨")
        self._btn_keys.setObjectName("composerModeToggle")
        self._btn_keys.setCheckable(True)
        self._btn_keys.setToolTip(_INPUT_MODE_TOOLTIP)
        self._btn_keys.toggled.connect(self._on_terminal_mode_toggled)
        row.addWidget(self._btn_keys, 0, Qt.AlignmentFlag.AlignBottom)
        self._btn_send = QPushButton("ส่ง ➤")
        self._btn_send.setObjectName("composerPrimary")
        self._btn_send.clicked.connect(self.submit)
        row.addWidget(self._btn_send, 0, Qt.AlignmentFlag.AlignBottom)
        root.addWidget(self._input_row)

        self._status = QLabel("")
        self._status.setObjectName("composerStatus")
        self._status.setVisible(False)
        root.addWidget(self._status)
        self._fit_editor()

    # ── public API ──────────────────────────────────────────────────────
    def attachments(self) -> list[str]:
        return list(self._attachments)

    def add_attachments(self, paths: list[str]) -> None:
        for p in paths:
            p = str(p)
            if p and p not in self._attachments:
                self._attachments.append(p)
        self._render_chips()

    def remove_attachment(self, path: str) -> None:
        if path in self._attachments:
            self._attachments.remove(path)
        self._render_chips()

    def set_question(self, state: dict | None) -> None:
        """Show the question card for *state* (`{"questions": [...]}`), or
        return to the editor when None. Unchanged questions keep the
        owner's in-progress selection."""
        questions = (state or {}).get("questions") or []
        if not questions:
            if self.card.isVisible():
                self.card.setVisible(False)
                self._input_row.setVisible(True)
                self._chips_row.setVisible(bool(self._attachments))
            return
        if not self.card.isVisible() or self.card.questions() != questions:
            self.card.set_questions(questions)
        self.card.setVisible(True)
        self._input_row.setVisible(False)
        self._chips_row.setVisible(False)

    def question_visible(self) -> bool:
        return self.card.isVisible()

    def set_terminal_unlocked(self, unlocked: bool) -> None:
        self._btn_keys.setChecked(bool(unlocked))

    def show_status(self, text: str) -> None:
        self._status.setText(text)
        self._status.setVisible(bool(text))

    def set_send_guard(self, guard) -> None:
        """*guard()* returns None when a message can be delivered now, else
        the reason to show — the draft and attachments are then kept."""
        self._send_guard = guard

    def submit(self) -> None:
        text = self.editor.toPlainText()
        message = compose_message(text, self._attachments)
        if not message:
            return
        guard = getattr(self, "_send_guard", None)
        reason = guard() if guard is not None else None
        if reason:
            self.show_status(reason)
            return
        self.textSubmitted.emit(message)
        self.editor.clear()
        self._attachments = []
        self._render_chips()
        self.show_status("")
        if self._btn_keys.isChecked():
            self._btn_keys.setChecked(False)

    # ── internals ───────────────────────────────────────────────────────
    def _on_browse(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "แนบไฟล์ให้ Lead")
        if paths:
            self.add_attachments(paths)

    def _on_image_pasted(self, image: QImage) -> None:
        try:
            path = save_pasted_image(image, self._runtime_dir)
        except OSError as exc:
            self.show_status(f"บันทึกรูปที่วางไม่ได้: {exc}")
            return
        self.add_attachments([str(path)])

    def _render_chips(self) -> None:
        while self._chips.count() > 1:
            item = self._chips.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()
        for p in self._attachments:
            chip = _Chip(p, self._chips_row)
            chip.removeRequested.connect(self.remove_attachment)
            self._chips.insertWidget(self._chips.count() - 1, chip)
        self._chips_row.setVisible(bool(self._attachments) and not self.card.isVisible())

    def _on_terminal_mode_toggled(self, on: bool) -> None:
        input_enabled = not on
        self.editor.setEnabled(input_enabled)
        self._btn_attach.setEnabled(input_enabled)
        self._btn_send.setEnabled(input_enabled)
        self.editor.setPlaceholderText(_CLI_PLACEHOLDER if on else _INPUT_PLACEHOLDER)
        self._btn_keys.setToolTip(_CLI_MODE_TOOLTIP if on else _INPUT_MODE_TOOLTIP)
        self.terminalUnlockChanged.emit(on)

    def _fit_editor(self) -> None:
        doc_lines = max(_EDITOR_MIN_LINES, min(_EDITOR_MAX_LINES, self.editor.blockCount()))
        fm = self.editor.fontMetrics()
        self.editor.setFixedHeight(int(fm.lineSpacing() * doc_lines + 14))

    @staticmethod
    def _stylesheet() -> str:
        t = cockpit_theme
        return f"""
            QFrame#leadComposer {{
                background: {t.GROUND_PANEL};
                border-top: 1px solid {t.BORDER_CARD};
            }}
            QPlainTextEdit#composerEdit {{
                background: {t.GROUND_INPUT};
                color: {t.TEXT_PRIMARY};
                border: 1px solid {t.BORDER_CONTROL};
                border-radius: {t.RADIUS_SM}px;
                padding: 4px 6px;
            }}
            QPlainTextEdit#composerEdit:focus {{ border-color: {t.ACCENT_GOLD}; }}
            QPushButton#composerPrimary {{
                background: {t.ACCENT_GOLD};
                color: {t.GOLD_TEXT_ON};
                border: none;
                border-radius: {t.RADIUS_SM}px;
                padding: 6px 14px;
                font-weight: 600;
            }}
            QPushButton#composerPrimary:hover {{ background: {t.GOLD_GRAD_HOVER_TOP}; }}
            QPushButton#composerPrimary:disabled {{
                background: {t.NEUTRAL_CHIP_BG};
                color: {t.TEXT_FAINT};
                border: 1px solid {t.BORDER_CARD};
            }}
            QPushButton#composerKey {{
                background: {t.NEUTRAL_CHIP_BG};
                color: {t.TEXT_SECONDARY};
                border: 1px solid {t.BORDER_CARD};
                border-radius: 6px;
                padding: 3px 8px;
                min-width: 22px;
            }}
            QPushButton#composerKey:hover {{ color: {t.TEXT_PRIMARY}; }}
            QPushButton#composerKey:checked {{
                background: {t.GOLD_CHIP_BG};
                color: {t.GOLD_CHIP_TEXT};
                border-color: {t.GOLD_CHIP_BORDER};
            }}
            QFrame#composerChip {{
                background: {t.GOLD_CHIP_BG};
                border: 1px solid {t.GOLD_CHIP_BORDER};
                border-radius: 10px;
                color: {t.GOLD_CHIP_TEXT};
            }}
            QFrame#questionCard {{
                background: {t.GROUND_PANEL_ALT};
                border: 1px solid {t.GOLD_CHIP_BORDER};
                border-radius: {t.RADIUS_MD}px;
            }}
            QFrame#questionCard QLabel,
            QFrame#questionCard QRadioButton,
            QFrame#questionCard QCheckBox {{
                color: {t.TEXT_PRIMARY};
                background: transparent;
            }}
            QFrame#questionCard QRadioButton,
            QFrame#questionCard QCheckBox {{ padding: 2px 0; }}
            QLabel#composerStatus {{ color: {t.ERROR_CHIP_TEXT}; }}
            QToolButton {{
                color: {t.TEXT_SECONDARY};
                background: transparent;
                border: 1px solid {t.BORDER_CARD};
                border-radius: {t.RADIUS_SM}px;
                font-size: 15px;
                padding: 4px 6px;
            }}
            QToolButton:hover {{ background: {t.HOVER_WEAK}; color: {t.TEXT_PRIMARY}; }}
            QToolButton#composerModeToggle:checked {{
                background: {t.ACCENT_GOLD};
                color: {t.GOLD_TEXT_ON};
                border-color: {t.ACCENT_GOLD};
            }}
            QFrame#composerChip QToolButton {{ border: none; padding: 0 2px; font-size: 11px; }}
            QFrame#composerChip QLabel {{ color: {t.GOLD_CHIP_TEXT}; background: transparent; }}
        """
