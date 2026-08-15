"""api.py — route handlers, the only code that talks to `cli_server` (over
loopback TCP, exactly like `cli.py`) or reads `projects.json` (in-process,
via `config.py`). Runs on the Qt main thread only — `http_server.py`'s
signal bridge marshals every authenticated request here before any of it
executes (§6.4).

Data minimization (§7.3 / finding B2): `pulse` strips the `list` response
down to a bare `{working, total}` count. It never uses `cmd:"status"` and
never lets role/task/transcript text anywhere near the response; `activity`
holds to the same bar, adding only a coarse `state: "working"|"idle"` per
pane. When `config.PULSE_SHOW_TEAM` is off, `pulse` scopes its count down to
the `lead` entry only and `activity` reports an empty `roles` list — the
pre-#200 default (see `remote/config.py`'s `LEAD_ONLY_STREAM`/
`PULSE_SHOW_TEAM` split for why these are two separate flags now).

Multi-project scoping (project picker): `from_project` is threaded through
from the HTTP layer (`http_server.py`'s `_Bridge._resolve_scoped_project`
validates the client-supplied project name against the open tabs before it
ever reaches here). `from_project=None` falls back to cli_server's
documented "falls back to the active project" default — unchanged for
callers that don't pass a project (e.g. tests, or a client that hasn't
picked one yet).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import socket
import time
import uuid
from datetime import datetime
from pathlib import Path

from .. import config as _config
from ..roles import LEAD
from . import config as _remote_config
from . import notify

_HISTORY_DEFAULT_LIMIT = 200
_HISTORY_MAX_LIMIT = 200
_MAX_REMOTE_IMAGE_BYTES = 8 * 1024 * 1024

_IMAGE_FORMATS = {
    "png": ("png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
    "jpeg": ("jpg", lambda data: data.startswith(b"\xff\xd8\xff")),
    "webp": (
        "webp",
        lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
    ),
    "gif": ("gif", lambda data: data.startswith((b"GIF87a", b"GIF89a"))),
}

_log = logging.getLogger(__name__)

# Strips C0 control chars (keeps \n) so a caption can never inject a PTY
# control sequence (e.g. \x03/Ctrl+C, ANSI escapes) into lead_say's terminal.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x09\x0b-\x1f]")


def _lead_provider_note(provider: str) -> str | None:
    """Explain a clean empty state when no history scanner is available."""
    if notify.supports_remote_history(provider):
        return None
    return f"Lead provider = {provider} — remote history/session unavailable"


# #192 (remote-blank-output): Thai, user-facing explanation for each
# `notify.lead_mirror_diagnosis` code — the phone must say *why* a chat is
# blank instead of staying silently empty. Data-min: only the provider name
# and an 8-char session id prefix ever reach this text, never a full uuid or
# filesystem path (§7.3 — same bar `notify.py`'s own docstrings hold this
# module to).
def _empty_reason_payload(diagnosis: dict) -> dict | None:
    code = diagnosis.get("code")
    if code is None:
        return None
    if code == "provider_unsupported":
        provider = diagnosis.get("provider") or "?"
        text = (
            f"Lead pane นี้ใช้ provider {provider} ที่ยังไม่รองรับการมิเรอร์บทสนทนามาที่มือถือ "
            "(#103) — อ่านคำตอบได้ที่เดสก์ท็อปเท่านั้น"
        )
    elif code == "no_session_uuid":
        text = (
            "ยังไม่มี session ที่บันทึกไว้สำหรับ Lead pane นี้ — พิมพ์ข้อความแรกจากเดสก์ท็อปก่อน หรือรอสักครู่แล้วรีเฟรช"
        )
    elif code == "transcript_missing":
        short = diagnosis.get("session_uuid_short")
        suffix = f" (session {short}…)" if short else ""
        text = (
            f"ไม่พบไฟล์บทสนทนาของเซสชันนี้{suffix} — อาจเพิ่งเปิด Lead pane ใหม่ (ไฟล์ยังไม่ถูกสร้าง) "
            "หรือ session ไม่ตรงกับที่ pane จำไว้ (เช่นสั่ง /resume จากเดสก์ท็อป) ลองพิมพ์ข้อความจาก"
            "เดสก์ท็อปอีกครั้ง หรือรีสตาร์ท Lead pane"
        )
    else:
        return None
    return {"code": code, "text": text}


def _pulse_project(from_project: str | None) -> str | None:
    """Resolve pulse's implicit active-project scope for provider labeling."""
    if from_project:
        return from_project
    try:
        active, _ = _config.active_project()
    except (OSError, TypeError, ValueError):
        return None
    return active if isinstance(active, str) and active else None


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
    """§7.3 / B2: count only — never `cmd:"status"`, never role/task text.

    PULSE_SHOW_TEAM (#200, 2026-08-14) defaults True: `total`/`working` count
    every open pane, not just Lead — the legacy `/api/pulse` route the
    shipped PWA no longer calls (see `app.js`'s `fetchPulse`, which reads
    `/api/activity`) but that stays live and authenticated for any other
    client. When the flag is off, scope the count down to the `lead` entry
    before counting so `total` never reveals team size and `working` never
    reveals teammate activity; `total` is then 1 (Lead open) or 0 (no Lead
    pane for this project), never > 1."""
    provider = notify.lead_provider_name(orch, _pulse_project(from_project))
    resp = _lead_frame(orch, {"cmd": "list", "from": "remote", "from_project": from_project})
    status = resp.get("status") if isinstance(resp, dict) else None
    if not isinstance(status, dict):
        return {"working": 0, "total": 0, "provider": provider}
    if not _remote_config.PULSE_SHOW_TEAM:
        lead_state = status.get(LEAD.name)
        if not isinstance(lead_state, str):
            return {"working": 0, "total": 0, "provider": provider}
        working = 1 if lead_state.startswith("working") else 0
        return {"working": working, "total": 1, "provider": provider}
    working = sum(
        1 for state in status.values() if isinstance(state, str) and state.startswith("working")
    )
    return {"working": working, "total": len(status), "provider": provider}


def activity(orch) -> dict:
    """Pulse page (project-grouped open panes). DATA-MIN (§7.3, same bar as
    `pulse`): role + project + state + runtime only — never task text, cwd,
    command, or fine-grained status detail (e.g. "working (stalled 12m)"
    collapses to the same "working"/"idle" the phone gets for everything
    else). Runs inline on the Qt main thread (like `projects`/`lead_history`,
    not `pulse`'s off-thread loopback call) since it reads
    `orch._panes_by_project` directly rather than going through cli_server.

    `pane._working_start` (the same wall-clock the pane header's own
    elapsed-time spinner reads, see `agent_pane.py:set_state`) is the
    "started" timestamp — a pane can be spawned long before it's actually
    given work, so runtime is measured from the current task starting, not
    from spawn. A pane whose state is "working" but has no `_working_start`
    yet (defensive — `set_state` always stamps one, but never fabricate a
    runtime) reports `runtime_sec: 0` rather than being dropped from the
    list — the whole point of #200 is that every open pane shows up.

    Lead is surfaced separately from `roles` (W4): every project with an open
    Lead pane gets a `lead` entry with `state: "working"|"idle"` regardless of
    whether it's currently working, so the phone always shows whether Lead is
    home.

    `roles` covers every other open pane (#200, 2026-08-14) — not just ones
    currently working, so a project with three idle teammates still shows
    three chips, not zero. When `config.PULSE_SHOW_TEAM` is off, `roles` is
    always `[]`: the phone mirrors Lead and nothing else (pre-#200 default).
    The key is still emitted either way — dropping it would break every PWA
    build that reads `p.roles.length`."""
    now = time.time()
    show_team = _remote_config.PULSE_SHOW_TEAM
    projects_out: list[dict] = []
    for project_ns, panes in (getattr(orch, "_panes_by_project", None) or {}).items():
        roles: list[dict] = []
        lead_out: dict | None = None
        known_states: dict[str, str] = {}
        for role, pane in panes.items():
            state = getattr(pane, "state", None)
            known_states[role] = state or ""
            working = state == "working"
            started = getattr(pane, "_working_start", None) if working else None
            runtime_sec = max(0, int(now - started)) if started is not None else 0
            state_out = "working" if working else "idle"
            if role == LEAD.name:
                lead_out = {
                    "state": state_out,
                    "runtime_sec": runtime_sec,
                    "provider": notify.pane_provider_name(orch, project_ns, role, pane),
                }
                continue
            if not show_team:
                continue
            roles.append(
                {
                    "role": role,
                    "state": state_out,
                    "runtime_sec": runtime_sec,
                    "provider": notify.pane_provider_name(orch, project_ns, role, pane),
                }
            )
        # #225: `done()` auto-closes its own pane ~2.5s after reporting, well
        # before the done-notice necessarily finishes its trip through the
        # digest/live/durable queues to Lead — a role can vanish from this
        # feed within seconds of finishing while its report is still in
        # flight (minutes, in a draft-hold worst case). `list_status`/
        # `list_status_detailed` already cover this for `takkub list`/
        # `status` (#163's `_pending_notice_roles`), but this endpoint reads
        # `_panes_by_project` directly (see docstring) so it never picked up
        # that fix. Reuse the same helper and fold the pending role in as a
        # coarse "idle" entry — no notice body, no fine-grained detail,
        # matching this endpoint's DATA-MIN contract — so a phone polling
        # this feed doesn't lose track of a role that just finished.
        if show_team:
            pending_fn = getattr(orch, "_pending_notice_roles", None)
            if callable(pending_fn):
                try:
                    pending_roles = pending_fn(project_ns, known_states)
                except Exception:
                    pending_roles = {}
                for role in pending_roles:
                    if role == LEAD.name or role in known_states:
                        continue
                    roles.append(
                        {
                            "role": role,
                            "state": "idle",
                            "runtime_sec": 0,
                            "provider": notify.pane_provider_name(orch, project_ns, role, None),
                        }
                    )
        if roles or lead_out is not None:
            entry: dict = {"project": project_ns, "roles": roles}
            if lead_out is not None:
                entry["lead"] = lead_out
            projects_out.append(entry)
    return {"projects": projects_out}


def lead_say(orch, text: str, from_project: str | None) -> dict:
    """control-mode only (enforced by the HTTP handler's mode gate before
    this runs). Delivers `text` into the Lead pane the same way a peer
    pane's `takkub send --to lead` would — Lead decides what to do with it.

    Delivery and mirroring are separate concerns (2026-08-13 remote-mirror
    fix): the message above always reaches Lead once `_lead_frame` returns
    ok — cli_server's `send` writes straight into the pane's pty, unrelated
    to whether this provider's replies can be read back. `mirror_supported`
    tells the PWA up front whether it should expect a live reply to ever
    arrive (`notify.supports_remote_history`, same gate `_lead_history`
    already exposes for the one-shot history/session reads) so it can stop
    showing an indefinite "…กำลังทำงาน" spinner for a provider that will
    never produce one, instead of the client discovering this only after a
    blind timeout with no explanation."""
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
    provider = notify.lead_provider_name(orch, _pulse_project(from_project))
    return {
        "ok": True,
        "provider": provider,
        "mirror_supported": notify.supports_remote_history(provider),
        "lead_provider_note": _lead_provider_note(provider),
    }


def lead_upload_image(
    orch,
    data_url: object,
    filename: object,
    caption: object,
    from_project: str | None,
) -> dict:
    """Persist one mobile image in the project's central artifacts directory
    and tell Lead its exact local path. All providers receive the same plain
    text path, so their native image-reading tools can inspect it without a
    provider-specific upload protocol.

    The client-supplied filename is display-only. Storage uses a random name,
    and MIME is verified against magic bytes before anything is written.
    """
    if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
        raise RemoteApiError(400, "invalid image")
    header, separator, encoded = data_url.partition(",")
    if not separator or not header.endswith(";base64"):
        raise RemoteApiError(400, "invalid image")
    mime_subtype = header.removeprefix("data:image/").removesuffix(";base64").lower()
    image_format = _IMAGE_FORMATS.get(mime_subtype)
    if image_format is None:
        raise RemoteApiError(400, "unsupported image type")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise RemoteApiError(400, "invalid image") from None
    if not image_bytes:
        raise RemoteApiError(400, "empty image")
    if len(image_bytes) > _MAX_REMOTE_IMAGE_BYTES:
        raise RemoteApiError(413, "image too large")
    extension, matches_magic = image_format
    if not matches_magic(image_bytes):
        raise RemoteApiError(400, "image content does not match type")

    try:
        project_ns = _config.validate_name(from_project or "default", "project")
    except ValueError:
        raise RemoteApiError(400, "invalid project") from None
    today = datetime.now().strftime("%Y-%m-%d")
    runtime_root = _config.RUNTIME_DIR.resolve()
    screenshots = (_config.RUNTIME_DIR / "exports" / today / project_ns / "screenshots").resolve()
    if runtime_root not in screenshots.parents:
        raise RemoteApiError(400, "invalid project")
    try:
        screenshots.mkdir(parents=True, exist_ok=True)
        image_path = screenshots / f"remote-{uuid.uuid4().hex}.{extension}"
        image_path.write_bytes(image_bytes)
    except OSError as exc:
        _log.warning("could not save remote image: %s", exc)
        raise RemoteApiError(500, "could not save image") from None

    display_name = Path(filename).name[:120] if isinstance(filename, str) else "image"
    clean_caption = (
        _CONTROL_CHARS_RE.sub("", caption).strip()[:2000] if isinstance(caption, str) else ""
    )
    message = f'[remote → lead] แนบรูปจาก mobile ให้เปิดดูจากไฟล์นี้: "{image_path}"'
    if clean_caption:
        message += f"\nข้อความประกอบ: {clean_caption}"
    try:
        say_result = lead_say(orch, message, project_ns)
    except Exception:
        try:
            image_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return {
        "ok": True,
        "name": display_name or "image",
        "mirror_supported": say_result.get("mirror_supported"),
        "lead_provider_note": say_result.get("lead_provider_note"),
    }


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
    provider, messages = notify.lead_history_snapshot(orch, project_ns, limit)
    panes = getattr(orch, "_panes_by_project", None)
    project_panes = panes.get(project_ns) if isinstance(panes, dict) else None
    lead_pane = project_panes.get("lead") if isinstance(project_panes, dict) else None
    working = getattr(lead_pane, "state", None) == "working"
    # #192: an empty history is ambiguous on its own (genuinely no messages
    # yet vs. provider gap vs. session drift) — classify it only when it's
    # actually empty, so a normal populated chat never pays this extra cost.
    empty_reason = (
        _empty_reason_payload(notify.lead_mirror_diagnosis(orch, project_ns))
        if not messages
        else None
    )
    return {
        "project": project_ns,
        "provider": provider,
        "messages": messages,
        "working": working,
        "lead_provider_note": _lead_provider_note(provider),
        "empty_reason": empty_reason,
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
    provider, sessions = notify.lead_sessions_snapshot(orch, project_ns, limit)
    return {
        "project": project_ns,
        "provider": provider,
        "sessions": sessions,
        "lead_provider_note": _lead_provider_note(provider),
    }


def resume_lead(orch, project: object, session_uuid: object) -> dict:
    """control-mode only (enforced by the HTTP handler's mode gate before this
    runs, same as `open_project`/`close_project`). Terminates the project's
    current Lead pane (same lifecycle `close_project` uses — protected-pane
    force-close) then respawns Lead with the active provider's resume flag.

    `session_uuid`/cwd match is prevalidated here (provider-specific local
    transcript lookup)
    BEFORE `orch.close()` runs, and re-checked again inside `spawn()` itself
    as defense in depth — prevalidating means a forged/mismatched uuid never
    tears down the live Lead pane in the first place (previously the mismatch
    was only caught inside `spawn()`, by which point `close()` had already
    run — leaving the project with no Lead pane at all and a rejected
    resume).

    Multi-provider note (#101): resume is capability-gated through
    ``ProviderSpec.supports_resume``. Claude uses ``--resume`` and Gemini/agy
    uses ``--conversation``; providers without a verified store+flag get a
    clear 409 before the live pane is touched."""
    if not isinstance(project, str) or project not in _config.list_project_names():
        raise RemoteApiError(400, "unknown project")
    if project not in _config.get_open_tabs():
        raise RemoteApiError(409, "project not open")
    from ..provider_config import effective_provider_for, lead_missing_capability

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
    provider = effective_provider_for("lead", project)
    from ..spawn_engine import _resume_uuid_matches_provider_cwd

    if not _resume_uuid_matches_provider_cwd(project, provider, session_uuid, cwd):
        raise RemoteApiError(409, "resume_uuid does not match cwd")
    # Snapshot the exact selected transcript before pane replacement. The
    # response lets Mobile paint it immediately instead of racing notifier
    # resync + a separate history request after the desktop already resumed.
    resume_messages = notify.read_resume_session_messages(
        project, provider, session_uuid, _HISTORY_MAX_LIMIT
    )
    orch.close(LEAD.name, project=project, force=True, reason="remote resume")
    ok, msg = orch.spawn(LEAD.name, cwd=cwd, project=project, resume_uuid=session_uuid)
    if not ok:
        raise RemoteApiError(409, msg or "resume failed")
    return {
        "ok": True,
        "project": project,
        "provider": provider,
        "messages": resume_messages,
    }


def usage() -> dict:
    """View-mode safe (read-only). Per the provider-usage design doc §4:
    remote must NEVER trigger a live provider fetch — a phone poll would
    otherwise become an extra rate-limit hit against a provider's endpoint.
    Reads `provider_usage.get_store()`'s cache only; the store's own
    background thread does the actual fetching. A provider with no cache
    entry yet (first request since cockpit start, before its first
    background fetch completes) reports `status: "loading"` here instead of
    this handler fetching it inline."""
    from .. import __version__, provider_usage

    store = provider_usage.get_store()
    cache = store.get_all()
    providers = [
        provider_usage.usage_to_dict(
            cache.get(name) or provider_usage.ProviderUsage(provider=name, status="loading")
        )
        for name in provider_usage.PROVIDER_NAMES
    ]
    # #192: the phone had no way to tell whether it was talking to an old
    # cockpit build that predates a mirror-diagnostics fix — this rides
    # along on the endpoint the PWA already polls every USAGE_POLL_MS tick,
    # no new round trip.
    return {"providers": providers, "cockpit_version": __version__}


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
