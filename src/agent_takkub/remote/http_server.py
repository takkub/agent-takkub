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

from . import api
from .auth import AuthGate
from .config import RemoteConfig

_log = logging.getLogger(__name__)

_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_MAX_BODY_BYTES = 64 * 1024
_BRIDGE_TIMEOUT_SEC = 8.0
_MAX_PORT_SCAN = 50
_MAX_SSE_CLIENTS = 4
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


@dataclass
class _PendingRequest:
    action: str
    params: dict
    reply: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))


class _Bridge(QObject):
    """The only door a handler thread has into Orchestrator. Constructed on
    the Qt main thread; `request` is emitted from arbitrary handler
    threads and Qt auto-queues delivery of `_handle` onto the main thread.
    """

    request = pyqtSignal(object)

    def __init__(self, orch) -> None:
        super().__init__()
        self._orch = orch
        self.request.connect(self._handle)

    def _handle(self, pending: _PendingRequest) -> None:
        try:
            if pending.action == "pulse":
                pending.reply.put((200, api.pulse(self._orch, None)))
            elif pending.action == "projects":
                pending.reply.put((200, api.projects(None)))
            elif pending.action == "lead_say":
                api.lead_say(self._orch, pending.params.get("text", ""), None)
                pending.reply.put((200, {"ok": True}))
            else:
                pending.reply.put((404, {"ok": False, "msg": "unknown action"}))
        except api.RemoteApiError as exc:
            pending.reply.put((exc.status, {"ok": False, "msg": exc.msg}))
        except Exception:
            # A handler-thread request must never be able to take down the
            # Qt main loop — log and answer with a generic 500 instead.
            _log.exception("remote api dispatch failed: %s", pending.action)
            pending.reply.put((500, {"ok": False, "msg": "internal error"}))


class SSEBroadcaster:
    """Fan-out for `/api/lead`. One bounded queue per connected client
    (finding B3): a full queue drops its oldest event instead of blocking
    the Qt-thread caller of `push`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []

    def register(self) -> queue.Queue | None:
        with self._lock:
            if len(self._clients) >= _MAX_SSE_CLIENTS:
                return None
            q: queue.Queue = queue.Queue(maxsize=_SSE_QUEUE_MAXSIZE)
            self._clients.append(q)
            return q

    def unregister(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def push(self, event: str, data: str) -> None:
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            while True:
                try:
                    q.put_nowait((event, data))
                    break
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break


class _RemoteHandler(http.server.BaseHTTPRequestHandler):
    server_version = "takkub-remote/1"
    # No keep-alive: bounds each API request to a single thread for its own
    # short lifetime (H4 — SSE is the only intentionally long-lived thread,
    # capped separately by _MAX_SSE_CLIENTS).
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args) -> None:
        # H3: BaseHTTPRequestHandler's default log echoes the full request
        # line — including a `?ticket=...` query string — to stderr. Never.
        pass

    # ── routing ──────────────────────────────────────────────────────────
    def _match_secret_path(self) -> tuple[str, dict] | None:
        parsed = urllib.parse.urlsplit(self.path)
        segments = parsed.path.split("/", 2)
        if len(segments) < 2 or not self.server.auth.check_secret_path(segments[1]):
            return None
        self.server.auth.touch()
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
            if self._check_bearer():
                self._respond_marshaled("pulse", {})
        elif rest == "/api/projects":
            if self._check_bearer():
                self._respond_marshaled("projects", {})
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
        if rest == "/api/sse-ticket":
            if self._check_bearer():
                self._send_json(200, {"ticket": self.server.auth.issue_ticket()})
        elif rest == "/api/lead/say":
            if not self._check_bearer():
                return
            if not self.server.auth.allows_control():
                self._send_json(403, {"ok": False, "msg": "view mode: control is disabled"})
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"ok": False, "msg": "bad json"})
                return
            self._respond_marshaled("lead_say", {"text": payload.get("text", "")})
        else:
            self._reject()

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
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    # ── SSE (§6.2/6.3, B3) ──────────────────────────────────────────────
    def _handle_sse(self, query: dict) -> None:
        if not self.server.auth.consume_ticket(query.get("ticket")):
            self._reject()
            return
        q = self.server.broadcaster.register()
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
            while True:
                try:
                    event, data = q.get(timeout=_SSE_KEEPALIVE_SEC)
                    chunk = f"event: {event}\ndata: {data}\n\n".encode()
                except queue.Empty:
                    chunk = b": keep-alive\n\n"
                self.wfile.write(chunk)
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
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.serve_forever, name="takkub-remote-http", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
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
