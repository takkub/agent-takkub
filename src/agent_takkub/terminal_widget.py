"""TerminalWidget: QWebEngineView hosting xterm.js for true terminal fidelity.

This replaces the Iter 1–9 QPlainTextEdit + pyte rendering pipeline. xterm.js
is the same terminal emulator VS Code, Hyper, GitHub Codespaces and Cursor
use; it handles ANSI, alt-screen, mouse modes, IME composition, BiDi text,
and Thai/CJK combining marks natively via the browser layout engine.

Wiring:
  PTY bytes (from PtySession reader thread)
    → terminal_widget.write_bytes(data)
    → page.runJavaScript("termWrite(...)")
    → xterm.js renders

  User keystroke / IME commit (inside xterm.js)
    → bridge.sendInput(data)        [QWebChannel slot]
    → emit inputBytes(bytes)        [Qt signal]
    → AgentPane → Orchestrator → PtySession.write()

  Resize:
    xterm.js FitAddon → bridge.resize(cols, rows)
    → emit resized(cols, rows)      [Qt signal]
    → PtySession.resize() → winpty.setwinsize()
"""

from __future__ import annotations

import base64
import codecs
import json
import re
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QVBoxLayout, QWidget

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_URL = QUrl.fromLocalFile(str(_STATIC_DIR / "terminal.html"))

_CLIPBOARD_KEEP = 50  # max clipboard-*.png files kept in runtime/


# ---------------------------------------------------------------------------
# Pure helpers — no Qt required; tested directly in test_image_input.py
# ---------------------------------------------------------------------------


def _normalize_path(raw: str) -> str:
    """Convert backslashes to forward slashes for cross-tool compatibility."""
    return raw.replace("\\", "/")


def _format_drop_paths(local_paths: list[str]) -> str:
    """Format a list of file-system paths for insertion into the terminal."""
    return " ".join(_normalize_path(p) for p in local_paths if p)


def _cleanup_clipboard_images(runtime_dir: Path, keep: int = _CLIPBOARD_KEEP) -> list[Path]:
    """Remove oldest clipboard-*.png files, keeping only `keep` most recent.

    Returns the list of deleted paths (useful for tests and logging).
    """
    images = sorted(
        runtime_dir.glob("clipboard-*.png"),
        key=lambda p: p.stat().st_mtime,
    )
    to_delete = images[: max(0, len(images) - keep)]
    for old in to_delete:
        try:
            old.unlink()
        except Exception:
            pass
    return to_delete


def _save_clipboard_image(b64data: str, runtime_dir: Path) -> Path:
    """Decode base64 image data and write to runtime/clipboard-<ISO-ts>.png."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    path = runtime_dir / f"clipboard-{ts}.png"
    path.write_bytes(base64.b64decode(b64data))
    return path


_TRAILING_PUNCT = ".,;:!?)]}>\"'`"


def _resolve_open_path(
    raw: str, cwd: str | None = None, extra_bases: tuple[str, ...] = ()
) -> Path | None:
    """Resolve a clicked terminal token to an existing file path, or None.

    Pure (no Qt) so it can be unit-tested. Absolute paths are checked
    directly; relative paths are tried against `cwd` (the pane's project
    dir) then each `extra_bases` entry (e.g. the cockpit repo root). The
    first candidate that exists wins. Surrounding quotes/brackets and
    trailing sentence punctuation are stripped first so a path printed mid
    sentence ("see docs/x.md.") still resolves.
    """
    if not raw:
        return None
    s = raw.strip().strip("\"'`").strip("()[]{}<>").rstrip(_TRAILING_PUNCT)
    if not s:
        return None
    try:
        p = Path(s)
        if p.is_absolute():
            return p if p.exists() else None
        bases: list[Path] = []
        if cwd:
            bases.append(Path(cwd))
        bases.extend(Path(b) for b in extra_bases if b)
        for base in bases:
            cand = base / s
            if cand.exists():
                return cand
    except OSError:
        return None
    return None


# Extensions that the OS would EXECUTE (not view) when handed to the default
# "open" verb — a clicked path to one of these is a code-exec vector if a pane
# can lure the user into clicking it. We reveal-in-folder instead of opening.
# `.js`/`.sh` are intentionally excluded: they're far more common as source the
# user legitimately wants to open in an editor, and on Windows `.sh` opens in an
# editor rather than executing. (M3#13)
_EXEC_EXTS = frozenset(
    {
        ".exe",
        ".com",
        ".scr",
        ".pif",
        ".bat",
        ".cmd",
        ".ps1",
        ".psm1",
        ".hta",
        ".lnk",
        ".msi",
        ".msp",
        ".vbs",
        ".vbe",
        ".wsf",
        ".wsh",
        ".jse",
        ".reg",
        ".cpl",
        ".msc",
        ".jar",
        ".gadget",
        ".inf",
    }
)


def _is_exec_path(p: Path) -> bool:
    """True if opening `p` with the OS default app would execute it (M3#13)."""
    return p.suffix.lower() in _EXEC_EXTS


# OSC 52 = "set clipboard": ESC ] 52 ; <selection> ; <base64> (BEL | ST). A pane
# emitting this can silently overwrite the user's system clipboard. xterm.js
# registers no OSC 52 handler today, but allowProposedApi is on, so we strip it
# from outbound render text as defense-in-depth. The data field is base64 +
# punctuation (never a control byte), so `[^\x07\x1b]*` safely spans it and stops
# at the BEL / ST terminator. (M3#14)
_OSC52_COMPLETE = re.compile(r"\x1b\]52;[^\x07\x1b]*(?:\x07|\x1b\\)")
# Cap on how long a trailing *incomplete* OSC 52 we'll hold back waiting for its
# terminator — beyond this a misbehaving program isn't really mid-sequence.
_OSC52_CARRY_MAX = 8192


def _strip_osc52(text: str) -> tuple[str, str]:
    """Strip complete OSC 52 clipboard-set sequences from outbound terminal text.

    Returns ``(cleaned, carry)``. ``carry`` is a trailing INCOMPLETE OSC 52
    (started but not yet terminated) that the caller should prepend to the next
    flush batch, so a sequence split across flushes is still filtered rather than
    leaking its head to the renderer. Pure / no Qt → unit-tested. (M3#14)
    """
    cleaned = _OSC52_COMPLETE.sub("", text)
    idx = cleaned.rfind("\x1b]52;")
    if idx != -1:
        rest = cleaned[idx:]
        # Incomplete only if no terminator appears after the start marker.
        if "\x07" not in rest and "\x1b\\" not in rest and len(rest) <= _OSC52_CARRY_MAX:
            return cleaned[:idx], rest
    return cleaned, ""


def _within_allowed_bases(p: Path, cwd: str | None, extra_bases: tuple[str, ...]) -> bool:
    """True if `p` resolves to somewhere inside the pane cwd or one of the
    allowed base dirs. Clicked paths that escape every allowed subtree (an
    absolute path elsewhere on disk, or a `../../` traversal) are refused so a
    pane can't lure a click onto an arbitrary file. (M3#13)
    """
    bases: list[Path] = []
    if cwd:
        bases.append(Path(cwd))
    bases.extend(Path(b) for b in extra_bases if b)
    try:
        rp = p.resolve()
    except OSError:
        return False
    for base in bases:
        try:
            rb = base.resolve()
        except OSError:
            continue
        if rp == rb or rp.is_relative_to(rb):
            return True
    return False


class _Bridge(QObject):
    """Object exposed to JS via QWebChannel."""

    inputData = pyqtSignal(str)  # text the user typed in xterm.js
    sizeChanged = pyqtSignal(int, int)  # cols, rows reported by FitAddon
    pageReady = pyqtSignal()
    imageDataPasted = pyqtSignal(str, str)  # base64_data, mime_type
    openUrlRequested = pyqtSignal(str)  # web URL clicked in a pane
    openPathRequested = pyqtSignal(str)  # file path clicked in a pane
    fontZoomedSig = pyqtSignal(int)  # pt size after a Ctrl/Cmd+wheel zoom or Ctrl/Cmd+0 reset

    @pyqtSlot(str)
    def sendInput(self, data: str) -> None:
        self.inputData.emit(data)

    @pyqtSlot(str)
    def openUrl(self, uri: str) -> None:
        """Called from JS when the user clicks a web link (WebLinksAddon)."""
        self.openUrlRequested.emit(uri)

    @pyqtSlot(str)
    def openPath(self, path: str) -> None:
        """Called from JS when the user clicks a file path (custom provider)."""
        self.openPathRequested.emit(path)

    @pyqtSlot(int, int)
    def resize(self, cols: int, rows: int) -> None:
        self.sizeChanged.emit(cols, rows)

    @pyqtSlot(int)
    def fontZoomed(self, pt: int) -> None:
        """Called from JS after a Ctrl/Cmd+wheel zoom or Ctrl/Cmd+0 reset."""
        self.fontZoomedSig.emit(pt)

    @pyqtSlot()
    def ready(self) -> None:
        self.pageReady.emit()

    @pyqtSlot(str, str)
    def pasteImageData(self, b64data: str, mime_type: str) -> None:
        """Called from JS when the user pastes an image (Ctrl+V or context menu)."""
        self.imageDataPasted.emit(b64data, mime_type)


class TerminalWidget(QWidget):
    """xterm.js-backed terminal that drops into an AgentPane.

    Public signals match the v0.2.x QPlainTextEdit-based widget so AgentPane
    can stay nearly identical:

      inputBytes(bytes)        — user typed something; forward to PTY
      resized(cols, rows)      — terminal grid size changed; resize PTY
      fontSizeChanged(int)     — for QSettings per-role persistence
    """

    inputBytes = pyqtSignal(bytes)
    resized = pyqtSignal(int, int)
    fontSizeChanged = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._view = QWebEngineView(self)
        layout.addWidget(self._view, 1)

        self._channel = QWebChannel(self)
        self._bridge = _Bridge(self)
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)

        # Stateful UTF-8 decoder: buffers partial multi-byte sequences across
        # PTY read chunks so Thai/CJK chars split at chunk boundaries are not
        # corrupted into replacement chars (U+FFFD).
        self._utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        # buffer bytes until xterm.js says it's ready, then flush in order
        self._pending_writes: list[str] = []
        self._page_ready = False

        # Input lock: when True, every USER-originated input (keystrokes, image
        # paste, file drop) is dropped before it reaches the PTY. The
        # orchestrator's own writes go straight to PtySession.write() and are
        # unaffected — only manual typing is gated. Teammate panes default to
        # locked so an accidental keypress can't derail an agent the cockpit is
        # driving; the Lead pane stays unlocked (it's the user's command surface).
        self._input_locked = False

        # Coalesce multiple write_bytes() calls within the same event-loop
        # tick into a single runJavaScript IPC roundtrip. Each PTY chunk
        # used to fire its own runJavaScript — for chatty TUIs that meant
        # dozens of IPC hops per frame and visible cursor jitter.
        self._write_buf: list[str] = []
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(0)
        self._flush_timer.timeout.connect(self._flush_writes)

        # Heartbeat: Chromium throttles RAF / paint on Windows when a
        # WebEngineView is not currently the focused widget. Symptom: PTY
        # output is delivered to xterm.js (term.write runs) but the DOM
        # paint never happens until the user types or moves the mouse, so
        # the "last frame after claude finishes" is stuck stale. A periodic
        # no-op runJavaScript keeps the JS context warm and forces a paint
        # tick so the stalled frame surfaces on its own. Pair this with the
        # in-page requestAnimationFrame self-loop in terminal.html and the
        # Chromium flags set in app.py for belt-and-suspenders coverage.
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(250)
        self._heartbeat.timeout.connect(self._heartbeat_poke)

        # Whether this pane's tab is currently visible. A pane in a HIDDEN
        # project tab is never on screen, so we suspend its paint keep-alive
        # (heartbeat + in-page rAF pulse) to let Chromium reclaim the
        # renderer's compositor memory. Default True — the tab that's active
        # at startup never receives a "switched to" event. Re-asserted in
        # _on_page_ready so a tab hidden before its page finished booting
        # still starts suspended.
        self._keepalive = True

        # Pane cwd (set by AgentPane.attach_session) so clicked relative
        # paths resolve against the project this pane is working in.
        self._cwd: str | None = None

        self._bridge.inputData.connect(self._on_input_data)
        self._bridge.sizeChanged.connect(self.resized.emit)
        self._bridge.pageReady.connect(self._on_page_ready)
        self._bridge.imageDataPasted.connect(self._on_image_pasted)
        self._bridge.openUrlRequested.connect(self._on_open_url)
        self._bridge.openPathRequested.connect(self._on_open_path)
        self._bridge.fontZoomedSig.connect(self._on_font_zoomed)

        # Enable drag-and-drop for file path insertion (Level 1).
        # We install an event filter on the child view so we see Qt-level
        # drag events before Chromium/WebEngine processes them.
        self._view.installEventFilter(self)
        self._view.setAcceptDrops(True)

        self._view.load(_INDEX_URL)

    # ------------------------------------------------------------------
    # Python → JS
    # ------------------------------------------------------------------
    def write_bytes(self, data: bytes | str) -> None:
        """Forward PTY output to xterm.js (batched per event-loop tick)."""
        if isinstance(data, bytes):
            text = self._utf8_decoder.decode(data)
        else:
            text = data
        if not text:
            return
        if not self._page_ready:
            self._pending_writes.append(text)
            return
        self._write_buf.append(text)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_writes(self) -> None:
        if not self._write_buf:
            return
        joined = "".join(self._write_buf)
        self._write_buf.clear()
        # M3#14: drop OSC 52 clipboard-set escapes before they reach the renderer.
        # A trailing incomplete sequence is held back (re-buffered) and combined
        # with the next batch; the next write_bytes restarts the flush timer, so a
        # never-terminated partial simply never renders (harmless, no busy-loop).
        cleaned, carry = _strip_osc52(joined)
        if carry:
            self._write_buf.append(carry)
        if not cleaned:
            return
        self._view.page().runJavaScript(f"termWrite({json.dumps(cleaned)});")

    def clear(self) -> None:
        if not self._page_ready:
            self._pending_writes.clear()
            return
        self._view.page().runJavaScript("termClear();")

    def reset(self) -> None:
        """Wipe scrollback + pending writes when a session detaches but the
        widget itself lives on (Lead pane reused across project switches).
        Without this, xterm.js's scrollback from the previous session sits
        in Chromium memory and grows unbounded across N project switches.

        Heartbeat keeps running — the JS context is still alive and will
        host the next attached session shortly.
        """
        self._pending_writes.clear()
        self._write_buf.clear()
        self._utf8_decoder.reset()
        if self._flush_timer.isActive():
            self._flush_timer.stop()
        if self._page_ready:
            try:
                self._view.page().runJavaScript("termReset();")
            except Exception:
                pass

    def destroy_terminal(self) -> None:
        """Tear down for good: stop every timer, drop the WebEngine view.

        Distinct from `reset()` (which keeps the widget alive). Used when a
        teammate pane is being permanently removed from the UI so Chromium
        can release the renderer process and the JS heap. Qt's parent-chain
        deleteLater would eventually do most of this, but stopping timers
        explicitly avoids stray runJavaScript calls into a destroyed page.
        """
        # Uninstall the drag-drop event filter FIRST. Tearing the view down
        # (deleteLater / parent changes) can otherwise re-enter eventFilter on a
        # half-destroyed QWebEngineView — one of the reentrant paths into the
        # Qt6Core __fastfail (0xc0000409) hard-crash seen on every pane close.
        try:
            self._view.removeEventFilter(self)
        except Exception:
            pass
        for timer in (self._flush_timer, self._heartbeat):
            try:
                if timer.isActive():
                    timer.stop()
            except Exception:
                pass
        try:
            self._view.page().deleteLater()
        except Exception:
            pass
        try:
            self._view.deleteLater()
        except Exception:
            pass

    def set_font_point_size(self, size: int) -> None:
        size = max(8, min(24, int(size)))
        # xterm.js wants pixel size; rough conversion: pt * 1.333 ≈ px
        px = int(size * 1.333)
        self._view.page().runJavaScript(f"termSetFontSize({px});")
        self.fontSizeChanged.emit(size)

    def _on_font_zoomed(self, pt: int) -> None:
        """JS already applied the new size (Ctrl/Cmd+wheel or +0 reset) and
        refit the terminal; just propagate the resulting pt size so the host
        (AgentPane) persists it the same way as a menu-driven font change."""
        self.fontSizeChanged.emit(int(pt))

    def request_buffer_text(self, callback) -> None:
        """Async fetch the current visible+scrollback buffer as text.
        `callback(str)` is invoked once the JS resolves."""
        self._view.page().runJavaScript("termGetBufferText();", callback)

    def set_idle(self, idle: bool) -> None:
        """Tell xterm.js whether claude is sitting at the ready prompt
        (idle=True) or busy (idle=False). The terminal uses this flag to
        decide whether to local-echo keystrokes for snappier feedback.

        Best-effort: if the page hasn't booted, we drop the call (the
        default JS-side flag is `false` so we err on the safe side).
        """
        if not self._page_ready:
            return
        flag = "true" if idle else "false"
        try:
            self._view.page().runJavaScript(f"termSetIdle({flag});")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------
    def set_input_locked(self, locked: bool) -> None:
        """Lock/unlock USER input. Locked panes drop keystrokes, image pastes,
        and file drops before they reach the PTY (orchestrator writes are
        unaffected). Also tells xterm.js to drop local echo so a locked pane
        gives no typing feedback at all."""
        self._input_locked = bool(locked)
        if self._page_ready:
            try:
                self._view.page().runJavaScript(
                    f"termSetLocked({'true' if self._input_locked else 'false'});"
                )
            except Exception:
                pass

    def is_input_locked(self) -> bool:
        return self._input_locked

    def _on_input_data(self, data: str) -> None:
        # xterm.js gives us already-encoded escape sequences for keys; just
        # ship the bytes to the PTY — unless this pane is input-locked, in which
        # case the keystroke is dropped (accidental-input guard).
        if self._input_locked:
            return
        self.inputBytes.emit(data.encode("utf-8"))

    def _on_page_ready(self) -> None:
        self._page_ready = True
        # Re-assert the lock state now that JS can receive it — the initial
        # set_input_locked() at construction ran before the page existed.
        if self._input_locked:
            self.set_input_locked(True)
        if self._pending_writes:
            joined = "".join(self._pending_writes)
            self._pending_writes.clear()
            self._view.page().runJavaScript(f"termWrite({json.dumps(joined)});")
        # Start heartbeat once the page is alive — keeps the renderer warm
        # so stalled-frame bugs (output delivered but not painted) can't
        # accumulate while the user is looking at another pane. If this pane's
        # tab was hidden before the page booted, honour that now: stay
        # suspended (heartbeat off + JS pulse paused) so the renderer can
        # release memory.
        if self._keepalive:
            self._heartbeat.start()
        else:
            try:
                self._view.page().runJavaScript("termSetKeepalive(false);")
            except Exception:
                pass

    def _heartbeat_poke(self) -> None:
        if not self._page_ready:
            return
        # Cheap no-op that nonetheless forces Chromium to tick the JS task
        # queue and schedule a frame; xterm.js's render service will flush
        # any pending DOM writes on that tick.
        self._view.page().runJavaScript("void 0;")

    def set_keepalive(self, active: bool) -> None:
        """Enable/suspend the background paint keep-alive for this pane.

        Called by the host tab when it gains/loses visibility. A pane in a
        HIDDEN project tab is never on screen, so there is no stale-frame to
        guard against — we stop the 250 ms heartbeat and tell xterm.js to
        pause its rAF pulse so Chromium can reclaim the renderer's compositor
        memory (otherwise a backgrounded tab's renderer balloons to multi-GB
        because --disable-backgrounding-occluded-windows forbids reclamation
        while we keep forcing paints). Re-enabling restarts the heartbeat and
        forces one repaint so the latest buffer surfaces the instant the tab
        is shown again. The PTY keeps streaming into the xterm.js buffer the
        whole time — only painting is suspended, so no output is lost.
        """
        active = bool(active)
        self._keepalive = active
        if active:
            if self._page_ready and not self._heartbeat.isActive():
                self._heartbeat.start()
        else:
            if self._heartbeat.isActive():
                self._heartbeat.stop()
        if self._page_ready:
            try:
                self._view.page().runJavaScript(
                    f"termSetKeepalive({'true' if active else 'false'});"
                )
            except Exception:
                pass

    def setFocus(self) -> None:
        self._view.setFocus()

    def set_cwd(self, cwd: str | None) -> None:
        """Record the pane's working dir so clicked relative paths resolve."""
        self._cwd = cwd

    # ------------------------------------------------------------------
    # Clickable links: open URL / file path clicked inside a pane
    # ------------------------------------------------------------------
    def _on_open_url(self, uri: str) -> None:
        """Open a clicked web link in the OS default browser.

        WebLinksAddon's default handler uses window.open(), which QtWebEngine
        silently blocks (no createWindow override) — so links looked dead.
        Routing through QDesktopServices opens them in the real browser.
        """
        u = (uri or "").strip()
        # M3#13: drop file:// — a clicked file:// URL bypasses _on_open_path's
        # confinement + exec-extension guards and would hand an arbitrary local
        # path straight to the OS opener. Web/mail schemes only.
        if not u or not u.lower().startswith(("http://", "https://", "mailto:")):
            return
        QDesktopServices.openUrl(QUrl(u))
        self._log_link_event("open_url", u)

    def _on_open_path(self, raw: str) -> None:
        """Open a clicked file path with its OS default app (html→browser,
        md→editor, png→viewer). Relative paths resolve against the pane cwd
        first, then the cockpit repo root."""
        from .config import REPO_ROOT

        bases = (str(REPO_ROOT),)
        resolved = _resolve_open_path(raw, self._cwd, bases)
        if resolved is None:
            self._log_link_event("open_path_miss", raw)
            return
        # M3#13: refuse paths that escape the pane cwd / repo subtree — a pane
        # could otherwise print a clickable absolute path to anywhere on disk.
        if not _within_allowed_bases(resolved, self._cwd, bases):
            self._log_link_event("open_path_outside", str(resolved))
            return
        # M3#13: never hand an executable to the OS "open" verb (it would run it).
        # Reveal it in the file manager instead so the user still finds the file.
        if _is_exec_path(resolved):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(resolved.parent)))
            self._log_link_event("open_path_exec_revealed", str(resolved))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(resolved)))
        self._log_link_event("open_path", str(resolved))

    def _log_link_event(self, kind: str, value: str) -> None:
        from .config import EVENTS_LOG, ensure_runtime

        try:
            ensure_runtime()
            with EVENTS_LOG.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now().isoformat()} {kind} {value}\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Level 1: Drag-drop file paths into pane
    # ------------------------------------------------------------------
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Intercept drag-drop events on the WebView to insert file paths.

        Returns True (consumed) for file-URL drag events so Chromium doesn't
        try to open them as a page load.
        """
        if watched is self._view:
            t = event.type()
            if t == QEvent.Type.DragEnter:
                if event.mimeData().hasUrls():  # type: ignore[attr-defined]
                    event.acceptProposedAction()  # type: ignore[attr-defined]
                    self._set_drop_highlight(True)
                    return True
            elif t == QEvent.Type.DragMove:
                if event.mimeData().hasUrls():  # type: ignore[attr-defined]
                    event.acceptProposedAction()  # type: ignore[attr-defined]
                    return True
            elif t == QEvent.Type.DragLeave:
                self._set_drop_highlight(False)
                return True
            elif t == QEvent.Type.Drop:
                self._set_drop_highlight(False)
                if self._input_locked:
                    return True  # locked pane: swallow the drop, forward nothing
                if event.mimeData().hasUrls():  # type: ignore[attr-defined]
                    local_paths = [
                        url.toLocalFile()
                        for url in event.mimeData().urls()  # type: ignore[attr-defined]
                        if url.isLocalFile()
                    ]
                    if local_paths:
                        self.inputBytes.emit(_format_drop_paths(local_paths).encode("utf-8"))
                    event.acceptProposedAction()  # type: ignore[attr-defined]
                    return True
        return super().eventFilter(watched, event)

    def _set_drop_highlight(self, active: bool) -> None:
        if active:
            self._view.setStyleSheet("border: 2px solid #3b82f6;")
        else:
            self._view.setStyleSheet("")

    # ------------------------------------------------------------------
    # Level 2: Ctrl+V image-from-clipboard
    # ------------------------------------------------------------------
    def _on_image_pasted(self, b64data: str, _mime_type: str) -> None:
        """Receive base64 image from JS, save to disk, insert path into terminal."""
        if self._input_locked:
            return  # locked pane: ignore pasted images
        from .config import EVENTS_LOG, RUNTIME_DIR, ensure_runtime

        ensure_runtime()
        try:
            img_path = _save_clipboard_image(b64data, RUNTIME_DIR)
        except Exception as exc:
            try:
                with EVENTS_LOG.open("a", encoding="utf-8") as fh:
                    fh.write(f"{datetime.now().isoformat()} image_paste_error {exc}\n")
            except Exception:
                pass
            return

        _cleanup_clipboard_images(RUNTIME_DIR)

        fwd = _normalize_path(str(img_path))
        self.inputBytes.emit(fwd.encode("utf-8"))

        try:
            with EVENTS_LOG.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now().isoformat()} image_paste {fwd}\n")
        except Exception:
            pass
