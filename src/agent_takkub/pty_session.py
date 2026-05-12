"""PTY session: pywinpty + pyte screen model.

A PtySession owns:
  - a pywinpty PtyProcess running `claude.exe` (or any command)
  - a background QThread reading bytes from the pty
  - a pyte.Screen for ANSI parsing
  - signals: outputUpdated (whenever pyte screen changes), processExited

The terminal widget consumes the screen state, the orchestrator triggers writes.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence

import pyte
from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from ._win_console import hide_hwnds, snapshot_console_hwnds


class _ReaderThread(QThread):
    bytesReceived = pyqtSignal(bytes)
    finished_clean = pyqtSignal()

    def __init__(self, proc, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc = proc
        self._stop = False

    def run(self) -> None:
        # pywinpty 3.x semantics: read(size) returns whatever is buffered, but
        # can raise EOFError when the buffer is momentarily empty even though
        # the child process is still alive and just waiting for input. We
        # only treat EOF as termination after isalive() confirms the process
        # has actually exited.
        import time

        while not self._stop:
            try:
                data = self._proc.read(4096)
            except EOFError:
                if not self._proc.isalive():
                    break
                time.sleep(0.04)
                continue
            except Exception as e:
                print(f"[pty_session] read error: {e!r}", flush=True)
                if not self._proc.isalive():
                    break
                time.sleep(0.04)
                continue

            if not data:
                if not self._proc.isalive():
                    break
                time.sleep(0.02)
                continue

            if isinstance(data, str):
                data = data.encode("utf-8", "replace")
            self.bytesReceived.emit(data)
        self.finished_clean.emit()

    def request_stop(self) -> None:
        self._stop = True


class PtySession(QObject):
    outputUpdated = pyqtSignal()  # screen state changed; widget should redraw
    processExited = pyqtSignal(int)  # exit code (best effort)

    def __init__(
        self,
        cols: int = 100,
        rows: int = 36,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.cols = cols
        self.rows = rows
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self._proc = None
        self._reader: _ReaderThread | None = None
        self._alive = False

    # ──────────────────────────────────────────────────────────────
    # lifecycle
    # ──────────────────────────────────────────────────────────────
    def spawn(
        self,
        argv: Sequence[str],
        cwd: str | None = None,
        env: dict | None = None,
    ) -> None:
        import winpty  # `pywinpty` pkg, module name is `winpty`

        cmd = subprocess.list2cmdline(list(argv))

        # Snapshot console windows before spawn so we can hide whatever new
        # console window (cmd.exe / conhost) pywinpty surfaces.
        pre_hwnds = snapshot_console_hwnds() if sys.platform == "win32" else set()

        # Prefer WinPTY backend on Windows: ConPTY spawns a visible conhost
        # console window when launched from a GUI process. WinPTY uses a
        # hidden agent process and stays out of sight (in theory).
        try:
            self._proc = winpty.PtyProcess.spawn(
                cmd,
                dimensions=(self.rows, self.cols),
                cwd=cwd,
                env=env,
                backend=winpty.Backend.WinPTY,
            )
        except Exception:
            # fall back to ConPTY if WinPTY isn't available
            self._proc = winpty.PtyProcess.spawn(
                cmd,
                dimensions=(self.rows, self.cols),
                cwd=cwd,
                env=env,
            )
        self._alive = True

        # The console window can take a moment to appear. Retry hiding on a
        # short backoff so we catch it whenever it shows up.
        if sys.platform == "win32":

            def _sweep() -> None:
                new = snapshot_console_hwnds() - pre_hwnds
                if new:
                    hide_hwnds(new)

            for delay in (150, 400, 900, 1800, 3500):
                QTimer.singleShot(delay, _sweep)

        self._reader = _ReaderThread(self._proc, parent=self)
        self._reader.bytesReceived.connect(self._on_bytes)
        self._reader.finished_clean.connect(self._on_exit)
        self._reader.start()

    def _on_bytes(self, data: bytes) -> None:
        try:
            self.stream.feed(data)
        except Exception:
            # pyte sometimes chokes on partial sequences; skip and continue
            pass
        self.outputUpdated.emit()

    def _on_exit(self) -> None:
        self._alive = False
        code = 0
        try:
            if self._proc is not None:
                code = self._proc.exitstatus or 0
        except Exception:
            pass
        self.processExited.emit(code)

    def write(self, data: bytes | str) -> None:
        if not self._alive or self._proc is None:
            return
        # pywinpty 3.x .write() expects str (it does its own UTF-8 encoding
        # internally). Passing bytes raises TypeError.
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        try:
            self._proc.write(data)
        except Exception as e:
            print(f"[pty_session] write error: {e!r}", flush=True)

    def resize(self, cols: int, rows: int) -> None:
        if cols < 20 or rows < 5:
            return
        self.cols = cols
        self.rows = rows
        self.screen.resize(rows, cols)
        if self._alive and self._proc is not None:
            try:
                self._proc.setwinsize(rows, cols)
            except Exception:
                pass

    def terminate(self) -> None:
        if self._reader is not None:
            self._reader.request_stop()
        if self._proc is not None:
            try:
                self._proc.terminate(force=True)
            except Exception:
                pass
        self._alive = False

    @property
    def is_alive(self) -> bool:
        return self._alive

    # ──────────────────────────────────────────────────────────────
    # screen access
    # ──────────────────────────────────────────────────────────────
    def display_lines(self) -> list[str]:
        """Return the visible screen as a list of rows (top → bottom)."""
        return list(self.screen.display)

    def cursor(self) -> tuple[int, int]:
        c = self.screen.cursor
        return c.x, c.y

    def display_rich(self) -> list[list[tuple[str, str, str, bool, bool, bool, bool]]]:
        """Return rows as lists of style-runs.

        Each run is (text, fg, bg, bold, italic, underline, reverse). fg/bg
        are pyte color strings ('default', a named colour like 'red', or a
        6-char hex). Adjacent cells with identical attrs are merged into one
        run, keeping the row's total run count low for fast rendering.
        """
        rows: list[list[tuple[str, str, str, bool, bool, bool, bool]]] = []
        for y in range(self.screen.lines):
            line = self.screen.buffer[y]
            runs: list[tuple[str, str, str, bool, bool, bool, bool]] = []
            cur_text = ""
            cur_key: tuple = ()
            for x in range(self.screen.columns):
                cell = line[x]
                key = (
                    cell.fg,
                    cell.bg,
                    cell.bold,
                    cell.italics,
                    cell.underscore,
                    cell.reverse,
                )
                if key != cur_key:
                    if cur_text:
                        runs.append((cur_text, *cur_key))
                    cur_text = cell.data
                    cur_key = key
                else:
                    cur_text += cell.data
            if cur_text:
                runs.append((cur_text, *cur_key))
            rows.append(runs)
        return rows

    # ──────────────────────────────────────────────────────────────
    # state detection: helps orchestrator auto-trust / wait-for-ready
    # ──────────────────────────────────────────────────────────────
    def is_at_trust_prompt(self) -> bool:
        """True when claude is showing the 'Yes, I trust this folder' modal."""
        text = "\n".join(self.display_lines()).lower()
        return "trust this folder" in text and "enter to confirm" in text

    def is_at_ready_prompt(self) -> bool:
        """True when claude's main `❯` input is idle and ready for a task.

        Identified by the presence of the bottom hint bar ('bypass permissions
        on' or 'shift+tab to cycle') and the absence of any modal/processing
        indicator ('esc to interrupt', 'trust this folder').
        """
        text = "\n".join(self.display_lines()).lower()
        if "trust this folder" in text:
            return False
        if "esc to interrupt" in text:
            return False
        return "bypass permissions" in text or "shift+tab to cycle" in text
