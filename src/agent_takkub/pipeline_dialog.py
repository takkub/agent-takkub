"""Pipeline-Settings dialog — hosts the approved settings page in a
QWebEngineView and bridges it to the pipeline/provider config.

Mirrors the QWebChannel pattern in :mod:`terminal_widget`. The page
(``static/pipeline_settings.html``) talks to Python through a registered
``bridge`` object:

* ``bridge.loadState(cb)``       → ``cb(json)`` with the full initial state
* ``bridge.savePipelines(json)`` → persist templates + per-role enable +
  active pointer; remember the desired provider on/off for the caller
* ``bridge.closeDialog(saved)``  → close the dialog (accept if saved, else reject)

Division of ownership:

* Templates / rolesEnabled / activeTemplate live in :mod:`pipeline_config`
  (``pipelines.json``) and are persisted here directly.
* Provider (codex/gemini) enable/disable lives in :mod:`provider_state`
  (``disabled-providers.json``). The bridge does NOT write it directly —
  it records the *desired* state (:attr:`_PipelineBridge.pending_provider_disabled`)
  and lets ``MainWindow`` apply it through ``orchestrator.toggle_provider`` so
  live Lead panes get the same ``[system]`` broadcast a status-bar chip click
  produces. This keeps the two provider-toggle entry points consistent.

Polarity note: the page uses ``true = native CLI on``; provider_state uses
``true = disabled``. :mod:`pipeline_config` helpers invert at the boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QObject, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QDialog, QMessageBox, QVBoxLayout, QWidget

from . import pipeline_config, provider_state

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_PAGE_URL = QUrl.fromLocalFile(str(_STATIC_DIR / "pipeline_settings.html"))


class _PipelineBridge(QObject):
    """Object exposed to the settings page via QWebChannel."""

    closeRequested = pyqtSignal(bool)  # True = saved (accept), False = cancel (reject)
    saveError = pyqtSignal(str)  # disk/parse failure → surfaced to the user
    savedOk = pyqtSignal()  # emitted only after a successful disk write

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # {provider: desired_disabled} from the last successful save; read by
        # MainWindow after the dialog closes to broadcast via the orchestrator.
        self.pending_provider_disabled: dict[str, bool] = {}

    @pyqtSlot(result=str)
    def loadState(self) -> str:
        """Return the full settings state as a JSON string.

        Composes pipeline_config (templates/rolesEnabled/activeTemplate) with
        provider enable-state read from provider_state (inverted to the page's
        ``true = enabled`` convention).
        """
        payload = pipeline_config.load()
        payload = pipeline_config.with_providers(
            payload, provider_state.all_disabled(), provider_state.TOGGLABLE
        )
        return json.dumps(payload)

    @pyqtSlot(str)
    def savePipelines(self, json_str: str) -> None:
        """Persist the state blob the page sends on Save & Apply.

        Templates/rolesEnabled/activeTemplate go to pipelines.json now; the
        provider on/off targets are stashed for MainWindow to apply through the
        orchestrator (so live Lead panes are notified). Disk/parse errors are
        reported via :attr:`saveError` instead of failing silently.
        """
        try:
            data = json.loads(json_str)
            if not isinstance(data, dict):
                self.saveError.emit("settings payload was not a JSON object")
                return
            self.pending_provider_disabled = pipeline_config.provider_disabled_targets(
                data, provider_state.TOGGLABLE
            )
            pipeline_config.save(data)  # ignores the providers key it carries
            self.savedOk.emit()  # only after successful disk write
        except (json.JSONDecodeError, TypeError) as e:
            self.saveError.emit(f"couldn't parse settings: {e}")
        except OSError as e:
            self.saveError.emit(f"couldn't write pipelines.json: {e}")

    @pyqtSlot(bool)
    def closeDialog(self, saved: bool) -> None:
        """Ask the host dialog to close (accept when ``saved`` else reject)."""
        self.closeRequested.emit(saved)


class PipelineSettingsDialog(QDialog):
    """Modal dialog hosting the pipeline-settings page with a bridged backend."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pipeline Settings — takkub")
        self.resize(980, 760)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Channel + bridge MUST be wired before load() or the JS handshake
        # races the page and `bridge` stays null on the JS side.
        self._view = QWebEngineView(self)
        layout.addWidget(self._view, 1)

        self._channel = QWebChannel(self)
        self._bridge = _PipelineBridge(self)
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)

        self._bridge.closeRequested.connect(self._on_close_requested)
        self._bridge.saveError.connect(self._on_save_error)
        # JS calls closeDialog(true) immediately after savePipelines (fire-and-
        # forget). We gate the accept() on savedOk so a disk error keeps the
        # dialog open and lets the user retry rather than silently losing data.
        self._pending_accept = False
        self._bridge.savedOk.connect(self._on_saved_ok)
        self._bridge.saveError.connect(self._on_save_error_cancel_close)

        self._view.load(_PAGE_URL)

    @property
    def bridge(self) -> _PipelineBridge:
        """Expose the bridge so the caller can read pending provider changes."""
        return self._bridge

    def _on_close_requested(self, saved: bool) -> None:
        if not saved:
            self.reject()
            return
        # Accept is gated: wait for savedOk (Python confirms disk write) before
        # closing, so a race between savePipelines error and closeDialog(true)
        # doesn't close the dialog on a failed save.
        self._pending_accept = True
        # If savedOk already fired in this event-loop tick (normal fast path),
        # _pending_accept will be consumed by _on_saved_ok; otherwise it waits.

    def _on_saved_ok(self) -> None:
        """Disk write confirmed — close with accept if JS already asked for it."""
        if self._pending_accept:
            self._pending_accept = False
            self.accept()

    def _on_save_error(self, msg: str) -> None:
        QMessageBox.critical(self, "Save failed", f"Couldn't save pipeline settings:\n{msg}")

    def _on_save_error_cancel_close(self, _msg: str) -> None:
        """Disk write failed — discard any pending accept so the dialog stays open."""
        self._pending_accept = False
