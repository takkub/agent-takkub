"""api.py — route handlers, the only code that talks to `cli_server` (over
loopback TCP, exactly like `cli.py`) or reads `projects.json` (in-process,
via `config.py`). Runs on the Qt main thread only — `http_server.py`'s
signal bridge marshals every authenticated request here before any of it
executes (§6.4).

Data minimization (§7.3 / finding B2): `pulse` strips the `list` response
down to a bare `{working, total}` count. It never uses `cmd:"status"` and
never lets role/task/state/transcript text anywhere near the response.

Multi-project scoping (project picker): `from_project` is threaded through
from the HTTP layer (`http_server.py`'s `_Bridge._resolve_scoped_project`
validates the client-supplied project name against the open tabs before it
ever reaches here). `from_project=None` falls back to cli_server's
documented "falls back to the active project" default — unchanged for
callers that don't pass a project (e.g. tests, or a client that hasn't
picked one yet).
"""

from __future__ import annotations

import json
import socket

from .. import config as _config
from . import notify

_HISTORY_DEFAULT_LIMIT = 200
_HISTORY_MAX_LIMIT = 200


class RemoteApiError(Exception):
    """Carries an HTTP status alongside the message the client should see."""

    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status = status
        self.msg = msg


def _lead_frame(orch, payload: dict, timeout: float = 5.0) -> dict:
    """Send `payload` to cli_server over loopback TCP — the same protocol
    `cli.py`'s `_request` uses. Stamps the Lead capability token so
    Lead-guarded cmds (namely `send`) are accepted."""
    token = getattr(orch, "_lead_token", None)
    if not token:
        raise RemoteApiError(500, "lead token unavailable")
    port = _config.read_port()
    if port is None:
        raise RemoteApiError(503, "cockpit not listening")
    payload = {**payload, "auth": token}
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError as exc:
        raise RemoteApiError(503, f"cli_server unreachable: {exc}") from None
    try:
        sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        sock.settimeout(timeout)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    except OSError as exc:
        raise RemoteApiError(503, f"cli_server request failed: {exc}") from None
    finally:
        sock.close()
    if not buf:
        raise RemoteApiError(502, "no response from cli_server")
    try:
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RemoteApiError(502, f"bad response from cli_server: {exc}") from None


def pulse(orch, from_project: str | None) -> dict:
    """§7.3 / B2: count only — never `cmd:"status"`, never role/task text."""
    resp = _lead_frame(orch, {"cmd": "list", "from": "remote", "from_project": from_project})
    status = resp.get("status") if isinstance(resp, dict) else None
    if not isinstance(status, dict):
        return {"working": 0, "total": 0}
    working = sum(
        1 for state in status.values() if isinstance(state, str) and state.startswith("working")
    )
    return {"working": working, "total": len(status)}


def lead_say(orch, text: str, from_project: str | None) -> dict:
    """control-mode only (enforced by the HTTP handler's mode gate before
    this runs). Delivers `text` into the Lead pane the same way a peer
    pane's `takkub send --to lead` would — Lead decides what to do with it."""
    text = (text or "").strip()
    if not text:
        raise RemoteApiError(400, "empty message")
    resp = _lead_frame(
        orch,
        {"cmd": "send", "to": "lead", "msg": text, "from": "remote", "from_project": from_project},
    )
    if not isinstance(resp, dict) or not resp.get("ok"):
        msg = resp.get("msg") if isinstance(resp, dict) else None
        raise RemoteApiError(502, msg or "send failed")
    return {"ok": True}


def open_project(orch, project: object) -> dict:
    """control-mode only (enforced by the HTTP handler's mode gate before
    this runs, same as `lead_say`). Validates `project` against
    `projects.json` before ever touching main_window — a non-string or
    unlisted name is rejected outright, never opened blind from client
    input. Already-open is a no-op (idempotent — the picker may retry).

    Reaches main_window dynamically via `orch.parent()` — the Qt parent
    `main_window.py` passes to `Orchestrator(self)` at construction — so
    this module never needs a static import of `main_window` (bolt-on
    isolation, X-check C2/U5)."""
    if not isinstance(project, str) or project not in _config.list_project_names():
        raise RemoteApiError(400, "unknown project")
    if project in _config.get_open_tabs():
        return {"ok": True, "project": project}
    open_tab = getattr(orch.parent(), "_open_project_tab", None)
    if open_tab is None:
        raise RemoteApiError(500, "cannot reach main window")
    open_tab(project)
    if project not in _config.get_open_tabs():
        # `_open_project_tab` skips silently if the project's folder is
        # missing on disk (status-bar message only) — surface that here
        # as a real error instead of a false "ok".
        raise RemoteApiError(409, "project could not be opened")
    return {"ok": True, "project": project}


def lead_history(orch, project_ns: str, limit: object = None) -> dict:
    """View-mode safe (read-only) — lets the PWA repopulate its chat log on
    connect/reconnect/project-switch instead of showing a blank screen for
    whatever happened while no SSE client was registered (`notify.py`'s live
    tail only ever reaches currently-connected clients). Reuses `notify.py`'s
    uuid→jsonl resolution and assistant-text extraction so this and the live
    tail can never disagree on what counts as a reply. Also surfaces
    user-typed turns (`kind: "me"`) interleaved in JSONL order, so a
    tab-switch/reconnect repopulates both sides of the conversation instead
    of only the Lead's."""
    try:
        limit = int(limit) if limit is not None else _HISTORY_DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = _HISTORY_DEFAULT_LIMIT
    limit = max(1, min(limit, _HISTORY_MAX_LIMIT))
    path = notify.resolve_lead_jsonl(orch, project_ns)
    messages = notify.read_recent_lead_messages(path, limit) if path is not None else []
    return {"project": project_ns, "messages": messages}


def projects(from_project: str | None, mode: str = "view") -> dict:
    """View-mode. In-process, no loopback: reads the same `projects.json`
    the desktop UI reads. Safe from the H2 cross-thread race because this
    only ever runs on the Qt main thread (the same thread that writes the
    file on tab switch/import).

    M-3/M-1 contract: `mode` rides along in the same response (the PWA has
    no dedicated mode endpoint), and each project is `{name, active, path}`
    — not a bare string — so the frontend can render the active-project tag
    and the real working directory without a second lookup. `path` is the
    project's Lead cwd (`config.lead_cwd`, same resolution main_window uses
    to spawn Lead) — empty string if the project has no configured paths."""
    active, _ = _config.active_project()
    return {
        "projects": [
            {"name": n, "active": n == active, "path": _config.lead_cwd(n) or ""}
            for n in _config.list_project_names()
        ],
        "mode": mode,
        "open_tabs": _config.get_open_tabs(),
    }
