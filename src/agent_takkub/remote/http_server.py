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
from . import api
from .auth import AuthGate
from .config import RemoteConfig

_log = logging.getLogger(__name__)

_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_MAX_BODY_BYTES = 64 * 1024
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
    "img-src 'self' data:; style-src 'self' 'unsafe-inline'"
)


@dataclass
class _PendingRequest:
    action: str
    params: dict
    reply: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))


class _Bridge(QObject):
    """The only door a handler thread has into Orchestrator. Constructed on
    the Qt main thread; `request` is emitted from arbitrary handler
    threads and Qt auto-queues delivery of `_handle` onto the main thread.

    M-5: `pulse`/`lead_say` do real loopback-socket I/O to `cli_server`
    (up to several seconds under load) — that must never block the Qt
    event loop. They never touch Orchestrator/pane state directly (only
    the already-thread-safe `orch._lead_token` attribute), so the actual
    socket call is kicked off on a throwaway worker thread instead of
    running inline here. Their client-supplied `project` param is still
    resolved inline first (`_resolve_scoped_project`, a cheap in-process
    read) so the open-tabs check happens on the Qt thread before the
    worker thread ever starts. `projects`/`sse_ticket`/`open`/`lead_history`
    stay fully inline: cheap, in-process work (a single project-scoped
    JSONL read for `lead_history`) that DOES need the Qt-thread ownership
    guarantee (the same thread that writes `projects.json` on tab
    switch/import, and the only thread allowed to touch `main_window`).
    """

    request = pyqtSignal(object)

    _OFF_THREAD_ACTIONS = frozenset({"pulse", "lead_say"})

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
        if pending.action in self._OFF_THREAD_ACTIONS:
            pending.params["project"] = self._resolve_scoped_project(pending.params.get("project"))
            threading.Thread(target=self._run_off_thread, args=(pending,), daemon=True).start()
            return
        try:
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
            elif pending.action == "lead_history":
                project_ns = self._resolve_scoped_project(pending.params.get("project"))
                pending.reply.put(
                    (200, api.lead_history(self._orch, project_ns, pending.params.get("limit")))
                )
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
        except api.RemoteApiError as exc:
            pending.reply.put((exc.status, {"ok": False, "msg": exc.msg}))
        except Exception:
            _log.exception("remote api dispatch failed: %s", pending.action)
            pending.reply.put((500, {"ok": False, "msg": "internal error"}))


_ALLOWED_SSE_EVENTS = frozenset({"done", "lead", "working"})


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

    def push(self, event: str, data: str, project_ns: str | None = None) -> None:
        """H-C: `data` is JSON-encoded before it ever reaches the wire, so a
        payload containing raw newlines can neither break SSE line framing
        nor inject a fake `event:`/`data:` line into the stream. `event` is
        checked against a fixed allowlist for the same reason."""
        if event not in _ALLOWED_SSE_EVENTS:
            return
        payload = json.dumps({"text": data}, ensure_ascii=False)
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

    def _reject(self) -> None:
        """§7.5: unauthenticated (wrong secret-path OR wrong token) always
        gets a bare 404 — never a 401, never a hint that anything exists."""
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
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
            self._reject()
            return False
        self.server.auth.touch()  # M-6: only a *successful* auth counts as activity
        return True

    def do_GET(self) -> None:
        matched = self._match_secret_path()
        if matched is None:
            self._reject()
            return
        rest, query = matched
        if rest == "/api/lead":
            self._handle_sse(query)
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
        elif rest.startswith("/api/"):
            self._reject()
        else:
            self._serve_static(rest)

    def do_POST(self) -> None:
        matched = self._match_secret_path()
        if matched is None:
            self._reject()
            return
        rest, _query = matched
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._reject()
            return
        if not (0 <= length <= _MAX_BODY_BYTES):
            self._reject()
            return
        body = self.rfile.read(length) if length else b""
        if rest == "/api/verify-password":
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
        else:
            self._reject()

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

    def _respond_marshaled(self, action: str, params: dict) -> None:
        pending = _PendingRequest(action=action, params=params)
        self.server.bridge.request.emit(pending)
        try:
            status, payload = pending.reply.get(timeout=_BRIDGE_TIMEOUT_SEC)
        except queue.Empty:
            # H1: the Qt main thread never answered (e.g. mid-shutdown) —
            # give up instead of blocking this worker thread forever.
            status, payload = 504, {"ok": False, "msg": "orchestrator did not respond"}
        self._send_json(status, payload)

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

    # ── static PWA shell ─────────────────────────────────────────────────
    def _serve_static(self, rest: str) -> None:
        rel = rest.lstrip("/") or "index.html"
        candidate = (_STATIC_ROOT / rel).resolve()
        if candidate != _STATIC_ROOT and _STATIC_ROOT not in candidate.parents:
            self._reject()
            return
        if not candidate.is_file():
            self._reject()
            return
        try:
            data = candidate.read_bytes()
        except OSError:
            self._reject()
            return
        self.send_response(200)
        self.send_header("Content-Type", _content_type(candidate.suffix))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", _CSP_HEADER)
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    # ── SSE (§6.2/6.3, B3) ──────────────────────────────────────────────
    def _handle_sse(self, query: dict) -> None:
        project_ns = self.server.auth.consume_ticket(query.get("ticket"))
        if project_ns is None:
            self._reject()
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
