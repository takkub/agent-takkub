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
from wcwidth import wcwidth

from ._win_console import hide_hwnds, snapshot_console_hwnds

# CREATE_NO_WINDOW so the helper taskkill doesn't flash a console window.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _safe_screen_display(screen: pyte.Screen) -> list[str]:
    """``pyte.Screen.display`` rendered defensively against orphaned wide-char stubs.

    pyte writes a ``data=""`` stub into the cell *after* a wide (width-2)
    character. When a TUI redraw overwrites that wide char with a narrower one —
    ubiquitous in the claude/agy/codex spinner + status lines that repaint a
    progress glyph (emoji / CJK / box-drawing) many times a second — the stub is
    orphaned, and pyte's own ``display`` property then crashes with
    ``IndexError: string index out of range`` on ``wcwidth(char[0])`` (it indexes
    ``char[0]`` without guarding ``char == ""``).

    That exception fires on *every* idle-watchdog tick via ``display_lines()``
    (``is_at_ready_prompt`` / ``has_unparsed_tool_call`` / ``is_blocked_on_tty_prompt``).
    The per-pane try/except in ``_check_idle_teammates`` then swallowed it, which:
      • on a teammate pane — skipped the forgot-``takkub done`` reminder + harvest
        hint, so the pane sat idle until the user closed it, never reporting; and
      • on a Lead pane — made ``is_at_ready_prompt`` raise inside the notify pump /
        reaper, so queued done notices never reached the Lead.
    Both surface as "pane finished, closed, never reported back" — and get worse
    with many panes/projects open (more terminals → higher odds one holds a poison
    stub). Rendering the stub as empty instead of indexing it removes the crash at
    the source. See runtime/events.log ``idle_watchdog_pane_error`` entries.
    """
    rows: list[str] = []
    for y in range(screen.lines):
        line = screen.buffer[y]
        chars: list[str] = []
        skip_stub = False
        for x in range(screen.columns):
            if skip_stub:  # the legitimate stub right after a wide char
                skip_stub = False
                continue
            data = line[x].data
            if not data:  # orphaned stub / empty cell — pyte would IndexError here
                continue
            skip_stub = wcwidth(data[0]) == 2
            chars.append(data)
        rows.append("".join(chars))
    return rows


def _tree_kill(pid: int | None) -> None:
    """Force-kill `pid` and its entire descendant process tree (Windows).

    pywinpty's PtyProcess.terminate() only reaps the root command (claude.exe);
    grandchildren spawned by a teammate — most painfully a `next dev` / `npm run
    dev` server and the postcss / jest-worker node subprocesses it forks — are
    left orphaned and accumulate into thousands of zombie node procs. `taskkill
    /T` walks the parent→child tree by PID and force-kills every descendant.

    Best-effort and non-blocking-ish: short timeout, never raises (a failure
    here must not prevent the rest of terminate() from running).
    """
    if pid is None or sys.platform != "win32":
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
            timeout=5,
            check=False,
        )
    except Exception:
        pass


# ── Malformed tool-call XML detection ───────────────────────────────────────
# When a model outputs tool-call XML without the required `antml:` namespace
# prefix (using bare `<invoke>` / `<parameter>` / `<function_calls>` instead
# of `<invoke>` etc.) the harness cannot parse and execute it — the XML
# simply renders as plain text and the pane appears to hang silently.
#
# Key invariant: a *well-formed* tool call is consumed by the harness before
# reaching the terminal renderer, so it never appears as literal text on-screen.
# Any occurrence of these tag patterns as visible text therefore means the tool
# call is malformed and was never executed.
#
# Patterns cover both opening and closing tags, with or without the `antml:`
# prefix, since either variant appearing as screen text signals a parse failure.
_MALFORMED_XML_RE = re.compile(
    r"<\s*/?\s*(antml:)?(invoke|parameter|function_calls)\b",
    re.IGNORECASE,
)
# Scan this many rows ending at the cursor row (tool-call XML tends to sit just
# above the cursor after the model finishes outputting it).
_MALFORMED_XML_TAIL_ROWS = 10


# ── Interactive TTY-prompt detection ────────────────────────────────────────
# When a shell command (npx, git, rm -rf) pauses for user input the pane
# output stops scrolling and the cursor sits on a y/N or credential line.
# The idle watchdog cannot tell this apart from "agent finished but forgot
# takkub done" — it fires reminders into the blocked pane indefinitely.
# `is_blocked_on_tty_prompt()` lets the watchdog distinguish the two states
# so it can log a warning and suppress repeated reminders instead.
#
# Patterns are anchored to the bottom _TTY_PROMPT_TAIL_ROWS rows (where the
# cursor rests after a prompt is emitted) to avoid matching identical text
# in earlier scrollback that the agent has already answered and moved past.
_TTY_PROMPT_RE = re.compile(
    r"ok to proceed\? \(y\)"  # npx first-install download gate
    r"|\[[yY]/[nN]\]"  # [Y/n] [y/N] [y/n] [Y/N]
    r"|\([yY]/[nN]\)"  # (y/n) (Y/n) (y/N)
    r"|press any key"  # generic pause / paginator
    r"|overwrite\?"  # rsync, cp -i, create-react-app
    r"|are you sure"  # git push --force guard, rm -rf guard
    r"|password:\s*$"  # git credential / SSH passphrase
    r"|username:\s*$",  # git credential username
    re.IGNORECASE | re.MULTILINE,
)
_TTY_PROMPT_TAIL_ROWS = 5


# ── Ready-prompt detection markers (M4#17) ──────────────────────────────────
# is_at_ready_prompt() decides whether a pane is idle at its input prompt. The
# markers are provider-specific bottom-row UI strings; when an upstream CLI
# rewords its prompt, detection silently breaks and that provider's idle
# watchdog / done-gate stalls — this happened 3x (gemini input hint, gemini
# update footer #51, codex splash). Centralised here as ONE ordered table so a
# reword is a one-line patch, with an env override to rescue a reworded prompt
# without shipping code, and a doctor self-test that flags a stale marker.
#
# Version-dependence registry (#20) — every marker below is natural-language UI
# text owned by an upstream CLI, so ALL are version-dependent. By blast radius:
#   HIGH  'esc to interrupt' / 'esc to cancel' — the busy indicators. A reword
#         makes a working pane read idle (premature done-nudge / early gate).
#   HIGH  'bypass permissions' / 'shift+tab to cycle' (claude), '? for shortcuts'
#         (agy), 'type your message or' (gemini) — the idle footers. A reword
#         makes an idle pane read busy (watchdog never nudges; #70-style stall).
#   MED   'update available!' (codex splash), 'trust this folder' / 'do you trust'
#         / 'press enter to continue' (trust modals) — transient spawn-time gates.
#   LOW   'gemini cli update available!' / 'openai codex (v' — passive banners.
# Replacing this text with structural signals (exit codes / pty mode / ANSI) is
# largely infeasible: the CLI is one long-lived interactive TUI (no exit code
# while running) always in raw mode (no discriminating pty flag). The mitigation
# is layered instead of a rewrite:
#   1. _ready_region() scopes matching to the bottom footer rows so conversation
#      body text quoting a marker can't poison the verdict (#70 root fix).
#   2. TAKKUB_EXTRA_READY_MARKERS lets an operator rescue a reword with no deploy.
#   3. ready_marker_selftest() (takkub doctor) catches a stale shipped marker.
#   4. The orchestrator's structural stale-marker detector (output-quiescence +
#      no-marker-match → 'ready_marker_possibly_stale' log with the real footer)
#      turns a field reword from a SILENT idle-watchdog stall into a loud,
#      actionable diagnostic. See Orchestrator._check_stale_markers.
#
# Hard blockers: any present → NEVER ready (active interrupt / modal), even if a
# ready marker is also on screen.
_READY_HARD_BLOCKERS = (
    "trust this folder",
    "do you trust the contents of this directory",
    "press enter to continue",
    "esc to interrupt",
    "esc to cancel",
)
# Ordered ready/soft-block rules — FIRST match wins. The order ENCODES the
# per-provider precedence: gemini's persistent input hint + passive update
# footer must beat the codex "update available!" splash blocker, which in turn
# beats codex's own banner. Changing the order changes behaviour — keep it.
# (ready_when, marker)
_READY_RULES: tuple[tuple[bool, str], ...] = (
    # agy (Antigravity) — the gemini role's engine since 2026-06-19. Its idle
    # TUI shows a '? for shortcuts' footer at the input prompt. Listed first so
    # it wins before the codex 'update available!' blocker (parity with how
    # gemini's footer used to). Busy state is covered by the 'esc to
    # interrupt/cancel' hard blockers above, which override this when present.
    (True, "? for shortcuts"),  # agy idle prompt footer
    (True, "type your message or"),  # gemini CLI (legacy) input prompt hint
    (True, "gemini cli update available!"),  # gemini CLI (legacy) passive footer (#51)
    (False, "update available!"),  # codex startup splash modal
    (True, "openai codex (v"),  # codex prompt banner
    (True, "bypass permissions"),  # claude footer
    (True, "shift+tab to cycle"),  # claude footer
)


def _extra_ready_markers() -> tuple[str, ...]:
    """Operator-supplied extra ready markers (lower-case substrings) to rescue an
    upstream-reworded prompt WITHOUT a code change. Checked after the hard
    blockers so an active interrupt/modal still wins. (M4#17)"""
    override = os.environ.get("TAKKUB_EXTRA_READY_MARKERS", "").strip()
    if not override:
        return ()
    return tuple(m.strip().lower() for m in override.split(",") if m.strip())


def _classify_ready(text_lower: str) -> bool:
    """Pure ready-prompt verdict over already-lowercased screen text. Shared by
    is_at_ready_prompt() and the doctor self-test so the two can't drift. (M4#17)

    Faithful to the original if/return chain: hard blockers (all → not ready)
    came first there too, so grouping them is equivalent; the ordered rules then
    reproduce the exact first-match-wins precedence."""
    if any(b in text_lower for b in _READY_HARD_BLOCKERS):
        return False
    for marker in _extra_ready_markers():
        if marker in text_lower:
            return True
    for ready_when, marker in _READY_RULES:
        if marker in text_lower:
            return ready_when
    return False


# Ready/blocker markers are bottom-row TUI chrome — the footer hint, the spinner
# status line ('esc to interrupt'), the input box. Conversation BODY text scrolls
# ABOVE that region. Scoping detection to the bottom rows stops body text that
# merely *quotes* a marker string (e.g. a Lead discussing "esc to interrupt" or
# "bypass permissions") from poisoning the verdict — the root of the #70 false-
# busy stall and the #20 text-marker fragility. Mirrors _TTY_PROMPT_TAIL_ROWS,
# which already anchors tty-prompt detection to the bottom for the same reason.
_READY_TAIL_ROWS = 6


def _ready_region(lines: list[str]) -> str:
    """Lowercased bottom _READY_TAIL_ROWS non-blank rows of the screen — the
    footer/status/input region where ready & blocker markers actually render.

    Trailing blank rows are stripped first so the window lands on real chrome on
    a partially-filled screen rather than empty padding. Short screens (≤ tail
    rows, e.g. test fixtures and fresh panes) are returned whole, so existing
    behaviour is unchanged there."""
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    start = max(0, end - _READY_TAIL_ROWS)
    return "\n".join(lines[start:end]).lower()


# Placeholder claude renders in its input box for a bracketed multi-line paste,
# e.g. "[Pasted text +42 lines]". Its presence in the input region confirms the
# paste actually landed (vs a swallowed paste that leaves the box empty — #26).
_PASTED_PLACEHOLDER = "[pasted text"
# Leading chars of the content to look for as a fallback presence signal when a
# short paste rendered inline (no placeholder).
_INPUT_FRAGMENT_LEN = 24


def _input_has_content(region: str, fragment: str) -> bool:
    """True when the bottom input region shows pasted/typed content.

    Two signals: the multi-line paste placeholder, or — for short inline
    content with no placeholder — a leading fragment of the expected text. The
    region is already lowercased by ``_ready_region``."""
    if _PASTED_PLACEHOLDER in region:
        return True
    frag = fragment.strip().lower()[:_INPUT_FRAGMENT_LEN]
    return bool(frag) and frag in region


# Canonical sample screens with their expected verdict — bake the behaviour so a
# marker going stale is caught by `takkub doctor` instead of silently breaking
# the idle watchdog. Each tuple is (screen_text, expected_is_ready).
_READY_SELFTEST_CASES: tuple[tuple[str, bool], ...] = (
    ("> \n? for shortcuts            Gemini 3.5 Flash (Medium)", True),  # agy idle
    # agy busy: even if the '? for shortcuts' footer persists, an active
    # interrupt indicator is a hard blocker → not ready (no premature done-nudge).
    ("Thinking... (esc to interrupt)\n? for shortcuts", False),  # agy busy
    ("Type your message or @path/to/file", True),  # gemini CLI (legacy) idle
    ("Thinking... (esc to cancel, 12s)\nType your message or @path", False),  # gemini busy
    ("Gemini CLI update available! 0.46.0 -> 0.47.0\nType your message or @path", True),
    ("Gemini CLI update available! 0.46.0 -> 0.47.0", True),  # passive footer alone
    ("OpenAI Codex (v1.2.3)\nupdate available! run npm i -g @openai/codex", False),  # codex splash
    ("bypass permissions", True),  # claude idle
    ("(esc to interrupt) building...\nbypass permissions", False),  # claude busy
)


def ready_marker_selftest() -> list[str]:
    """Run the canned ready/busy screens through _classify_ready and return a
    list of human-readable failures (empty = all good). Called by doctor so a
    stale ready marker surfaces as a diagnostic rather than a silent stall. The
    env override is intentionally ignored here — the self-test validates the
    SHIPPED table. (M4#17)"""
    failures: list[str] = []
    saved = os.environ.pop("TAKKUB_EXTRA_READY_MARKERS", None)
    try:
        for text, expected in _READY_SELFTEST_CASES:
            got = _classify_ready(text.lower())
            if got != expected:
                first = text.splitlines()[0] if text else ""
                failures.append(
                    f"ready-marker selftest: {first!r} expected ready={expected}, got {got}"
                )
    finally:
        if saved is not None:
            os.environ["TAKKUB_EXTRA_READY_MARKERS"] = saved
    return failures


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
        # Root PID of the spawned command (claude.exe), captured at spawn so
        # terminate() can tree-kill descendants even after _proc is torn down.
        self._pid: int | None = None
        self._reader: _ReaderThread | None = None
        self._writer: _WriterThread | None = None
        self._alive = False
        self._transcript = None  # open file handle; None = not capturing
        # CLAUDE_CONFIG_DIR this session was spawned with, captured from the
        # spawn env (None = default profile / ~/.claude). The token meter reads
        # it so a pane on a non-default user profile finds its session JSONL
        # under <config_dir>/projects/ instead of ~/.claude/projects/.
        self._claude_config_dir: str | None = None
        # Monotonic timestamp of the last PTY output chunk. A *structural*
        # idle/busy signal (independent of TUI text markers, #20): a generating
        # CLI streams output continuously (spinner repaint + token stream), so a
        # long gap since the last chunk means the pane has gone quiet. 0.0 = no
        # output seen yet. Written in the reader thread, read on the main thread;
        # a plain float read/write is atomic under the GIL so no lock is needed.
        self._last_output_ts: float = 0.0

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

        # Remember which Claude config home this pane uses so the token meter
        # can locate its session JSONL (non-default profiles redirect it).
        self._claude_config_dir = (env or {}).get("CLAUDE_CONFIG_DIR")

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
        try:
            self._pid = int(self._proc.pid)
        except Exception:
            self._pid = None

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
        # Structural quiescence signal (#20): stamp every real output chunk.
        self._last_output_ts = time.monotonic()
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
        # PyQt6 raises RuntimeError (not AttributeError) for any attribute access on
        # a QObject created via __new__ without __init__ (used by some test fixtures).
        # Guard every attribute access here so terminate() is always safe to call.
        try:
            _writer = self._writer
        except (AttributeError, RuntimeError):
            _writer = None
        try:
            _reader = self._reader
        except (AttributeError, RuntimeError):
            _reader = None
        if _writer is not None:
            _writer.request_stop()  # enqueue sentinel → writer loop exits
        if _reader is not None:
            _reader.request_stop()  # set stop flag
        # Tree-kill the whole descendant chain BEFORE killing the root. pywinpty's
        # terminate(force=True) only reaps claude.exe itself; a teammate that ran
        # `next dev` / `npm run dev` leaves the node dev server and its postcss /
        # jest-worker subprocesses orphaned, which pile up into thousands of zombie
        # node procs (observed: a single leaked `next dev` reached 3170 procs /
        # 18 GB). `taskkill /T` walks the live parent→child tree, so it MUST run
        # while the root is still alive — kill the root first and the descendants
        # re-parent away from this PID and survive.
        try:
            _pid = self._pid
        except (AttributeError, RuntimeError):
            _pid = None
        _tree_kill(_pid)
        try:
            _proc = self._proc
        except (AttributeError, RuntimeError):
            _proc = None
        if _proc is not None:
            try:
                _proc.terminate(force=True)  # unblocks reader's proc.read()
            except Exception:
                pass
        try:
            self._alive = False
        except (AttributeError, RuntimeError):
            pass
        # Bug-7 fix: join threads so they don't accumulate as zombies across
        # many close/respawn cycles.  500 ms timeout avoids blocking the UI
        # thread; if a thread hasn't exited by then we leave it — the process
        # kill above ensures it will exit momentarily on its own.
        if _writer is not None:
            _writer.quit()
            _writer.wait(500)
        if _reader is not None:
            _reader.quit()
            _reader.wait(500)
        try:
            _transcript = self._transcript
        except (AttributeError, RuntimeError):
            _transcript = None
        if _transcript is not None:
            try:
                _transcript.close()
            except Exception:
                pass
            try:
                self._transcript = None
            except (AttributeError, RuntimeError):
                pass

    @property
    def is_alive(self) -> bool:
        return self._alive

    # ──────────────────────────────────────────────────────────────
    # screen access
    # ──────────────────────────────────────────────────────────────
    def display_lines(self) -> list[str]:
        """Return the visible screen as a list of rows (top → bottom)."""
        with self._screen_lock:
            return _safe_screen_display(self.screen)

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

        Handles claude, codex, and gemini(agy) panes:
          - claude: bottom hint 'bypass permissions' or 'shift+tab to cycle',
                    never 'esc to interrupt' (working) or trust modal.
          - codex:  splash banner 'openai codex (v' visible, no modal
                    (`update available!`, `do you trust`, `press enter
                    to continue`) and no active interrupt indicator.
          - gemini: now the Antigravity `agy` TUI — idle footer
                    '? for shortcuts' visible, no active 'esc to
                    interrupt/cancel' indicator. (The legacy Gemini CLI
                    marker 'type your message or @path' is kept for
                    backward-compat.) Without a matching idle marker the
                    watchdog never fires (root cause of 'gemini forgot
                    takkub done' incidents 2026-05-20).
        """
        # Detection markers + their precedence live in the central table
        # (_READY_HARD_BLOCKERS / _READY_RULES) so an upstream reword is a
        # one-line patch and `takkub doctor` can self-test them. See M4#17.
        # Scoped to the bottom footer/status region (_ready_region) so a marker
        # string quoted in the conversation body can't poison the verdict — the
        # #70 false-busy stall / #20 fragility root fix.
        return _classify_ready(_ready_region(self.display_lines()))

    def shows_pending_input(self, fragment: str = "") -> bool:
        """True when the bottom input region holds unsent content.

        After a bracketed paste claude renders a ``[Pasted text +N lines]``
        placeholder (or, for short inline content, the literal text) in its
        input box. Detecting this lets the delivery self-heal tell a swallowed
        *Enter* (content present but not submitted — #22) apart from a swallowed
        *paste* (input box empty — #26): the first needs a CR resend, the second
        needs the payload re-pasted (a CR resend can't recover a missing paste).
        Scoped to the same bottom footer/input region as is_at_ready_prompt() so
        conversation-body text quoting the content can't poison the verdict. (#79)
        """
        return _input_has_content(_ready_region(self.display_lines()), fragment)

    def is_at_update_splash(self) -> bool:
        """True when a codex 'update available!' startup splash is blocking the prompt.

        The codex splash modal ('update available! run npm i -g @openai/codex')
        prevents the CLI from reaching its ready state.  Distinguished from the
        passive Gemini update footer ('gemini cli update available!') which is
        already classified ready=True by _READY_RULES and must NOT match here.

        Caller note: this is only meaningful when is_at_ready_prompt() is False;
        pairing both ensures the splash, not some other block, is the cause. (#62)

        Scoped to the bottom region (_ready_region) for the same reason as
        is_at_ready_prompt: a conversation that merely mentions "update
        available!" must not read as a live splash. (#70/#20)
        """
        text = _ready_region(self.display_lines())
        return "update available!" in text and "gemini cli update available!" not in text

    def seconds_since_output(self) -> float:
        """Monotonic seconds since the PTY last produced output — a structural
        idle/busy signal that does NOT depend on TUI text wording (#20).

        A generating CLI streams output continuously (animated spinner + token
        stream), so a large value means the pane has gone quiet. Used to
        corroborate text-marker detection and, in the orchestrator, to flag a
        pane that is quiet-but-unrecognised (the signature of an upstream prompt
        reword that silently broke the markers). Returns ``inf`` before any
        output has been seen."""
        ts = self._last_output_ts
        if not ts:
            return float("inf")
        return max(0.0, time.monotonic() - ts)

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

    def is_blocked_on_tty_prompt(self) -> str | None:
        """Return the first matching line if the pane is stuck on an interactive
        shell prompt (y/N, credential, 'press any key'); else ``None``.

        Scans the ``_TTY_PROMPT_TAIL_ROWS`` rows ending at the current cursor
        row.  Using the cursor position (rather than a fixed bottom-of-screen
        slice) means:
        - In a fresh/short session the cursor is near the top (row 0–2) and the
          scan window follows it there, so short test screens work correctly.
        - Older content that scrolled above the cursor window does NOT trigger
          false-positives even if it happened to contain one of the patterns
          (e.g. command output that printed 'Are you sure' in its own text).

        The screen and cursor are sampled under a single lock acquisition so
        the two reads are consistent.  The returned string is the stripped
        content of the matched line, suitable for watchdog log messages.
        """
        with self._screen_lock:
            lines = _safe_screen_display(self.screen)
            cursor_row = self.screen.cursor.y
        if not lines:
            return None
        lo = max(0, cursor_row - _TTY_PROMPT_TAIL_ROWS + 1)
        hi = cursor_row + 1
        for line in reversed(lines[lo:hi]):
            if _TTY_PROMPT_RE.search(line):
                return line.strip() or "interactive prompt detected"
        return None

    def has_unparsed_tool_call(self) -> str | None:
        """Return the first line containing a literal tool-call XML tag if one is
        visible near the cursor; else ``None``.

        A well-formed tool call is consumed by the Claude Code harness before it
        ever reaches the terminal renderer, so it never appears as plain text on
        screen.  If one of the recognised tag patterns IS visible, the tool call
        was malformed (missing ``antml:`` prefix or otherwise unparseable) and was
        silently no-op'd by the harness — the model appears to hang even though
        no real hang occurred.

        Scans the ``_MALFORMED_XML_TAIL_ROWS`` rows ending at the current cursor
        row (same cursor-relative window used by ``is_blocked_on_tty_prompt``).
        The screen and cursor are sampled under a single lock acquisition.  The
        returned string is the stripped content of the first matched line,
        suitable for watchdog log messages.
        """
        with self._screen_lock:
            lines = _safe_screen_display(self.screen)
            cursor_row = self.screen.cursor.y
        if not lines:
            return None
        lo = max(0, cursor_row - _MALFORMED_XML_TAIL_ROWS + 1)
        hi = cursor_row + 1
        for line in reversed(lines[lo:hi]):
            if _MALFORMED_XML_RE.search(line):
                return line.strip() or "unparsed tool-call XML detected"
        return None
