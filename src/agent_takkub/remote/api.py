"""api.py — route handlers, the only code that talks to `cli_server` (over
loopback TCP, exactly like `cli.py`) or reads `projects.json` (in-process,
via `config.py`). Runs on the Qt main thread only — `http_server.py`'s
signal bridge marshals every authenticated request here before any of it
executes (§6.4).

Data minimization (§7.3 / finding B2): `pulse` strips the `list` response
down to a bare `{working, total}` count. It never uses `cmd:"status"` and
never lets role/task/state/transcript text anywhere near the response.

Multi-project scoping (ponytail): v1 always targets the orchestrator's
currently-active project (`from_project=None` -> cli_server's documented
"falls back to the active project" default). The design doc's `from_project`
stamp is for a future per-connection project picker; upgrade path is to
thread an actual project name through here once the PWA offers that choice.
"""

from __future__ import annotations

import json
import socket

from .. import config as _config


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


def projects(from_project: str | None, mode: str = "view") -> dict:
    """View-mode. In-process, no loopback: reads the same `projects.json`
    the desktop UI reads. Safe from the H2 cross-thread race because this
    only ever runs on the Qt main thread (the same thread that writes the
    file on tab switch/import).

    M-3/M-1 contract: `mode` rides along in the same response (the PWA has
    no dedicated mode endpoint), and each project is `{name, active}` —
    not a bare string — so the frontend can render the active-project tag
    without a second lookup."""
    active, _ = _config.active_project()
    return {
        "projects": [{"name": n, "active": n == active} for n in _config.list_project_names()],
        "mode": mode,
        "open_tabs": _config.get_open_tabs(),
    }
