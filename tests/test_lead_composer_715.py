"""#715: the Lead composer — the cockpit's own input bar under the Lead pane.

Covers the parts that decide what reaches the Lead PTY: the message format
(text + attachment paths), Enter/Shift+Enter, the paste+Enter write through
`inputBytes`, the terminal lock, keypad bytes, the question card's
validation, and the host that answers a question with picker keys.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import ClassVar

import pytest
from PyQt6.QtCore import QMimeData, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QKeyEvent
from PyQt6.QtWidgets import QApplication, QWidget

import agent_takkub.agent_pane as agent_pane_mod
from agent_takkub import lead_composer
from agent_takkub.agent_pane import AgentPane
from agent_takkub.lead_composer import LeadComposer, answers_valid, compose_message
from agent_takkub.roles import LEAD, by_name

from ._qt_timer_leak_guard import stop_timers_after


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeTerminalWidget(QWidget):
    inputBytes = pyqtSignal(bytes)
    resized = pyqtSignal(int, int)
    fontSizeChanged = pyqtSignal(int)
    openInEditorRequested = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.locked = False

    def set_input_locked(self, locked: bool) -> None:
        self.locked = bool(locked)

    def is_input_locked(self) -> bool:
        return self.locked

    def set_font_point_size(self, size: int) -> None:
        self.fontSizeChanged.emit(int(size))

    def __getattr__(self, name):  # every other TerminalWidget call is a no-op here
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


@pytest.fixture(autouse=True)
def _fake_terminal(monkeypatch):
    monkeypatch.setattr(agent_pane_mod, "TerminalWidget", _FakeTerminalWidget)


@pytest.fixture(autouse=True)
def _stop_pane_timers(monkeypatch):
    finalize = stop_timers_after(
        monkeypatch,
        AgentPane,
        "_tick_timer",
        "_token_timer",
        "_render_timer",
        "_done_clear_timer",
        "_idle_clear_timer",
    )
    yield
    finalize()


def _pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()


class TestComposeMessage:
    def test_text_only(self) -> None:
        assert compose_message("  สวัสดี  ", []) == "สวัสดี"

    def test_attachments_become_quoted_paths(self) -> None:
        msg = compose_message("ดูรูปนี้", ["C:/a/shot.png", "/tmp/spec.md"])
        assert msg.startswith("ดูรูปนี้\n\n")
        assert '- "C:/a/shot.png"' in msg and '- "/tmp/spec.md"' in msg

    def test_attachments_without_text(self) -> None:
        assert compose_message("", ["x.png"]).startswith("ไฟล์แนบ")

    def test_empty(self) -> None:
        assert compose_message("   ", []) == ""


class TestAnswersValid:
    Q: ClassVar[list[dict]] = [
        {"prompt": "a", "multiSelect": False, "options": [{"index": 0}, {"index": 1}]},
        {"prompt": "b", "multiSelect": True, "options": [{"index": 0}, {"index": 1}]},
    ]

    def test_valid(self) -> None:
        assert answers_valid(self.Q, [[1], [0, 1]])

    def test_single_select_needs_exactly_one(self) -> None:
        assert not answers_valid(self.Q, [[0, 1], [0]])

    def test_every_question_needs_an_answer(self) -> None:
        assert not answers_valid(self.Q, [[0], []])


class TestComposerWidget:
    def test_enter_submits_shift_enter_newlines(self, qapp, tmp_path) -> None:
        c = LeadComposer(tmp_path)
        sent: list[str] = []
        c.textSubmitted.connect(sent.append)
        c.editor.setPlainText("บรรทัดแรก")
        c.editor.moveCursor(c.editor.textCursor().MoveOperation.End)
        shift = QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier
        )
        c.editor.keyPressEvent(shift)
        assert sent == [] and c.editor.blockCount() == 2
        c.editor.keyPressEvent(
            QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        )
        assert sent == ["บรรทัดแรก"]
        assert c.editor.toPlainText() == ""

    def test_pasted_image_is_saved_and_attached(self, qapp, tmp_path) -> None:
        c = LeadComposer(tmp_path)
        mime = QMimeData()
        img = QImage(8, 8, QImage.Format.Format_RGB32)
        img.fill(QColor("red"))
        mime.setImageData(img)
        c.editor.insertFromMimeData(mime)
        (path,) = c.attachments()
        assert path.endswith(".png")
        assert "attachments" in path

    def test_dropped_files_are_attached_not_typed(self, qapp, tmp_path) -> None:
        f = tmp_path / "spec.md"
        f.write_text("x", encoding="utf-8")
        c = LeadComposer(tmp_path)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(f))])
        c.editor.insertFromMimeData(mime)
        assert c.attachments() == [str(f).replace("\\", "/")] or c.attachments() == [str(f)]
        assert c.editor.toPlainText() == ""

    def test_chip_remove(self, qapp, tmp_path) -> None:
        c = LeadComposer(tmp_path)
        c.add_attachments(["a.png", "b.pdf"])
        c.remove_attachment("a.png")
        assert c.attachments() == ["b.pdf"]

    def test_keypad_emits_terminal_bytes(self, qapp, tmp_path) -> None:
        c = LeadComposer(tmp_path)
        got: list[bytes] = []
        c.rawKeys.connect(got.append)
        keys = dict(lead_composer.KEYPAD_KEYS)
        for label in ("1", "↓", "Enter", "Esc"):
            btn = next(b for b in c._keypad.findChildren(type(c._btn_send)) if b.text() == label)
            btn.click()
        assert got == [keys["1"], b"\x1b[B", b"\r", b"\x1b"]

    def test_question_card_replaces_editor_and_validates(self, qapp, tmp_path) -> None:
        c = LeadComposer(tmp_path)
        c.show()
        answers: list = []
        c.questionAnswered.connect(answers.append)
        state = {
            "questions": [
                {
                    "prompt": "ทางไหน?",
                    "multiSelect": False,
                    "options": [{"index": 0, "label": "ก"}, {"index": 1, "label": "ข"}],
                }
            ]
        }
        c.set_question(state)
        assert c.question_visible() and not c._input_row.isVisible()
        c.card._on_submit()
        assert answers == [] and c.card._error.text()
        c.card._inputs[0][1].setChecked(True)
        c.card._on_submit()
        assert answers == [[[1]]]
        c.set_question(None)
        assert not c.question_visible() and c._input_row.isVisible()

    def test_same_question_keeps_the_selection(self, qapp, tmp_path) -> None:
        c = LeadComposer(tmp_path)
        c.show()
        state = {
            "questions": [
                {"prompt": "q", "multiSelect": True, "options": [{"index": 0, "label": "a"}]}
            ]
        }
        c.set_question(state)
        c.card._inputs[0][0].setChecked(True)
        c.set_question({"questions": [dict(state["questions"][0])]})
        assert c.card._inputs[0][0].isChecked()


class TestLeadPaneWiring:
    def test_only_lead_gets_a_composer_and_its_terminal_is_locked(self, qapp) -> None:
        lead = AgentPane(LEAD)
        backend = AgentPane(by_name("backend"))
        assert lead.composer is not None and lead._terminal.locked
        assert backend.composer is None

    def test_env_switch_turns_it_off(self, qapp, monkeypatch) -> None:
        monkeypatch.setenv("TAKKUB_LEAD_COMPOSER", "0")
        lead = AgentPane(LEAD)
        assert lead.composer is None and not lead._terminal.locked

    def test_submit_writes_paste_then_enter(self, qapp) -> None:
        lead = AgentPane(LEAD)
        lead.model.session = SimpleNamespace(is_alive=True)
        writes: list[bytes] = []
        lead.inputBytes.connect(lambda _role, data: writes.append(data))
        lead.composer.editor.setPlainText("บรรทัด 1\nบรรทัด 2")
        lead.composer.submit()
        assert writes and writes[0].startswith(b"\x1b[200~") and writes[0].endswith(b"\x1b[201~")
        _pump(qapp, 1.2)
        assert writes[-1] == b"\r"

    def test_submit_without_a_live_lead_shows_status(self, qapp) -> None:
        lead = AgentPane(LEAD)
        writes: list[bytes] = []
        lead.inputBytes.connect(lambda _role, data: writes.append(data))
        lead.composer.editor.setPlainText("hi")
        lead.composer.submit()
        assert writes == []
        assert "Lead ยังไม่ได้รัน" in lead.composer._status.text()
        assert lead.composer.editor.toPlainText() == "hi"  # the draft is kept

    def test_unlock_toggle_and_relock_on_send(self, qapp) -> None:
        lead = AgentPane(LEAD)
        lead.model.session = SimpleNamespace(is_alive=True)
        lead.composer.set_terminal_unlocked(True)
        assert not lead._terminal.locked
        lead.composer.editor.setPlainText("x")
        lead.composer.submit()
        assert lead._terminal.locked


class TestQuestionHost:
    def _host(self, qapp, state, *, answer_ok=True):
        from agent_takkub import lead_composer_host

        composer = SimpleNamespace(shown=[], statuses=[], set_question=None, show_status=None)
        composer.set_question = lambda s: composer.shown.append(s)
        composer.show_status = lambda t: composer.statuses.append(t)
        pane = SimpleNamespace(composer=composer, session=SimpleNamespace(is_alive=True))
        calls: list = []
        orch = SimpleNamespace(
            _project_panes=lambda _p: {"lead": pane},
            answer_picker=lambda keys, project=None: calls.append(keys) or (answer_ok, "x"),
        )
        notify = SimpleNamespace(
            current_ask_state=lambda _o, _p: state, lead_provider_name=lambda _o, _p: "claude"
        )
        from agent_takkub.remote import api

        mods = {"notify": notify, "api": api}
        host = lead_composer_host.LeadQuestionHost(orch, lambda: "p")
        host._timer.stop()
        return host, composer, calls, mods

    def test_answer_sends_picker_keys_and_hides_the_card(self, qapp, monkeypatch) -> None:
        from agent_takkub import lead_composer_host

        state = {
            "questions": [
                {"prompt": "q", "multiSelect": False, "options": [{"index": 0}, {"index": 1}]}
            ]
        }
        host, composer, calls, mods = self._host(qapp, state)
        monkeypatch.setattr(lead_composer_host, "_remote", lambda name: mods.get(name))
        host.answer("p", [[1]])
        assert calls == [["2"]]
        assert composer.shown[-1] is None
        # the transcript still shows the answered question for a moment —
        # a poll result in that window must not redraw it
        host._apply_state("p", state)
        assert composer.shown[-1] is None

    def test_stale_question_is_not_answered(self, qapp, monkeypatch) -> None:
        from agent_takkub import lead_composer_host

        host, composer, calls, mods = self._host(qapp, None)
        monkeypatch.setattr(lead_composer_host, "_remote", lambda name: mods.get(name))
        host.answer("p", [[0]])
        assert calls == []
        assert composer.statuses and "ตอบไปแล้ว" in composer.statuses[-1]
