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
import logging
import socket
import time

from .. import config as _config
from ..roles import LEAD
from . import config as _remote_config
from . import notify

_HISTORY_DEFAULT_LIMIT = 200
_HISTORY_MAX_LIMIT = 200

_log = logging.getLogger(__name__)


def _lead_provider_note(project_ns: str) -> str | None:
    """Human-readable note when `project_ns`'s Lead is degraded off claude
    (issue #101) — surfaced in mobile-facing responses so a blank
    history/session list reads as "Lead isn't claude right now", not as a
    silent bug. `None` when Lead is claude (the common case; no per-request
    cost beyond one dict lookup)."""
    from ..provider_config import lead_capability_gap

    gap = lead_capability_gap(project_ns)
    if gap is None:
        return None
    provider, missing = gap
    return f"Lead provider = {provider} (ไม่ใช่ claude) — ไม่มี: {', '.join(missing)}"


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
    except OSError:
        _log.exception("remote api could not connect to cli_server")
        raise RemoteApiError(503, "cockpit unreachable") from None
    try:
        sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        sock.settimeout(timeout)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    except OSError:
        _log.exception("remote api cli_server request failed")
        raise RemoteApiError(503, "cockpit unreachable") from None
    finally:
        sock.close()
    if not buf:
        raise RemoteApiError(502, "no response from cli_server")
    try:
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        _log.exception("remote api received an invalid cli_server response")
        raise RemoteApiError(502, "invalid cockpit response") from None


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


def activity(orch) -> dict:
    """Pulse page (project-grouped active panes). DATA-MIN (§7.3, same bar as
    `pulse`): role + project + runtime only — never task text, cwd, command,
    or status detail. Runs inline on the Qt main thread (like `projects`/
    `lead_history`, not `pulse`'s off-thread loopback call) since it reads
    `orch._panes_by_project` directly rather than going through cli_server.

    `pane._working_start` (the same wall-clock the pane header's own
    elapsed-time spinner reads, see `agent_pane.py:set_state`) is the
    "started" timestamp — a pane can be spawned long before it's actually
    given work, so runtime is measured from the current task starting, not
    from spawn.

    Lead is surfaced separately from `roles` (W4): every project with an open
    Lead pane gets a `lead` entry with `state: "working"|"idle"` regardless of
    whether it's currently working, so the phone always shows whether Lead is
    home. Idle Lead's `_working_start` is `None` (cleared by `set_state` —
    see `agent_pane.py`), so idle never reuses a stale/previous runtime; it's
    reported as 0.

    When `config.LEAD_ONLY_STREAM` is on (the default since 2026-07-23),
    `roles` is always `[]`: the phone mirrors Lead and nothing else. The key
    is still emitted — dropping it would break every PWA build that reads
    `p.roles.length` — it is simply always empty."""
    now = time.time()
    lead_only = _remote_config.LEAD_ONLY_STREAM
    projects_out: list[dict] = []
    for project_ns, panes in (getattr(orch, "_panes_by_project", None) or {}).items():
        roles: list[dict] = []
        lead_out: dict | None = None
        for role, pane in panes.items():
            state = getattr(pane, "state", None)
            if role == LEAD.name:
                working = state == "working"
                started = getattr(pane, "_working_start", None) if working else None
                runtime_sec = max(0, int(now - started)) if started is not None else 0
                lead_out = {"state": "working" if working else "idle", "runtime_sec": runtime_sec}
                continue
            if lead_only:
                continue
            if state != "working":
                continue
            started = getattr(pane, "_working_start", None)
            if started is None:
                continue
            roles.append({"role": role, "runtime_sec": max(0, int(now - started))})
        if roles or lead_out is not None:
            entry: dict = {"project": project_ns, "roles": roles}
            if lead_out is not None:
                entry["lead"] = lead_out
            projects_out.append(entry)
    return {"projects": projects_out}


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


def close_project(orch, project: object) -> dict:
    """control-mode only (enforced by the HTTP handler's mode gate before
    this runs, same as `open_project`). Validates `project` against
    `projects.json` before touching main_window. Already-closed is a
    no-op (idempotent — the phone may retry). Delegates to
    `MainWindow._close_project_tab(project, confirm=False)` — the same
    teardown the desktop close-tab path uses, minus the Qt confirm dialog
    (the phone confirms on its own side, e.g. a tap-to-confirm sheet,
    before ever calling in)."""
    if not isinstance(project, str) or project not in _config.list_project_names():
        raise RemoteApiError(400, "unknown project")
    if project not in _config.get_open_tabs():
        return {"ok": True, "project": project}
    close_tab = getattr(orch.parent(), "_close_project_tab", None)
    if close_tab is None:
        raise RemoteApiError(500, "cannot reach main window")
    ok, msg = close_tab(project, confirm=False)
    if not ok:
        raise RemoteApiError(409, msg or "project could not be closed")
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
    return {
        "project": project_ns,
        "messages": messages,
        "lead_provider_note": _lead_provider_note(project_ns),
    }


_SESSIONS_MAX_LIMIT = 20


def lead_sessions(orch, project_ns: str, limit: object = None) -> dict:
    """View-mode safe (read-only) — W3 resume/session picker. Lists recent
    Lead sessions for `project_ns`'s cwd so the phone can resume a closed or
    crashed Lead without the desktop cockpit's help. Data-min per
    `notify.list_recent_lead_sessions`: `{uuid, mtime, preview}` only."""
    try:
        limit = int(limit) if limit is not None else notify._SESSION_LIST_DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = notify._SESSION_LIST_DEFAULT_LIMIT
    limit = max(1, min(limit, _SESSIONS_MAX_LIMIT))
    return {
        "project": project_ns,
        "sessions": notify.list_recent_lead_sessions(project_ns, limit),
        "lead_provider_note": _lead_provider_note(project_ns),
    }


def resume_lead(orch, project: object, session_uuid: object) -> dict:
    """control-mode only (enforced by the HTTP handler's mode gate before this
    runs, same as `open_project`/`close_project`). Terminates the project's
    current Lead pane (same lifecycle `close_project` uses — protected-pane
    force-close) then respawns Lead with `--resume <session_uuid>`.

    `session_uuid`/cwd match is prevalidated here (`_resume_uuid_matches_cwd`)
    BEFORE `orch.close()` runs, and re-checked again inside `spawn()` itself
    as defense in depth — prevalidating means a forged/mismatched uuid never
    tears down the live Lead pane in the first place (previously the mismatch
    was only caught inside `spawn()`, by which point `close()` had already
    run — leaving the project with no Lead pane at all and a rejected
    resume).

    Multi-provider note (#101): `--resume` is a claude-CLI-specific
    capability (`ProviderSpec.supports_resume`). Gated explicitly below via
    `provider_config.lead_capability_gap` — a codex/agy-backed Lead gets a
    clear 409 here instead of reaching `spawn()` and failing opaquely on an
    unknown flag / provider mismatch."""
    if not isinstance(project, str) or project not in _config.list_project_names():
        raise RemoteApiError(400, "unknown project")
    if project not in _config.get_open_tabs():
        raise RemoteApiError(409, "project not open")
    from ..provider_config import lead_missing_capability

    missing_provider = lead_missing_capability("supports_resume", project)
    if missing_provider is not None:
        raise RemoteApiError(
            409, f"resume unavailable — Lead provider = {missing_provider} (ไม่รองรับ --resume)"
        )
    if not isinstance(session_uuid, str) or not session_uuid.strip():
        raise RemoteApiError(400, "missing session_uuid")
    session_uuid = session_uuid.strip()
    cwd = _config.lead_cwd(project)
    if not cwd:
        raise RemoteApiError(409, "project has no lead cwd")
    from ..spawn_engine import _resume_uuid_matches_cwd

    if not _resume_uuid_matches_cwd(project, session_uuid, cwd):
        raise RemoteApiError(409, "resume_uuid does not match cwd")
    orch.close(LEAD.name, project=project, force=True, reason="remote resume")
    ok, msg = orch.spawn(LEAD.name, cwd=cwd, project=project, resume_uuid=session_uuid)
    if not ok:
        raise RemoteApiError(409, msg or "resume failed")
    return {"ok": True, "project": project}


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
