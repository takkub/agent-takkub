"""OpenCode CLI (`opencode`) helper — transcript resolution, history, and live tailing.

Backs the cockpit's `opencode` provider role and Lead remote mirror. OpenCode
stores its conversation history in a local SQLite database (`opencode.db`)
located under `~/.local/share/opencode/opencode.db` (Linux/macOS) or
`%LOCALAPPDATA%\\opencode\\opencode.db` (Windows).

Design rules (mirror codex_helper.py / gemini_helper.py):
- Best-effort. Any DB failure returns clean fallback, never raises.
- SQLite access uses read-only URI mode (`file:<path>?mode=ro`) to prevent
  database locks or write conflicts with the running OpenCode process.
- No PTY dependencies here.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW

_MAX_EVENT_CHARS = 16000
_SESSION_PREVIEW_CHARS = 140
_TEAMMATE_TASK_PREFIXES = ("[ROLE:", "[SESSION GOAL")
_REMOTE_PREFIX = "[remote → lead] "

_TOOL_ACTIVITY: dict[str, str] = {
    "read": "reading",
    "glob": "reading",
    "grep": "reading",
    "edit": "editing",
    "write": "editing",
    "todowrite": "editing",
    "notebookedit": "editing",
    "bash": "running",
    "powershell": "running",
    "webfetch": "web",
    "websearch": "web",
    "task": "delegating",
    "agent": "delegating",
    "workflow": "delegating",
    "skill": "skill",
    "question": "working",
}

_INSTALL_HINT = (
    "opencode binary not on PATH. Install with `npm install -g opencode-ai`, "
    "then run `opencode auth login` once to connect a model provider."
)


def _default_opencode_paths() -> list[Path]:
    """Known fallback install locations for `opencode`."""
    candidates: list[Path] = []
    home = Path.home()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "opencode" / "bin" / "opencode.exe")
            candidates.append(Path(local) / "opencode" / "bin" / "opencode.cmd")
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "npm" / "opencode.cmd")
            candidates.append(Path(appdata) / "npm" / "opencode.exe")
        candidates.append(home / ".opencode" / "bin" / "opencode.exe")
    else:
        candidates.append(home / ".opencode" / "bin" / "opencode")
        candidates.append(home / ".local" / "bin" / "opencode")
        candidates.append(Path("/opt/homebrew/bin/opencode"))
        candidates.append(Path("/usr/local/bin/opencode"))
    return candidates


def find_opencode_executable() -> str | None:
    """Return the absolute path to the `opencode` binary, or None when it
    can't be located."""
    for name in ("opencode", "opencode.cmd", "opencode.exe"):
        on_path = shutil.which(name)
        if on_path:
            return on_path
    for candidate in _default_opencode_paths():
        if candidate.is_file():
            return str(candidate)
    return None


def opencode_db_path() -> Path | None:
    """Locate OpenCode's SQLite database (`opencode.db`).

    An installed cockpit spawns OpenCode with ``XDG_DATA_HOME`` pointed into
    DATA_HOME (``config.provider_home_env("opencode")``), so that location is
    checked FIRST — before the inherited XDG value or the OS default — for
    the same reason ``codex_helper.codex_home`` is isolation-first: the
    reader must land where the pane writes, or the Remote mirror reads a
    database nothing updates. The legacy locations stay in the candidate
    list below, so a pre-isolation install's existing history is still found
    until OpenCode has written to the new home.
    """
    override = os.environ.get("OPENCODE_DB_PATH", "").strip()
    if override:
        p = Path(override)
        if p.is_file():
            return p

    home = Path.home()
    candidates: list[Path] = []
    from . import config

    isolated = config.provider_home_dir("opencode", "XDG_DATA_HOME")
    if isolated is not None:
        candidates.append(isolated / "opencode" / "opencode.db")
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "opencode" / "opencode.db")
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "opencode" / "opencode.db")
        candidates.append(home / ".local" / "share" / "opencode" / "opencode.db")
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        if xdg_data:
            candidates.append(Path(xdg_data) / "opencode" / "opencode.db")
        candidates.append(home / ".local" / "share" / "opencode" / "opencode.db")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0] if candidates else None


def normalize_opencode_cwd(value: object) -> str:
    """Normalize a directory path for matching `session.directory`."""
    if not isinstance(value, str) or not value.strip():
        return ""
    normalized = os.path.normcase(os.path.abspath(os.path.expanduser(value.strip())))
    normalized = normalized.replace("\\", "/").rstrip("/")
    if sys.platform in ("win32", "darwin"):
        normalized = normalized.lower()
    return normalized


def _connect_ro(db_path: Path) -> sqlite3.Connection | None:
    """Open read-only connection to SQLite database."""
    if not db_path or not db_path.is_file():
        return None
    try:
        # uri=True with mode=ro avoids obtaining write locks or conflicting with OpenCode
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except (sqlite3.Error, OSError):
        return None


def _strip_remote_prefix(text: str) -> str:
    return text[len(_REMOTE_PREFIX) :] if text.startswith(_REMOTE_PREFIX) else text


def _live_user_payload(text: str) -> list[dict]:
    if not text:
        return []
    remote = text.startswith(_REMOTE_PREFIX)
    clean = _strip_remote_prefix(text).strip()
    return [{"text": clean[:_MAX_EVENT_CHARS], "remote": remote}] if clean else []


# Cache for resolved (db_path, cwd, session_uuid, not_before) -> session_id.
#
# Entries carry an expiry because the cache key is CONSTANT for the whole life
# of a pane (the db path, the cwd, the empty uuid and the spawn timestamp never
# change) while the answer is not: OpenCode can start a fresh session inside a
# pane that is still running — `/new` in its TUI, or a `session_id` rotation
# after a compaction — and an entry with no expiry pinned the Remote mirror to
# the retired session until the cockpit was restarted, with no error anywhere.
# A hit resolved from an EXPLICIT session id is exempt: it verified that this
# id belongs to this cwd, and a session never changes directory, so that fact
# cannot go stale.
#
# The TTL is short because re-resolving is one indexed sqlite read, and the
# notifier already throttles how often it asks (`_UUIDLESS_RESYNC_THROTTLE_S`).
_OPENCODE_RESOLVE_TTL_S = 10.0
_OPENCODE_RESOLVE_CACHE: dict[tuple[str, str, str, int], tuple[Path, str, float]] = {}
_LAST_RESOLVED_SESSION_BY_PROJECT: dict[str, str] = {}


def resolve_opencode_session(
    cwd: str,
    session_id: str | None = None,
    not_before: float = 0.0,
    *,
    db_path: Path | None = None,
) -> tuple[Path, str] | None:
    """Resolve OpenCode's `opencode.db` path and session id matching `cwd`."""
    wanted_cwd = normalize_opencode_cwd(cwd)
    if not wanted_cwd:
        return None

    real_db = db_path if db_path is not None else opencode_db_path()
    if real_db is None or not real_db.is_file():
        return None

    wanted_uuid = str(session_id or "").strip()
    cache_key = (str(real_db.resolve()), wanted_cwd, wanted_uuid, int(not_before or 0.0))
    cached = _OPENCODE_RESOLVE_CACHE.get(cache_key)
    if cached is not None:
        cached_db, cached_sid, expires_at = cached
        if cached_db.is_file() and (expires_at == 0.0 or time.monotonic() < expires_at):
            return cached_db, cached_sid
        del _OPENCODE_RESOLVE_CACHE[cache_key]

    conn = _connect_ro(real_db)
    if conn is None:
        return None

    try:
        cur = conn.cursor()
        if wanted_uuid:
            cur.execute("SELECT id, directory FROM session WHERE id = ?", (wanted_uuid,))
            row = cur.fetchone()
            if row is not None:
                row_dir = normalize_opencode_cwd(row["directory"])
                if row_dir == wanted_cwd:
                    # expiry 0.0 = never: an explicit id verified against its
                    # own directory is a fact, not a "newest right now" guess.
                    _OPENCODE_RESOLVE_CACHE[cache_key] = (real_db, wanted_uuid, 0.0)
                    return real_db, wanted_uuid

        earliest_ms = max(0, int((float(not_before or 0.0) - 15.0) * 1000))
        cur.execute(
            "SELECT id, directory, time_updated FROM session ORDER BY time_updated DESC LIMIT 50"
        )
        for row in cur.fetchall():
            if earliest_ms and row["time_updated"] < earliest_ms:
                break
            if normalize_opencode_cwd(row["directory"]) == wanted_cwd:
                sid = str(row["id"])
                # "newest session for this cwd" — true now, not forever.
                _OPENCODE_RESOLVE_CACHE[cache_key] = (
                    real_db,
                    sid,
                    time.monotonic() + _OPENCODE_RESOLVE_TTL_S,
                )
                return real_db, sid
    except (sqlite3.Error, OSError):
        return None
    finally:
        conn.close()

    return None


def read_opencode_session_messages(
    db_path: Path,
    session_id: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Read recent conversation messages for `session_id` from OpenCode's database."""
    conn = _connect_ro(db_path)
    if conn is None:
        return []

    try:
        cur = conn.cursor()
        sid = session_id
        if not sid:
            # Fallback to the latest updated session in this db
            cur.execute("SELECT id FROM session ORDER BY time_updated DESC LIMIT 1")
            row = cur.fetchone()
            if row is None:
                return []
            sid = row["id"]

        cur.execute(
            """
            SELECT m.id, json_extract(m.data, '$.role') AS role,
                   p.id AS part_id, p.time_created, p.data
            FROM message m
            JOIN part p ON p.message_id = m.id
            WHERE m.session_id = ?
            ORDER BY m.time_created ASC, p.time_created ASC
            """,
            (sid,),
        )
        out: list[dict] = []
        for row in cur.fetchall():
            role = row["role"]
            try:
                pdata = json.loads(row["data"])
            except (json.JSONDecodeError, TypeError):
                continue
            ptype = pdata.get("type")
            if ptype != "text":
                continue
            text = (pdata.get("text") or "").strip()
            if not text:
                continue
            if role == "user":
                clean = _strip_remote_prefix(text)
                out.append({"text": clean[:_MAX_EVENT_CHARS], "kind": "me"})
            elif role == "assistant":
                out.append({"text": text[:_MAX_EVENT_CHARS], "kind": "lead"})
        return out[-limit:]
    except (sqlite3.Error, OSError):
        return []
    finally:
        conn.close()


def read_opencode_token_usage(db_path: Path, session_id: str) -> dict | None:
    """Return the unified token_meter usage dict for the most recent
    assistant message in *session_id*.

    Verified against a real `opencode.db` (opencode-ai, 2026-08-30): an
    assistant `message.data` row carries `tokens: {total, input, output,
    reasoning, cache: {write, read}}` and `modelID` directly — no separate
    event stream to scan, unlike codex/claude's JSONL turn logs. `prompt` =
    `input + cache.write + cache.read` (context occupancy), mirroring the
    same `input + cache_creation + cache_read` shape token_meter.read_last_usage
    uses for claude.

    OpenCode reports no context-window size in this row, so `limit` is
    always None here — `token_meter.format_token_badge` falls back to its
    own per-model table default in that case (no per-backend table added
    here: opencode fans out to 75+ model backends via `-m provider/model`,
    and inventing sizes per backend is exactly the kind of guess the
    schema-drift policy forbids).
    """
    conn = _connect_ro(db_path)
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT data FROM message
            WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant'
            ORDER BY time_created DESC LIMIT 1
            """,
            (session_id,),
        )
        row = cur.fetchone()
    except (sqlite3.Error, OSError):
        return None
    finally:
        conn.close()

    if row is None:
        return {"status": "no_data", "model": None, "reason": "no assistant message logged yet"}
    try:
        data = json.loads(row["data"])
    except (json.JSONDecodeError, TypeError):
        return {"status": "no_data", "model": None, "reason": "assistant message row unparseable"}

    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return {
            "status": "no_data",
            "model": data.get("modelID") if isinstance(data, dict) else None,
            "reason": "assistant message missing tokens block (schema drift)",
        }
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    inp = int(tokens.get("input") or 0)
    cache_write = int(cache.get("write") or 0)
    cache_read = int(cache.get("read") or 0)
    out = int(tokens.get("output") or 0)
    prompt = inp + cache_write + cache_read
    return {
        "status": "ok",
        "model": data.get("modelID") or "opencode",
        "input": inp,
        "cache_creation": cache_write,
        "cache_read": cache_read,
        "output": out,
        "prompt": prompt,
        "total": prompt + out,
        "limit": None,
    }


def list_recent_opencode_sessions(
    cwd: str,
    limit: int = 10,
    *,
    db_path: Path | None = None,
) -> list[dict]:
    """List recent Lead sessions for `cwd` from OpenCode's database."""
    wanted_cwd = normalize_opencode_cwd(cwd)
    if not wanted_cwd:
        return []
    real_db = db_path if db_path is not None else opencode_db_path()
    if real_db is None or not real_db.is_file():
        return []

    conn = _connect_ro(real_db)
    if conn is None:
        return []

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, directory, title, time_updated FROM session ORDER BY time_updated DESC LIMIT 50"
        )
        out: list[dict] = []
        for row in cur.fetchall():
            if normalize_opencode_cwd(row["directory"]) != wanted_cwd:
                continue
            sid = str(row["id"])
            mtime = float(row["time_updated"]) / 1000.0

            # Query the first user text part to get session preview
            cur.execute(
                """
                SELECT p.data
                FROM message m
                JOIN part p ON p.message_id = m.id
                WHERE m.session_id = ? AND json_extract(m.data, '$.role') = 'user' AND json_extract(p.data, '$.type') = 'text'
                ORDER BY m.time_created ASC, p.time_created ASC
                LIMIT 1
                """,
                (sid,),
            )
            prow = cur.fetchone()
            preview = ""
            if prow is not None:
                try:
                    pdata = json.loads(prow["data"])
                    preview = _strip_remote_prefix(str(pdata.get("text") or "")).strip()
                except Exception:
                    preview = ""

            if preview.startswith(_TEAMMATE_TASK_PREFIXES):
                continue

            out.append(
                {
                    "uuid": sid,
                    "mtime": mtime,
                    "preview": preview[:_SESSION_PREVIEW_CHARS],
                }
            )
            if len(out) >= limit:
                break
        return out
    except (sqlite3.Error, OSError):
        return []
    finally:
        conn.close()


def _opencode_part_settled(pdata: dict) -> bool:
    """Whether an OpenCode part has finished being written.

    OpenCode inserts a `text`/`reasoning` part the moment the model starts
    emitting it and then keeps UPDATING that same row while the tokens stream
    in — measured on a live session: `step-start`/`step-finish` land with
    `time_updated - time_created` of 1-2 ms, while `text` rows take 660-720 ms
    and `reasoning` rows 850-1370 ms to fill. The row carries its own
    `time: {"start": …, "end": …}` and **`end` appears only once the stream
    for that part is complete**, so it is an exact, poll-rate-independent
    completion signal — no quiet-period heuristic needed.

    Parts with no `time` block at all (`step-start`, `step-finish`, `tool`)
    are written once and are settled on sight.
    """
    time_block = pdata.get("time")
    if not isinstance(time_block, dict):
        return True
    return time_block.get("end") is not None


def get_opencode_latest_part_time(db_path: Path, session_id: str) -> int:
    """Return the newest write timestamp for `session_id` in `opencode.db`.

    `COALESCE(time_updated, time_created)` — not the bare `time_created` this
    used to read — so it lines up with the cursor `poll_opencode_delta` keeps
    (see its docstring for why an immutable-`time_created` cursor silently
    dropped every streamed reply).
    """
    conn = _connect_ro(db_path)
    if conn is None:
        return 0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT MAX(COALESCE(time_updated, time_created)) FROM part WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        val = row[0] if row is not None and row[0] is not None else 0
        return int(val)
    except (sqlite3.Error, OSError):
        return 0
    finally:
        conn.close()


def poll_opencode_delta(
    db_path: Path,
    session_id: str,
    since_time_ms: int = 0,
    *,
    emitted: set[str] | None = None,
) -> tuple[int, list[tuple[str, object]]]:
    """Poll incremental parts for `session_id` written after `since_time_ms`.

    Returns `(new_cursor_ms, [(event_type, payload), ...])`.
    `event_type` is one of `"lead"`, `"user"`, `"working"`, `"blocked_on_picker"`.

    **Why the cursor is `time_updated`, not `time_created`:** OpenCode creates
    a `text` part and then streams the tokens into that same row over the next
    ~700 ms (see `_opencode_part_settled`). The original implementation
    filtered on `p.time_created` — which never changes — *and* advanced the
    cursor past every row it looked at, including one whose text was still
    empty. With the notifier polling every 200 ms, the first poll inside that
    ~700 ms window pushed the cursor past the row, so the finished reply was
    never queried again: the phone got a truncated reply, or (when the row was
    still empty) nothing at all while the preceding tool part left the "…"
    spinner up forever. This is the sqlite equivalent of the half-written
    trailing line the JSONL tails already hold back in `_Tail.partial`.

    Two guards replace it:

    * an **unsettled part never lets the cursor past it** — its `time_updated`
      keeps bumping, so it is re-queried on each poll until `time.end` lands;
    * `emitted` (part ids already pushed) makes the re-queries idempotent, so
      holding the cursor back cannot double-push anything.

    Pass the same `emitted` set across polls of one session (the caller owns
    it, keyed by tail); omitting it degrades to at-most-once-per-call dedupe.
    """
    conn = _connect_ro(db_path)
    if conn is None:
        return since_time_ms, []

    seen = emitted if emitted is not None else set()
    events: list[tuple[str, object]] = []
    settled_max = since_time_ms
    pending_min: int | None = None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.id, json_extract(m.data, '$.role') AS role,
                   p.id AS part_id, p.time_created, p.time_updated, p.data
            FROM message m
            JOIN part p ON p.message_id = m.id
            WHERE m.session_id = ?
              AND COALESCE(p.time_updated, p.time_created) > ?
            ORDER BY p.time_created ASC
            """,
            (session_id, since_time_ms),
        )
        for row in cur.fetchall():
            ptime = int(row["time_updated"] or row["time_created"])
            role = row["role"]
            try:
                pdata = json.loads(row["data"])
            except (json.JSONDecodeError, TypeError):
                # Unparseable rows are settled as far as this poll can tell —
                # holding the cursor on one would stall the stream forever.
                settled_max = max(settled_max, ptime)
                continue
            if not _opencode_part_settled(pdata):
                pending_min = ptime if pending_min is None else min(pending_min, ptime)
                continue
            part_id = str(row["part_id"])
            if part_id in seen:
                settled_max = max(settled_max, ptime)
                continue
            seen.add(part_id)
            settled_max = max(settled_max, ptime)
            ptype = pdata.get("type")

            if ptype == "text":
                text = (pdata.get("text") or "").strip()
                if not text:
                    continue
                if role == "user":
                    for up in _live_user_payload(text):
                        events.append(("user", up))
                elif role == "assistant":
                    events.append(("lead", text))
            elif ptype == "tool":
                tool_name = str(pdata.get("tool") or "").lower()
                if tool_name == "question":
                    state = pdata.get("state") or {}
                    status = state.get("status")
                    if status != "completed":
                        inp = state.get("input") or {}
                        questions = inp.get("questions") or []
                        if questions:
                            first_q = questions[0]
                            prompt_text = str(first_q.get("question") or "").strip()
                            options = [
                                opt.get("label", "")
                                for opt in first_q.get("options", [])
                                if opt.get("label")
                            ]
                            events.append(
                                (
                                    "blocked_on_picker",
                                    {
                                        "prompt": prompt_text[:200],
                                        "options": options[:6],
                                        "multiSelect": False,
                                    },
                                )
                            )
                            continue
                activity = _TOOL_ACTIVITY.get(tool_name, "working")
                events.append(("working", activity))
        # Never let the cursor cross a part that is still streaming: it would
        # be excluded from every later query and its finished text lost. The
        # query filtered on `> since_time_ms`, so `pending_min` is always
        # greater than it and this can only hold the cursor still, never
        # rewind it below where the caller already was.
        if pending_min is not None:
            return min(settled_max, pending_min - 1), events
        return settled_max, events
    except (sqlite3.Error, OSError):
        return since_time_ms, []
    finally:
        conn.close()


def opencode_exec(
    prompt: str,
    *,
    cwd: str | None = None,
    timeout: float = 120.0,
    model: str | None = None,
) -> tuple[bool, str]:
    """Run `opencode run "<prompt>"` non-interactively and return `(ok, output)`."""
    binary = find_opencode_executable()
    if binary is None:
        return False, _INSTALL_HINT
    if not (prompt or "").strip():
        return False, "empty prompt"
    argv: list[str] = [binary, "run", prompt]
    if model:
        argv = [binary, "-m", model, "run", prompt]
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, "opencode exec timed out"
    except FileNotFoundError:
        return False, "opencode binary disappeared from PATH"
    except Exception as e:
        return False, f"opencode exec failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "opencode exec failed").strip()
        return False, tail
    return True, (proc.stdout or "").strip()
