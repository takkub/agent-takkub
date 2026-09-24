"""Question-card plumbing for the Lead composer (#715).

The composer (`lead_composer.py`) is UI only. This host watches the active
project's Lead for a pending multiple-choice question and answers it:

- detection and answering reuse the Remote's proven code —
  `remote.notify.current_ask_state` (reads the provider transcript) and
  `remote.api._build_picker_key_sequence` + `Orchestrator.answer_picker`
  (paced key presses, #660). `remote/` is a delete-to-uninstall bolt-on
  (import-linter `remote-bolt-on-isolation`), so it is reached through
  `importlib` exactly like `MainWindow._boot`: with the folder gone the card
  simply never appears and the composer's keypad still answers any menu.
- the transcript read runs on a plain worker thread (never a QThread
  parented to a widget, #688) and the result comes back through a queued
  signal; one read in flight at a time.
- only providers whose history scanner reports questions are polled
  (`_ASK_PROVIDERS`) — resolving a transcript is not free for every CLI
  (#701: agy scans hundreds of databases).
"""

from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Callable

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .orchestrator_text import _log_event

# Providers whose `remote.notify` scanner implements `live_ask` today.
_ASK_PROVIDERS = frozenset({"claude"})
_POLL_MS = 1500
# After answering, the transcript still shows the question until the CLI
# writes its tool result — don't redraw the same card in that window.
_ANSWERED_HIDE_S = 8.0


def _remote(name: str):
    try:
        return importlib.import_module(f"agent_takkub.remote.{name}")
    except Exception:
        return None


class LeadQuestionHost(QObject):
    _stateReady = pyqtSignal(str, object)  # project, state dict | None

    def __init__(self, orch, active_project: Callable[[], str | None], parent=None) -> None:
        super().__init__(parent)
        self._orch = orch
        self._active_project = active_project
        self._busy = False
        self._answered: dict[str, tuple[list, float]] = {}
        self._stateReady.connect(self._apply_state)
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def wire(self, pane, project: str) -> None:
        """Connect a Lead pane's composer answers for *project*."""
        if getattr(pane, "composer", None) is None:
            return
        pane.leadQuestionAnswered.connect(lambda answers, p=project: self.answer(p, answers))

    # ── polling ─────────────────────────────────────────────────────────
    def _lead(self, project: str):
        try:
            return self._orch._project_panes(project).get("lead")
        except Exception:
            return None

    def _tick(self) -> None:
        if self._busy:
            return
        project = self._active_project()
        if not project:
            return
        pane = self._lead(project)
        if pane is None or getattr(pane, "composer", None) is None:
            return
        session = getattr(pane, "session", None)
        if session is None or not getattr(session, "is_alive", False):
            pane.composer.set_question(None)
            return
        notify = _remote("notify")
        if notify is None:
            return
        try:
            provider = notify.lead_provider_name(self._orch, project)
        except Exception:
            return
        if provider not in _ASK_PROVIDERS:
            pane.composer.set_question(None)
            return
        self._busy = True

        def _work(p=project) -> None:
            try:
                state = notify.current_ask_state(self._orch, p)
            except Exception:
                state = None
            self._stateReady.emit(p, state)

        threading.Thread(target=_work, name="lead-question-poll", daemon=True).start()

    def _apply_state(self, project: str, state) -> None:
        self._busy = False
        pane = self._lead(project)
        if pane is None or getattr(pane, "composer", None) is None:
            return
        if not isinstance(state, dict):
            state = None
        hidden = self._answered.get(project)
        if state is not None and hidden is not None:
            questions, until = hidden
            if time.monotonic() < until and state.get("questions") == questions:
                state = None
        pane.composer.set_question(state)

    # ── answering ───────────────────────────────────────────────────────
    def answer(self, project: str, answers: list) -> None:
        pane = self._lead(project)
        composer = getattr(pane, "composer", None)
        notify, api = _remote("notify"), _remote("api")
        if composer is None or notify is None or api is None:
            return
        try:
            # Re-read now: the picker may have moved on since the card was
            # drawn — same freshness guard the Remote endpoint applies.
            state = notify.current_ask_state(self._orch, project)
            if not state:
                composer.set_question(None)
                composer.show_status("คำถามนี้ถูกตอบไปแล้ว หรือ Lead ไปต่อแล้ว")
                return
            keys = api._build_picker_key_sequence(state["questions"], answers)
        except Exception as exc:
            composer.show_status(f"ส่งคำตอบไม่ได้: {exc}")
            return
        ok, msg = self._orch.answer_picker(keys, project=project)
        if not ok:
            composer.show_status(f"ส่งคำตอบไม่ได้: {msg}")
            return
        self._answered[project] = (state["questions"], time.monotonic() + _ANSWERED_HIDE_S)
        composer.set_question(None)
        _log_event("lead_composer_answer", project=project, questions=len(answers))
