"""PTY session: pywinpty + pyte screen model.

A PtySession owns:
  - a pywinpty PtyProcess running `claude.exe` (or any command)
  - a background QThread reading bytes from the pty
  - a pyte.Screen for ANSI parsing
  - signals: outputUpdated (whenever pyte screen changes), processExited

The terminal widget consumes the screen state, the orchestrator triggers writes.
"""

from __future__ import annotations

import queue
import subprocess
import sys
from collections.abc import Sequence

import pyte
from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from ._win_console import hide_hwnds, snapshot_console_hwnds


class _WriterThread(QThread):
    def __init__(self, proc, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc = proc
        self._q: queue.Queue[str | None] = queue.Queue()

    def run(self) -> None:
        while True:
            data = self._q.get()
            if data is None:
                break
            try:
                self._proc.write(data)
            except Exception as e:
                print(f"[pty_session] write error: {e!r}", flush=True)

    def write(self, data: str) -> None:
        self._q.put(data)

    def request_stop(self) -> None:
        self._q.put(None)


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
    # Raw PTY bytes — consumed by the xterm.js TerminalWidget for rendering.
    bytesIn = pyqtSignal(bytes)
    # pyte screen mutated — still used by state-detection helpers
    # (is_at_trust_prompt, is_at_ready_prompt) and display_lines() export.
    outputUpdated = pyqtSignal()
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
        self._writer: _WriterThread | None = None
        self._alive = False
        self._transcript = None  # open file handle; None = not capturing

    # ──────────────────────────────────────────────────────────────
    # lifecycle
    # ──────────────────────────────────────────────────────────────
    def spawn(
        self,
        argv: Sequence[str],
        cwd: str | None = None,
        env: dict | None = None,
        transcript_path: str | None = None,
    ) -> None:
        import winpty  # `pywinpty` pkg, module name is `winpty`

        cmd = subprocess.list2cmdline(list(argv))

        # Snapshot console windows before spawn so we can hide whatever new
        # console window (cmd.exe / conhost) pywinpty surfaces.
        pre_hwnds = snapshot_console_hwnds() if sys.platform == "win32" else set()

        # Prefer ConPTY backend for lowest latency. ConPTY sends ANSI directly
        # instead of scraping the screen buffer like WinPTY, eliminating the
        # "delay" and replacing-character symptoms users experience during fast typing.
        try:
            import winpty

            self._proc = winpty.PtyProcess.spawn(
                cmd,
                dimensions=(self.rows, self.cols),
                cwd=cwd,
                env=env,
                backend=winpty.Backend.ConPTY,
            )
        except Exception:
            # fall back if ConPTY isn't available
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

        if transcript_path is not None:
            try:
                import logging

                self._transcript = open(transcript_path, "wb")
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "transcript open failed (%s): %r — PTY still running", transcript_path, exc
                )
                self._transcript = None

        self._reader = _ReaderThread(self._proc, parent=self)
        self._reader.bytesReceived.connect(self._on_bytes)
        self._reader.finished_clean.connect(self._on_exit)
        self._reader.start()

        self._writer = _WriterThread(self._proc, parent=self)
        self._writer.start()

    def _on_bytes(self, data: bytes) -> None:
        # Forward raw bytes to xterm.js (rendering layer) first so the user
        # sees output ASAP. pyte still consumes them for the state-detection
        # helpers (is_at_trust_prompt / is_at_ready_prompt / display_lines).
        self.bytesIn.emit(data)
        if self._transcript is not None:
            try:
                self._transcript.write(data)
                self._transcript.flush()
            except Exception:
                # disk full / handle closed — stop trying rather than blocking the PTY
                self._transcript = None
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
        if not self._alive or self._proc is None or self._writer is None:
            return
        # pywinpty 3.x .write() expects str (it does its own UTF-8 encoding
        # internally). Passing bytes raises TypeError.
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        self._writer.write(data)

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
        if self._writer is not None:
            self._writer.request_stop()
        if self._proc is not None:
            try:
                self._proc.terminate(force=True)
            except Exception:
                pass
        self._alive = False
        if self._transcript is not None:
            try:
                self._transcript.close()
            except Exception:
                pass
            self._transcript = None

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
        """True when claude OR codex is showing a trust-directory modal.

        Both CLIs default-select "Yes/trust" so a single Enter keypress
        accepts. Patterns:
          - claude: "Yes, I trust this folder" + "Enter to confirm"
          - codex:  "Do you trust the contents of this directory"
                    + "Press enter to continue"
        """
        text = "\n".join(self.display_lines()).lower()
        if "trust this folder" in text and "enter to confirm" in text:
            return True
        if "do you trust the contents of this directory" in text:
            return True
        return False

    def is_at_ready_prompt(self) -> bool:
        """True when the underlying TUI is idle at its main input prompt.

        Handles both claude and codex panes:
          - claude: bottom hint 'bypass permissions' or 'shift+tab to cycle',
                    never 'esc to interrupt' (working) or trust modal.
          - codex:  splash banner 'openai codex (v' visible, no modal
                    (`update available!`, `do you trust`, `press enter
                    to continue`) and no active interrupt indicator.
        """
        text = "\n".join(self.display_lines()).lower()
        # ── modal / interrupt blockers (apply to both providers) ────
        if "trust this folder" in text:
            return False
        if "do you trust the contents of this directory" in text:
            return False
        if "update available!" in text:
            return False
        if "press enter to continue" in text:
            return False
        if "esc to interrupt" in text:
            return False
        # ── ready markers ───────────────────────────────────────────
        if "openai codex (v" in text:
            return True
        return "bypass permissions" in text or "shift+tab to cycle" in text
