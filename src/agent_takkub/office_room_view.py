"""OfficeRoomView: canvas-based 'Office Room' game view for the cockpit.

Hosts a QWebEngineView running a <canvas> scene (vanilla JS) and bridges
orchestrator signals → JSON game events so on-screen characters track real
pane state.

Bridge protocol (Qt → JS):
  bridge.gameEvent  pyqtSignal(str)   — emits JSON string to JS scene
  JSON shape: {"type": "pane_state", "role": str, "state": str,
               "project": str, "note": str}
  state values: "spawn" | "busy" | "idle" | "done" | "close"

Bridge protocol (JS → Qt):
  bridge.leadClicked()           pyqtSlot       — user clicked Lead desk (no msg)
  bridge.sendMessage(msg: str)   pyqtSlot(str)  — send typed msg to Lead pane
  bridge.requestFocus(role: str) slot            — switch cockpit to that role's tab

**Import constraint:** must NOT import app, cli, or orchestrator.
"""

from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QObject, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QVBoxLayout, QWidget

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_OFFICE_ROOM_URL = QUrl.fromLocalFile(str(_STATIC_DIR / "office_room.html"))


class _OfficeRoomBridge(QObject):
    """Object registered with QWebChannel under the name 'bridge'."""

    # Python → JS: game scene listens on this signal for state updates
    gameEvent = pyqtSignal(str)

    # JS → Python: emitted when user clicks Lead character in game (no message)
    leadClickedInGame = pyqtSignal()
    # JS → Python: user typed a message and clicked Send in the chat overlay
    messageToLead = pyqtSignal(str)
    # JS → Python: user clicked any role desk → focus that role's pane tab
    focusRoleRequested = pyqtSignal(str)

    @pyqtSlot()
    def leadClicked(self) -> None:
        self.leadClickedInGame.emit()

    @pyqtSlot(str)
    def sendMessage(self, msg: str) -> None:
        """JS btn-send handler calls this with the textarea text."""
        if msg.strip():
            self.messageToLead.emit(msg)

    @pyqtSlot(str)
    def requestFocus(self, role: str) -> None:
        self.focusRoleRequested.emit(role)


class OfficeRoomView(QWidget):
    """Full-view canvas game that tracks orchestrator pane events.

    Drop this into a QStackedWidget alongside the normal pane_tabs widget;
    the ProjectTab toggles between them via show/hide on the stack.
    """

    # Propagated outward so ProjectTab/MainWindow can react to Lead clicks
    leadClickedInGame = pyqtSignal()
    # Propagated outward: user sent a message via the game chat overlay
    messageToLead = pyqtSignal(str)
    focusRoleRequested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._view = QWebEngineView(self)
        layout.addWidget(self._view, 1)

        self._channel = QWebChannel(self)
        self._bridge = _OfficeRoomBridge(self)
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)

        self._bridge.leadClickedInGame.connect(self.leadClickedInGame)
        self._bridge.messageToLead.connect(self.messageToLead)
        self._bridge.focusRoleRequested.connect(self.focusRoleRequested)

        self._page_ready = False
        self._pending: list[str] = []

        self._view.loadFinished.connect(self._on_load_finished)
        self._view.load(_OFFICE_ROOM_URL)

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            return
        self._page_ready = True
        for payload in self._pending:
            self._bridge.gameEvent.emit(payload)
        self._pending.clear()

    def dispatch_event(
        self,
        role: str,
        state: str,
        project: str = "",
        note: str = "",
    ) -> None:
        """Send a pane state change to the JS scene.

        state: "spawn" | "busy" | "idle" | "done" | "close"
        """
        payload = json.dumps(
            {
                "type": "pane_state",
                "role": role,
                "state": state,
                "project": project,
                "note": note,
            },
            ensure_ascii=False,
        )
        if self._page_ready:
            self._bridge.gameEvent.emit(payload)
        else:
            self._pending.append(payload)

    # Keep-alive: follow the same pattern as TerminalWidget / AgentPane
    def set_keepalive(self, active: bool) -> None:
        page = self._view.page()
        if page is not None:
            page.setLifecycleState(
                page.LifecycleState.Active if active else page.LifecycleState.Frozen
            )

    def destroy_view(self) -> None:
        """Explicit cleanup for the WebEngine view and its timers."""
        try:
            self._view.page().setWebChannel(None)
            self._view.stop()
            self._view.setPage(None)
        except Exception:
            pass
