"""PTY session: pywinpty + pyte screen model.

A PtySession owns:
  - a pywinpty PtyProcess running `claude.exe` (or any command)
  - a background QThread reading bytes from the pty
  - a pyte.Screen for ANSI parsing
  - signals: outputUpdated (whenever pyte screen changes), processExited

The terminal widget consumes the screen state, the orchestrator triggers writes.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Sequence

import pyte
from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from ._win_console import hide_hwnds, snapshot_console_hwnds

# ── Claude usage-limit detection ────────────────────────────────────────────
# When a claude pane hits the plan's usage limit it stops producing output and
# prints a "limit reached … resets <time>" banner. Without detection the idle
# watchdog mistakes this for "finished but forgot takkub done" and nags every
# 90s (and eventually force-respawns into the same limit). These markers let
# the orchestrator recognise the state, suppress the watchdog, and notify when
# the limit resets instead.
#
# ⚠ The exact wording + time format are Claude-Code-version-dependent and must
# be verified against a real limit banner. Override without a code change via
# TAKKUB_RATE_LIMIT_MARKERS (comma-separated substrings, lower-case).
_DEFAULT_RATE_LIMIT_MARKERS = (
    "usage limit",
    "limit reached",
    "limit will reset",
    "reached your usage",
    "out of usage",
)


def _rate_limit_markers() -> tuple[str, ...]:
    override = os.environ.get("TAKKUB_RATE_LIMIT_MARKERS", "").strip()
    if override:
        return tuple(m.strip().lower() for m in override.split(",") if m.strip())
    return _DEFAULT_RATE_LIMIT_MARKERS


# Reset clock-time, e.g. "resets 3pm", "resets at 3:30pm", "reset at 14:00".
_RESET_TIME_RE = re.compile(
    r"reset[s]?(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE
)
# Fallback window when the banner is present but no parseable time is found —
# Anthropic's rolling window is ~5h, so wait that long before re-checking.
_RATE_LIMIT_FALLBACK_SEC = 5 * 60 * 60


def _parse_rate_limit_reset(text: str, now: float) -> float | None:
    """Given lower-cased pane text, return the epoch the usage limit resets at,
    or None if no limit banner is present.

    If a banner is present but the reset time can't be parsed, fall back to
    now + ~5h so the watchdog still backs off rather than nagging forever.
    """
    if not any(m in text for m in _rate_limit_markers()):
        return None
    m = _RESET_TIME_RE.search(text)
    if not m:
        return now + _RATE_LIMIT_FALLBACK_SEC
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return now + _RATE_LIMIT_FALLBACK_SEC
    lt = time.localtime(now)
    target = time.struct_time(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst)
    )
    epoch = time.mktime(target)
    if epoch <= now:  # clock time already passed today → it means tomorrow
        epoch += 24 * 60 * 60
    return epoch


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

    def __init__(self, proc, on_data=None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc = proc
        # Called in THIS reader thread for each chunk — does the heavy pyte
        # parse + transcript write off the Qt main thread so many panes don't
        # serialise on it (see docs/cockpit-freeze-rca-2026-05-29.md).
        self._on_data = on_data
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
            # Parse + log in this thread first, then hand the raw bytes to the
            # main thread purely for rendering (xterm.js) + state-change notify.
            if self._on_data is not None:
                self._on_data(data)
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
        # Guards every read/write of the pyte screen. stream.feed() now runs in
        # the reader thread while the main thread reads display_lines() /
        # is_at_*_prompt() — without this lock those race.
        self._screen_lock = threading.Lock()
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

        self._reader = _ReaderThread(self._proc, on_data=self._feed_and_log, parent=self)
        self._reader.bytesReceived.connect(self._on_bytes)
        self._reader.finished_clean.connect(self._on_exit)
        self._reader.start()

        self._writer = _WriterThread(self._proc, parent=self)
        self._writer.start()

    def _feed_and_log(self, data: bytes) -> None:
        """Runs in the reader thread. Does the heavy work off the Qt main
        thread: write the transcript and feed pyte (under the screen lock).
        Best-effort — never raises so a bad chunk can't kill the reader."""
        if self._transcript is not None:
            try:
                self._transcript.write(data)
                self._transcript.flush()
            except Exception:
                # disk full / handle closed — stop trying rather than blocking the PTY
                self._transcript = None
        try:
            with self._screen_lock:
                self.stream.feed(data)
        except Exception:
            # pyte sometimes chokes on partial sequences; skip and continue
            pass

    def _on_bytes(self, data: bytes) -> None:
        # Runs on the Qt main thread (queued from the reader). pyte parsing and
        # the transcript write already happened in _feed_and_log on the reader
        # thread, so here we only forward raw bytes to xterm.js (rendering must
        # touch QWebEngine on the main thread) and notify state-detection
        # consumers that the screen changed.
        self.bytesIn.emit(data)
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
        with self._screen_lock:
            self.screen.resize(rows, cols)
        if self._alive and self._proc is not None:
            try:
                self._proc.setwinsize(rows, cols)
            except Exception:
                pass

    def terminate(self) -> None:
        if self._writer is not None:
            self._writer.request_stop()  # enqueue sentinel → writer loop exits
        if self._reader is not None:
            self._reader.request_stop()  # set stop flag
        if self._proc is not None:
            try:
                self._proc.terminate(force=True)  # unblocks reader's proc.read()
            except Exception:
                pass
        self._alive = False
        # Bug-7 fix: join threads so they don't accumulate as zombies across
        # many close/respawn cycles.  500 ms timeout avoids blocking the UI
        # thread; if a thread hasn't exited by then we leave it — the process
        # kill above ensures it will exit momentarily on its own.
        if self._writer is not None:
            self._writer.quit()
            self._writer.wait(500)
        if self._reader is not None:
            self._reader.quit()
            self._reader.wait(500)
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
        with self._screen_lock:
            return list(self.screen.display)

    def cursor(self) -> tuple[int, int]:
        with self._screen_lock:
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
        with self._screen_lock:
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

        Handles claude, codex, and gemini panes:
          - claude: bottom hint 'bypass permissions' or 'shift+tab to cycle',
                    never 'esc to interrupt' (working) or trust modal.
          - codex:  splash banner 'openai codex (v' visible, no modal
                    (`update available!`, `do you trust`, `press enter
                    to continue`) and no active interrupt indicator.
          - gemini: prompt hint 'type your message or @path' visible.
                    Without this marker, the idle watchdog never fired
                    on gemini panes (root cause of 'gemini forgot
                    takkub done' incidents 2026-05-20).
        """
        text = "\n".join(self.display_lines()).lower()
        # ── modal / interrupt blockers (apply to all providers) ─────
        if "trust this folder" in text:
            return False
        if "do you trust the contents of this directory" in text:
            return False
        if "press enter to continue" in text:
            return False
        if "esc to interrupt" in text:
            return False
        # gemini/codex show "Thinking… (esc to cancel)" while working, but
        # keep their "type your message or @path" input box visible the whole
        # time — so without this blocker the idle watchdog reads a thinking
        # gemini as idle and floods the pane with `takkub done` reminders
        # (root cause of the 2026-05-30 gemini reminder-pileup + search loop).
        if "esc to cancel" in text:
            return False
        # ── ready markers ───────────────────────────────────────────
        # gemini: the "type your message or @path" input prompt stays usable
        # even while the passive "Gemini CLI update available! <cur> → <new>"
        # footer banner is showing — which it does PERSISTENTLY once a newer
        # gemini release exists upstream. Check this ready marker BEFORE the
        # generic "update available!" blocker below; otherwise a gemini that
        # merely has an update banner reads as perpetually-busy, the idle
        # watchdog never nudges it to run `takkub done`, and its report never
        # reaches Lead (issue #51 — surfaced right after gemini auto-updated
        # to 0.46.0 with a newer release already published upstream).
        if "type your message or" in text:  # gemini's input prompt hint
            return True
        # codex: "update available!" is part of its startup splash modal that
        # must be dismissed before the prompt is usable — keep blocking it.
        # Checked AFTER gemini's ready marker so it only ever gates the codex
        # splash (and claude, which never shows this string), never a ready
        # gemini wearing an update footer.
        if "update available!" in text:
            return False
        if "openai codex (v" in text:
            return True
        return "bypass permissions" in text or "shift+tab to cycle" in text

    def rate_limit_reset_at(self) -> float | None:
        """If the pane is showing claude's usage-limit banner, return the epoch
        the limit resets at; else None.

        Used by the orchestrator's idle watchdog to tell "rate-limited, can't
        work until reset" apart from "idle, forgot takkub done" so it suppresses
        the reminder loop and notifies at reset time instead. See the marker
        notes at module top — detection wording needs real-banner verification.
        """
        text = "\n".join(self.display_lines()).lower()
        return _parse_rate_limit_reset(text, time.time())
