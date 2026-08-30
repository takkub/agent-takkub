"""http_server.py — stdlib HTTP + SSE in a background thread (§6.2, X-check
3.1/4.1). Never runs on the Qt main thread: `http.server.ThreadingHTTPServer`
accepts and handles every connection on its own worker threads so a tunnel
scanner flooding the loopback port can never freeze the cockpit GUI.

Handler threads only ever touch `AuthGate` (thread-safe) and `SSEBroadcaster`
(thread-safe). Once a request has cleared secret-path + token/ticket auth,
it is marshalled onto the Qt main thread through `_Bridge` — a QObject whose
`request` signal is emitted from the handler thread and auto-queued for
delivery on the thread that owns it (the same cross-thread pattern
`pty_session.py`'s PTY-reader thread uses for `bytesIn`). No handler thread
ever constructs a QWidget or reaches into Orchestrator/pane state directly
(X-check H1's ownership rule).
"""

from __future__ import annotations

import http.server
import json
import logging
import queue
import socketserver
import threading
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from .. import config as _config
from . import api, reports
from .auth import AuthGate
from .config import RemoteConfig

_log = logging.getLogger(__name__)

# #445 follow-up: throttled events.log breadcrumb for every bare-404 rejection.
# Best-effort, never raises. Route is the path *after* the secret segment with
# the query string dropped (tickets/tokens live there).
_REJECT_LOG_MAX_PER_MIN = 60
_reject_log_window: list[float] = [0.0, 0.0]  # [window_start_epoch, count]
_reject_log_lock = threading.Lock()


def _log_reject(reason: str, raw_path: str) -> None:
    try:
        import os
        import time
        from datetime import datetime

        now = time.time()
        with _reject_log_lock:
            if now - _reject_log_window[0] >= 60.0:
                _reject_log_window[0] = now
                _reject_log_window[1] = 0.0
            if _reject_log_window[1] >= _REJECT_LOG_MAX_PER_MIN:
                return
            _reject_log_window[1] += 1
        path_only = raw_path.split("?", 1)[0]
        segments = path_only.split("/", 2)
        route = "/" + segments[2] if len(segments) > 2 else "/"
        if len(route) > 120:
            route = route[:120] + "..."
        events_log = _config.EVENTS_LOG
        try:
            events_log.parent.mkdir(parents=True, exist_ok=True)
            if events_log.exists() and events_log.stat().st_size > 5_000_000:
                os.replace(events_log, events_log.parent / (events_log.name + ".old"))
        except OSError:
            pass
        line = json.dumps(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "event": "remote_reject",
                "reason": reason or "unspecified",
                "route": route,
            },
            ensure_ascii=False,
        )
        with open(events_log, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except Exception:
        pass


_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_MAX_BODY_BYTES = 64 * 1024
_MAX_IMAGE_BODY_BYTES = 12 * 1024 * 1024
_BRIDGE_TIMEOUT_SEC = 8.0
_MAX_PORT_SCAN = 50
_MAX_SSE_CLIENTS = 6
_SSE_QUEUE_MAXSIZE = 200
_SSE_KEEPALIVE_SEC = 15.0
_SSE_WRITE_TIMEOUT_SEC = 10.0

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".json": "application/json; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


def _content_type(suffix: str) -> str:
    return _CONTENT_TYPES.get(suffix, "application/octet-stream")


# L2: the PWA shell has no inline <script> (only <script src="app.js">) and
# its markdown renderer is XSS-safe by construction, but a network-exposed
# page that innerHTMLs Lead-authored text should carry a CSP as
# defense-in-depth against any future renderer regression.
_CSP_HEADER = (
    "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; "
    "img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'"
)

# #367 Remote Reports (`/r/` route): a report is a standalone document.
# `publish()`/`validate_standalone_html` (`reports.py`) reject the external
# reference shapes it knows to look for at store time, but that regex-based
# check is a secondary gate, not the enforcement — this CSP is what actually
# blocks a report from reaching a third-party origin at render/fetch time
# (default-src 'self' catches anything the store-time check misses via
# directive fallback), which is why inline script/style can safely be
# allowed here (unlike the shell's `_CSP_HEADER` above).
_REPORT_CSP_HEADER = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; object-src 'none'; "
    "base-uri 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'"
)

_REPORT_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    # #389 — always served with Content-Disposition: attachment (see
    # `_report_headers` below), so the Content-Type here is a courtesy for
    # the downloaded file's association, not a render decision.
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
    ".csv": "text/csv; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


_FILENAME_SAFE_CHARS: dict[str, str] = {
    ch: ch for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
}


def _content_disposition_attachment(name: str) -> str:
    """#389: build the `Content-Disposition` header for a forced-download
    report. `name` is always a value that already passed `reports.
    _validate_report_name` (letters/digits/`._-` only — no quotes, no CR/LF,
    no path separators), so no separate escaping step could let it inject a
    second header or break out of the quoted filename; this only guards
    against that invariant ever changing without this header changing too."""
    # CodeQL #41/#42 (py/http-response-splitting): rebuild the filename
    # character-by-character from the constant `_FILENAME_SAFE_CHARS` table
    # — every output char is a dict VALUE from that table (never the input
    # char itself), so anything outside `[A-Za-z0-9._-]` (CR/LF, quotes,
    # separators, unicode) becomes `_` AND no request-derived string reaches
    # the header at all. (`re.sub` did the same filtering but CodeQL does not
    # treat it as a taint barrier — alert #42 re-fired on the same line.)
    safe = "".join(_FILENAME_SAFE_CHARS.get(ch, "_") for ch in name[:120]) or "report"
    return f'attachment; filename="{safe}"'


@dataclass
class _PendingRequest:
    action: str
    params: dict
    reply: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))


class _Bridge(QObject):
    """The only door a handler thread has into Orchestrator. Constructed on
    the Qt main thread; `request` is emitted from arbitrary handler
    threads and Qt auto-queues delivery of `_handle` onto the main thread.

    M-5: `pulse`/`lead_say`/`answer_picker` do real loopback-socket I/O to
    `cli_server` (up to several seconds under load) — that must never block
    the Qt event loop. They never touch Orchestrator/pane state directly
    (only the already-thread-safe `orch._lead_token` attribute), so the
    actual socket call is kicked off on a throwaway worker thread instead of
    running inline here. `answer_picker` additionally does a synchronous
    JSONL re-read (`notify.current_ask_state`) before that socket call — off
    the Qt thread for the same reason. Their client-supplied `project` param
    is still
    resolved inline first (`_resolve_scoped_project`, a cheap in-process
    read) so the open-tabs check happens on the Qt thread before the
    worker thread ever starts. `projects`/`sse_ticket`/`open`/`lead_history`/
    `lead_sessions`/`lead_resume`/`activity` stay fully inline: cheap,
    in-process work (a single project-scoped JSONL read for `lead_history`, a
    JSONL-root scan for `lead_sessions`, a direct pane-registry read for
    `activity`) that DOES need the Qt-thread ownership guarantee (the same
    thread that writes `projects.json` on tab switch/import, and the only
    thread allowed to touch `main_window`/pane state — `lead_resume` calls
    `orch.close`/`orch.spawn` directly, same ownership rule as `open`/`close`).
    """

    request = pyqtSignal(object)

    _OFF_THREAD_ACTIONS = frozenset({"pulse", "lead_say", "lead_upload", "answer_picker"})

    def __init__(self, orch) -> None:
        super().__init__()
        self._orch = orch
        self.request.connect(self._handle)

    def _resolve_scoped_project(self, requested: object) -> str:
        """Validate a client-supplied project name against the open tabs
        (project picker) — read here, on the Qt main thread, the same
        ownership rule `api.py` documents for every other `projects.json`
        touch. Anything not currently open (missing, wrong type, stale,
        or a forged name) falls back to the orchestrator's active project,
        so a client can never scope a request to a project it can't
        already see in `/api/projects`."""
        if isinstance(requested, str) and requested in _config.get_open_tabs():
            return requested
        return self._orch._resolve_project(None)

    def _handle(self, pending: _PendingRequest) -> None:
        try:
            if pending.action in self._OFF_THREAD_ACTIONS:
                pending.params["project"] = self._resolve_scoped_project(
                    pending.params.get("project")
                )
                threading.Thread(target=self._run_off_thread, args=(pending,), daemon=True).start()
                return
            if pending.action == "projects":
                mode = pending.params.get("mode", "view")
                pending.reply.put((200, api.projects(None, mode)))
            elif pending.action == "sse_ticket":
                project_ns = self._resolve_scoped_project(pending.params.get("project"))
                pending.reply.put((200, {"project_ns": project_ns}))
            elif pending.action == "open":
                try:
                    pending.reply.put(
                        (200, api.open_project(self._orch, pending.params.get("project")))
                    )
                except api.RemoteApiError as exc:
                    pending.reply.put((exc.status, {"ok": False, "msg": exc.msg}))
            elif pending.action == "close":
                try:
                    pending.reply.put(
                        (200, api.close_project(self._orch, pending.params.get("project")))
                    )
                except api.RemoteApiError as exc:
                    pending.reply.put((exc.status, {"ok": False, "msg": exc.msg}))
            elif pending.action == "lead_history":
                project_ns = self._resolve_scoped_project(pending.params.get("project"))
                pending.reply.put(
                    (200, api.lead_history(self._orch, project_ns, pending.params.get("limit")))
                )
            elif pending.action == "lead_sessions":
                project_ns = self._resolve_scoped_project(pending.params.get("project"))
                pending.reply.put(
                    (200, api.lead_sessions(self._orch, project_ns, pending.params.get("limit")))
                )
            elif pending.action == "lead_resume":
                try:
                    pending.reply.put(
                        (
                            200,
                            api.resume_lead(
                                self._orch,
                                pending.params.get("project"),
                                pending.params.get("session_uuid"),
                            ),
                        )
                    )
                except api.RemoteApiError as exc:
                    pending.reply.put((exc.status, {"ok": False, "msg": exc.msg}))
            elif pending.action == "image_path":
                try:
                    pending.reply.put(
                        (
                            200,
                            api.lead_image_path(
                                pending.params.get("project"), pending.params.get("path")
                            ),
                        )
                    )
                except api.RemoteApiError as exc:
                    pending.reply.put((exc.status, {"ok": False, "msg": exc.msg}))
            elif pending.action == "activity":
                pending.reply.put((200, api.activity(self._orch)))
            elif pending.action == "usage":
                pending.reply.put((200, api.usage()))
            else:
                pending.reply.put((404, {"ok": False, "msg": "unknown action"}))
        except Exception:
            # A handler-thread request must never be able to take down the
            # Qt main loop — log and answer with a generic 500 instead.
            _log.exception("remote api dispatch failed: %s", pending.action)
            pending.reply.put((500, {"ok": False, "msg": "internal error"}))

    def _run_off_thread(self, pending: _PendingRequest) -> None:
        try:
            if pending.action == "pulse":
                pending.reply.put((200, api.pulse(self._orch, pending.params.get("project"))))
            elif pending.action == "lead_say":
                api.lead_say(
                    self._orch, pending.params.get("text", ""), pending.params.get("project")
                )
                pending.reply.put((200, {"ok": True}))
            elif pending.action == "lead_upload":
                result = api.lead_upload_image(
                    self._orch,
                    pending.params.get("data_url"),
                    pending.params.get("name"),
                    pending.params.get("caption"),
                    pending.params.get("project"),
                )
                pending.reply.put((200, result))
            elif pending.action == "answer_picker":
                result = api.answer_picker(
                    self._orch, pending.params.get("project"), pending.params.get("answers")
                )
                pending.reply.put((200, result))
        except api.RemoteApiError as exc:
            pending.reply.put((exc.status, {"ok": False, "msg": exc.msg}))
        except Exception:
            _log.exception("remote api dispatch failed: %s", pending.action)
            pending.reply.put((500, {"ok": False, "msg": "internal error"}))


_ALLOWED_SSE_EVENTS = frozenset(
    {
        "done",
        "lead",
        "user",
        "working",
        "idle",
        "blocked_on_picker",
        "session_changed",
        # #390: `takkub report publish --send` -> `Orchestrator.push_report`
        # -> `LeadNotifier._on_report_shared` — a report to render as a
        # native attachment card in the PWA feed.
        "report",
    }
)


def _force_wake(q: queue.Queue) -> None:
    """Put the `(None, None)` close/evict sentinel on `q`, guaranteed —
    if the queue is already full, drop its oldest entry first (same
    drop-oldest policy `SSEBroadcaster.push` uses) instead of silently
    discarding the sentinel and leaving the handler blocked until its next
    15s keepalive timeout."""
    while True:
        try:
            q.put_nowait((None, None))
            return
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                return


class SSEBroadcaster:
    """Fan-out for `/api/lead`. One bounded queue per connected client
    (finding B3): a full queue drops its oldest event instead of blocking
    the Qt-thread caller of `push`.

    Each client is registered with the project namespace its ticket was
    issued for (H-A) — `push` only ever reaches clients whose namespace
    matches the event's `project_ns`, so a `done`/`lead` event from one
    project can never leak into another project's mobile session.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[tuple[queue.Queue, str]] = []

    def register(self, project_ns: str) -> queue.Queue | None:
        # Never hard-503 at the cap. This is a single-user tool reached through
        # cloudflared, which keeps the *origin* TCP socket open after the phone
        # reloads or switches projects — so a full table is almost always dead
        # reconnects, not real concurrent viewers. Evict the oldest slot (wake
        # its handler with the close sentinel so it exits and unregisters) and
        # admit the newcomer instead of locking the user out with a 503.
        evicted: queue.Queue | None = None
        with self._lock:
            if len(self._clients) >= _MAX_SSE_CLIENTS:
                evicted, _ = self._clients.pop(0)
            q: queue.Queue = queue.Queue(maxsize=_SSE_QUEUE_MAXSIZE)
            self._clients.append((q, project_ns))
        if evicted is not None:
            _force_wake(evicted)
        return q

    def unregister(self, q: queue.Queue) -> None:
        with self._lock:
            self._clients = [(cq, ns) for cq, ns in self._clients if cq is not q]

    def push(self, event: str, data: str | dict, project_ns: str | None = None) -> None:
        """H-C: `data` is JSON-encoded before it ever reaches the wire, so a
        payload containing raw newlines can neither break SSE line framing
        nor inject a fake `event:`/`data:` line into the stream. `event` is
        checked against a fixed allowlist for the same reason.

        `data` is normally a plain string, wrapped as `{"text": data}` for the
        client's generic parser. B2's `blocked_on_picker` event instead needs
        a structured payload (prompt + option chips) — passing a `dict` sends
        it as-is, unwrapped."""
        if event not in _ALLOWED_SSE_EVENTS:
            return
        payload = json.dumps(data if isinstance(data, dict) else {"text": data}, ensure_ascii=False)
        with self._lock:
            clients = list(self._clients)
        for q, ns in clients:
            if project_ns is not None and ns != project_ns:
                continue
            while True:
                try:
                    q.put_nowait((event, payload))
                    break
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break

    def close_all(self) -> None:
        """M-4: wake every blocked SSE handler thread (each sits in a
        `q.get(timeout=...)`) so it notices the server's stop event and
        exits immediately instead of lingering until the next keepalive."""
        with self._lock:
            clients = list(self._clients)
        for q, _ns in clients:
            _force_wake(q)


class _RemoteHandler(http.server.BaseHTTPRequestHandler):
    server_version = "takkub-remote/1"
    # No keep-alive: bounds each API request to a single thread for its own
    # short lifetime (H4 — SSE is the only intentionally long-lived thread,
    # capped separately by _MAX_SSE_CLIENTS).
    protocol_version = "HTTP/1.0"
    # M1: BaseHTTPRequestHandler.timeout defaults to None, so a connection
    # trickling its request line/headers or body one byte at a time pins a
    # handler thread forever (pre-auth — no secret path/token needed). This
    # bounds every socket read (setup/request-line/headers/body) to 30s.
    timeout = 30

    def log_message(self, format: str, *args) -> None:
        # H3: BaseHTTPRequestHandler's default log echoes the full request
        # line — including a `?ticket=...` query string — to stderr. Never.
        pass

    # ── routing ──────────────────────────────────────────────────────────
    def _match_secret_path(self) -> tuple[str, dict] | None:
        """M-6: this only checks the secret path — it must NOT record idle
        activity. A wrong-token request that merely knows the secret path
        would otherwise keep resetting the idle-expire clock forever
        (`touch()` now only runs after bearer/ticket auth actually succeeds,
        see `_check_bearer`/`_handle_sse`)."""
        parsed = urllib.parse.urlsplit(self.path)
        segments = parsed.path.split("/", 2)
        if len(segments) < 2 or not self.server.auth.check_secret_path(segments[1]):
            return None
        rest = "/" + segments[2] if len(segments) > 2 else "/"
        return rest, dict(urllib.parse.parse_qsl(parsed.query))

    def _reject(self, reason: str = "") -> None:
        """§7.5: unauthenticated (wrong secret-path OR wrong token) always
        gets a bare 404 — never a 401, never a hint that anything exists.

        #445 follow-up: the PWA treats *any* bare 404 on `/api/*` as "token
        died" and wipes the pairing, and nothing on the cockpit side ever
        said which route/why. Every rejection now leaves a `remote_reject`
        breadcrumb in events.log (route path only — never the secret
        segment, query string, token or ticket) so the next "phone got
        logged out" report is diagnosable from the log instead of guessed.
        """
        _log_reject(reason, self.path)
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # API state must always come from the running cockpit. In particular,
        # a newly paired phone must not reuse another tab's stale history or
        # project list from an intermediary/browser cache.
        self.send_header("Cache-Control", "private, no-store")
        # L2: JSON isn't rendered as a document, but a CSP costs one header
        # line and closes the gap for any client that mis-sniffs the body.
        self.send_header("Content-Security-Policy", _CSP_HEADER)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _check_bearer(self) -> bool:
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else None
        if not self.server.auth.check_token(token):
            self._reject(
                "locked_out"
                if self.server.auth.is_locked_out()
                else ("no_bearer" if not token else "bad_token")
            )
            return False
        self.server.auth.touch()  # M-6: only a *successful* auth counts as activity
        return True

    def _drain_request_body(self) -> None:
        """Read and discard the request body, bounded to the largest size
        this route ever accepts (`_MAX_IMAGE_BODY_BYTES`). An oversized or
        malformed `Content-Length` is left unread — the socket close in
        that case races against a client that was never going to send a
        well-formed request anyway, so there's nothing worth draining."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < length <= _MAX_IMAGE_BODY_BYTES:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def do_GET(self) -> None:
        matched = self._match_secret_path()
        if matched is None:
            self._reject("bad_secret_path")
            return
        rest, query = matched
        if rest == "/api/lead":
            self._handle_sse(query)
        elif rest == "/api/bootstrap":
            # Password-gate preflight: after bearer auth, report whether this
            # browser still needs the optional third factor without making the
            # PWA probe three gated endpoints and leave expected 403s in the
            # console on every reload.
            if self._check_bearer():
                session_token = self.headers.get("X-Session")
                password_required = bool(
                    self.server.config.password_hash
                    and not self.server.auth.password_ok(session_token)
                )
                self._send_json(200, {"password_required": password_required})
        elif rest == "/api/pulse":
            if self._check_bearer() and self._check_password_gate():
                self._respond_marshaled("pulse", {"project": query.get("project")})
        elif rest == "/api/projects":
            if self._check_bearer() and self._check_password_gate():
                self._respond_marshaled("projects", {"mode": self.server.config.mode})
        elif rest == "/api/lead/history":
            if self._check_bearer() and self._check_password_gate():
                self._respond_marshaled(
                    "lead_history",
                    {"project": query.get("project"), "limit": query.get("limit")},
                )
        elif rest == "/api/lead/sessions":
            if self._check_bearer() and self._check_password_gate():
                self._respond_marshaled(
                    "lead_sessions",
                    {"project": query.get("project"), "limit": query.get("limit")},
                )
        elif rest == "/api/activity":
            if self._check_bearer() and self._check_password_gate():
                self._respond_marshaled("activity", {})
        elif rest == "/api/image":
            if self._check_bearer() and self._check_password_gate():
                self._serve_image(query)
        elif rest == "/api/usage":
            if self._check_bearer() and self._check_password_gate():
                self._respond_marshaled("usage", {})
        elif rest.startswith("/r/"):
            # #367 Remote Reports — deliberately NOT behind bearer/password
            # auth (see `_serve_report`'s docstring): a report link is meant
            # to be handed to someone who never gets cockpit credentials.
            self._serve_report(rest, query)
        elif rest.startswith("/api/"):
            self._reject("unknown_get_route")
        else:
            self._serve_static(rest)

    def do_POST(self) -> None:
        matched = self._match_secret_path()
        if matched is None:
            self._reject("bad_secret_path")
            return
        rest, _query = matched
        upload_authorized = False
        if rest == "/api/lead/upload":
            # Authenticate before accepting a multi-megabyte body. Other JSON
            # routes retain their small global cap; an unauthenticated tunnel
            # client can never make us buffer an image-sized request.
            if not self._check_bearer() or not self._check_password_gate():
                return
            if not self.server.auth.allows_control():
                # #206: this is the only forbidden-route branch that answers
                # before the unconditional `self.rfile.read(length)` below —
                # every other view-mode-forbidden route (lead/say, open,
                # close, lead/resume) drains the body first. HTTP/1.0 has no
                # keep-alive, so the handler closes the socket right after
                # this response; if the client's body is still unread in the
                # kernel receive buffer at that point, the OS can send an RST
                # instead of a clean close, which Windows surfaces to the
                # client as ConnectionAbortedError/WinError 10053 (racy,
                # proven flaky under full-suite load). Drain it first so the
                # close is always clean.
                self._drain_request_body()
                self._send_json(403, {"ok": False, "msg": "view mode: control is disabled"})
                return
            upload_authorized = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._reject("bad_content_length")
            return
        max_body = _MAX_IMAGE_BODY_BYTES if upload_authorized else _MAX_BODY_BYTES
        if not (0 <= length <= max_body):
            if upload_authorized:
                self._send_json(413, {"ok": False, "msg": "image too large"})
            else:
                self._reject("body_too_large")
            return
        body = self.rfile.read(length) if length else b""
        if rest == "/api/lead/upload":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"ok": False, "msg": "bad json"})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"ok": False, "msg": "bad json"})
                return
            self._respond_marshaled(
                "lead_upload",
                {
                    "data_url": payload.get("data_url"),
                    "name": payload.get("name"),
                    "caption": payload.get("caption"),
                    "project": payload.get("project"),
                },
            )
        elif rest == "/api/verify-password":
            if self._check_bearer():
                self._handle_verify_password(body)
        elif rest == "/api/sse-ticket":
            if self._check_bearer() and self._check_password_gate():
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload = {}
                requested = payload.get("project") if isinstance(payload, dict) else None
                self._issue_sse_ticket(requested)
        elif rest == "/api/lead/say":
            if not self._check_bearer() or not self._check_password_gate():
                return
            if not self.server.auth.allows_control():
                self._send_json(403, {"ok": False, "msg": "view mode: control is disabled"})
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"ok": False, "msg": "bad json"})
                return
            self._respond_marshaled(
                "lead_say", {"text": payload.get("text", ""), "project": payload.get("project")}
            )
        elif rest == "/api/lead/answer-picker":
            if not self._check_bearer() or not self._check_password_gate():
                return
            if not self.server.auth.allows_control():
                self._send_json(403, {"ok": False, "msg": "view mode: control is disabled"})
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"ok": False, "msg": "bad json"})
                return
            self._respond_marshaled(
                "answer_picker",
                {"answers": payload.get("answers"), "project": payload.get("project")},
            )
        elif rest == "/api/open":
            if not self._check_bearer() or not self._check_password_gate():
                return
            if not self.server.auth.allows_control():
                self._send_json(403, {"ok": False, "msg": "view mode: control is disabled"})
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"ok": False, "msg": "bad json"})
                return
            self._respond_marshaled("open", {"project": payload.get("project")})
        elif rest == "/api/close":
            if not self._check_bearer() or not self._check_password_gate():
                return
            if not self.server.auth.allows_control():
                self._send_json(403, {"ok": False, "msg": "view mode: control is disabled"})
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"ok": False, "msg": "bad json"})
                return
            self._respond_marshaled("close", {"project": payload.get("project")})
        elif rest == "/api/lead/resume":
            if not self._check_bearer() or not self._check_password_gate():
                return
            if not self.server.auth.allows_control():
                self._send_json(403, {"ok": False, "msg": "view mode: control is disabled"})
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"ok": False, "msg": "bad json"})
                return
            self._respond_marshaled(
                "lead_resume",
                {"project": payload.get("project"), "session_uuid": payload.get("session_uuid")},
            )
        else:
            self._reject("unknown_post_route")

    def _check_password_gate(self) -> bool:
        """Third auth factor (H1 fix): every authenticated route besides
        verify-password itself is blocked unless the request carries a
        live per-client session credential in `X-Session`, minted by a
        successful `/api/verify-password` POST — a bearer token alone
        (e.g. from a leaked pairing link) is never enough. `msg` is a
        stable literal the PWA matches on to show its password prompt
        instead of a generic error (never the pairing-URL/QR flow — the
        password never travels there)."""
        session_token = self.headers.get("X-Session")
        if self.server.auth.password_ok(session_token):
            return True
        self._send_json(403, {"ok": False, "msg": "password_required"})
        return False

    def _handle_verify_password(self, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "msg": "bad json"})
            return
        password = payload.get("password", "")
        if not self.server.config.password_hash:
            # No password configured — nothing to verify (backward
            # compatible with configs/tests predating this feature). Still
            # mint a session so the client's X-Session contract is uniform
            # regardless of whether a password is configured.
            session = self.server.auth.issue_password_session()
            self._send_json(200, {"ok": True, "session": session})
            return
        if not isinstance(password, str) or not self.server.auth.check_password(password):
            self._send_json(401, {"ok": False, "msg": "wrong password"})
            return
        session = self.server.auth.issue_password_session()
        self._send_json(200, {"ok": True, "session": session})

    def _bridge_call(self, action: str, params: dict) -> tuple[int, dict]:
        pending = _PendingRequest(action=action, params=params)
        self.server.bridge.request.emit(pending)
        try:
            return pending.reply.get(timeout=_BRIDGE_TIMEOUT_SEC)
        except queue.Empty:
            # H1: the Qt main thread never answered (e.g. mid-shutdown) —
            # give up instead of blocking this worker thread forever.
            return 504, {"ok": False, "msg": "orchestrator did not respond"}

    def _respond_marshaled(self, action: str, params: dict) -> None:
        status, payload = self._bridge_call(action, params)
        self._send_json(status, payload)

    def _serve_image(self, query: dict) -> None:
        """#424 `/api/image?path=<p>&project=<ns>` — stream one on-disk image
        referenced by a Lead message so the phone can render it inline.
        Bearer + password gated like every other `/api/*` read; the path is
        validated on the Qt main thread (`api.lead_image_path` — extension
        whitelist, must live under a project cwd or RUNTIME_DIR, magic bytes,
        size cap) and the bytes are read here on the worker thread. Any
        rejection is the same bare 404 as an unknown route."""
        status, payload = self._bridge_call(
            "image_path", {"project": query.get("project"), "path": query.get("path")}
        )
        if status != 200 or not payload.get("ok"):
            if status >= 500:
                self._send_json(status, payload)
            else:
                self._reject("image_unservable")
            return
        try:
            data = Path(str(payload["path"])).read_bytes()
        except (OSError, KeyError, TypeError):
            self._reject("image_read_failed")
            return
        self.send_response(200)
        self.send_header("Content-Type", str(payload.get("mime") or "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", "inline")
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def _issue_sse_ticket(self, requested_project: object = None) -> None:
        """H-A / project picker: stamp the ticket with `requested_project`
        if it names a project the user actually has open right now
        (validated on the Qt main thread via the bridge, same ownership
        rule as every other orchestrator touch) — otherwise fall back to
        whichever project is active. `_handle_sse` later scopes that
        client's events to this namespace for the life of the connection."""
        pending = _PendingRequest(action="sse_ticket", params={"project": requested_project})
        self.server.bridge.request.emit(pending)
        try:
            status, payload = pending.reply.get(timeout=_BRIDGE_TIMEOUT_SEC)
        except queue.Empty:
            status, payload = 504, {"ok": False, "msg": "orchestrator did not respond"}
        if status != 200:
            self._send_json(status, payload)
            return
        project_ns = payload.get("project_ns") or "default"
        ticket = self.server.auth.issue_ticket(project_ns)
        self._send_json(200, {"ticket": ticket})

    # ── Remote Reports (#367) ───────────────────────────────────────────
    def _serve_report(self, rest: str, query: dict) -> None:
        """`/<secret_path>/r/<project_ns>/<name>?k=<token>`. Still requires
        the secret path (stripped by `_match_secret_path` before this runs,
        same as every other route) but skips the bearer/password/session
        tiers entirely — its own per-file token substitutes for all three,
        checked with `hmac.compare_digest` inside `reports.resolve`. Wrong/
        missing/expired token and "no such report" all answer with the
        exact same 404 (§7.5 — never let a guess distinguish "exists" from
        "doesn't"), and a wrong token counts against its own lockout,
        scoped per (project_ns, name) (`AuthGate.record_report_token_result`
        — separate from `check_token`/`check_password`'s counters so a
        report guesser can never be reset by an unrelated successful bearer
        request, same H1 rationale; and separate per-report so a success
        against one report can never reset a guess streak against another).
        A request with no `k` at all doesn't count against the counter,
        mirroring `check_token`'s `if token:` guard — reaching this route
        already required the secret path, so a bare probe with the query
        string dropped shouldn't contribute to locking out the real
        recipient."""
        parts = rest.split("/", 3)  # ["", "r", "<project_ns>", "<name>"]
        if len(parts) != 4 or not parts[2] or not parts[3]:
            self._reject("report_bad_path")
            return
        project_ns, name = parts[2], parts[3]
        token = query.get("k")
        if self.server.auth.is_report_locked_out(project_ns, name):
            self._reject("report_locked_out")
            return
        path = reports.resolve(project_ns, name, token)
        if token:
            self.server.auth.record_report_token_result(project_ns, name, path is not None)
        if path is None:
            self._reject("report_not_found")
            return
        try:
            data = path.read_bytes()
        except OSError:
            self._reject("report_read_failed")
            return
        self.send_response(200)
        self.send_header(
            "Content-Type",
            _REPORT_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", _REPORT_CSP_HEADER)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Robots-Tag", "noindex")
        # #389: office/archive/plain-text extensions and anything published
        # with `--attachment` are always forced downloads — never rendered
        # inline, so widening the extension whitelist never grows what the
        # CSP above has to defend against.
        if reports.is_attachment(project_ns, name):
            self.send_header("Content-Disposition", _content_disposition_attachment(path.name))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    # ── static PWA shell ─────────────────────────────────────────────────
    def _serve_static(self, rest: str) -> None:
        """`rel` is attacker-controlled (URL path segment). Traversal is
        blocked by canonicalize-then-check-containment, the standard-safe
        pattern (not string-prefix filtering, which is bypassable by `..`,
        symlinks, and — on Windows — drive-relative overrides): `.resolve()`
        collapses `..` and symlinks into an absolute path *first*, then
        `is_relative_to()` verifies that absolute path is `_STATIC_ROOT`
        itself or strictly inside it before any filesystem read happens.
        `PurePath.__truediv__` resets to an absolute/drive-anchored operand
        when `rel` supplies one (e.g. `rel="C:/secret"` discards
        `_STATIC_ROOT` entirely) — harmless here because that only changes
        what `candidate` resolves to, and the containment check below still
        catches it precisely like any other escape.
        """
        rel = rest.lstrip("/") or "index.html"
        # codeql[py/path-injection]: sink is unavoidable — resolving the
        # candidate path (incl. following symlinks) is the first step of
        # the containment check itself; there is no way to validate
        # containment without first computing the canonical path.
        candidate = (_STATIC_ROOT / rel).resolve()
        if not candidate.is_relative_to(_STATIC_ROOT):
            self._reject("static_escape")
            return
        # codeql[py/path-injection]: `candidate` is proven contained by the
        # `is_relative_to()` guard above — safe to stat/read.
        if not candidate.is_file():
            self._reject("static_not_found")
            return
        try:
            data = candidate.read_bytes()  # codeql[py/path-injection]: see guard above
        except OSError:
            self._reject("static_read_failed")
            return
        self.send_response(200)
        self.send_header("Content-Type", _content_type(candidate.suffix))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", _CSP_HEADER)
        # Cloudflare Web Analytics otherwise auto-injects beacon.min.js into
        # this private control surface, which our intentionally strict CSP
        # blocks and reports as a red console error. no-transform prevents the
        # edge rewrite while no-store avoids turning the secret-path shell into
        # shared proxy cache content.
        self.send_header("Cache-Control", "private, no-store, no-transform")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    # ── SSE (§6.2/6.3, B3) ──────────────────────────────────────────────
    def _handle_sse(self, query: dict) -> None:
        project_ns = self.server.auth.consume_ticket(query.get("ticket"))
        if project_ns is None:
            self._reject("sse_bad_ticket")
            return
        self.server.auth.touch()  # M-6: a valid ticket is a successful auth
        q = self.server.broadcaster.register(project_ns)
        if q is None:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.connection.settimeout(_SSE_WRITE_TIMEOUT_SEC)
            while not self.server.stop_event.is_set():
                try:
                    event, data = q.get(timeout=_SSE_KEEPALIVE_SEC)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    continue
                if event is None:
                    # M-4: `SSEBroadcaster.close_all()`'s wake-up sentinel —
                    # the server is stopping, don't write it as a real event.
                    break
                self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
        except OSError:
            pass  # client gone / stalled (write timeout) — B3: cut it, don't hang the thread
        finally:
            self.server.broadcaster.unregister(q)


class RemoteHttpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_class, config: RemoteConfig, orch):
        super().__init__(server_address, handler_class)
        self.port = self.server_address[1]
        self.config = config
        self.auth = AuthGate(config)
        self.bridge = _Bridge(orch)
        self.broadcaster = SSEBroadcaster()
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.serve_forever, name="takkub-remote-http", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        # M-4: without this, an open `/api/lead` SSE connection's handler
        # thread stays blocked in `q.get(timeout=_SSE_KEEPALIVE_SEC)` for up
        # to 15s after stop() returns — `shutdown()`/`server_close()` only
        # ever touch the listening socket, never an already-accepted one.
        self.stop_event.set()
        self.broadcaster.close_all()
        try:
            self.shutdown()
        except Exception:
            pass
        try:
            self.server_close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


def start_server(config: RemoteConfig, orch) -> RemoteHttpServer:
    """Bind `config.bind_port`, scanning forward on conflict (§8b) — except
    `bind_port == 0`, which means "let the OS choose" (tests) and is never
    worth retrying on failure."""
    last_exc: OSError | None = None
    for offset in range(_MAX_PORT_SCAN):
        port = 0 if config.bind_port == 0 else config.bind_port + offset
        try:
            server = RemoteHttpServer(("127.0.0.1", port), _RemoteHandler, config, orch)
        except OSError as exc:
            last_exc = exc
            if port == 0:
                break
            continue
        server.start()
        return server
    raise RuntimeError(f"no free loopback port near {config.bind_port}: {last_exc}")


__all__ = ["RemoteHttpServer", "SSEBroadcaster", "start_server"]
