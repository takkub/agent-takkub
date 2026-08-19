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


# Cache for resolved (db_path, cwd, session_uuid, not_before) -> session_id
_OPENCODE_RESOLVE_CACHE: dict[tuple[str, str, str, int], tuple[Path, str]] = {}
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
    if cached is not None and cached[0].is_file():
        return cached

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
                    res = (real_db, wanted_uuid)
                    _OPENCODE_RESOLVE_CACHE[cache_key] = res
                    return res

        earliest_ms = max(0, int((float(not_before or 0.0) - 15.0) * 1000))
        cur.execute(
            "SELECT id, directory, time_updated FROM session ORDER BY time_updated DESC LIMIT 50"
        )
        for row in cur.fetchall():
            if earliest_ms and row["time_updated"] < earliest_ms:
                break
            if normalize_opencode_cwd(row["directory"]) == wanted_cwd:
                res = (real_db, str(row["id"]))
                _OPENCODE_RESOLVE_CACHE[cache_key] = res
                return res
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


def get_opencode_latest_part_time(db_path: Path, session_id: str) -> int:
    """Return the highest `p.time_created` for `session_id` in `opencode.db`."""
    conn = _connect_ro(db_path)
    if conn is None:
        return 0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT MAX(time_created) FROM part WHERE session_id = ?",
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
) -> tuple[int, list[tuple[str, object]]]:
    """Poll incremental parts for `session_id` created after `since_time_ms`.

    Returns `(new_max_time_ms, [(event_type, payload), ...])`.
    `event_type` is one of `"lead"`, `"user"`, `"working"`, `"blocked_on_picker"`.
    """
    conn = _connect_ro(db_path)
    if conn is None:
        return since_time_ms, []

    events: list[tuple[str, object]] = []
    max_time = since_time_ms
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.id, json_extract(m.data, '$.role') AS role,
                   p.id AS part_id, p.time_created, p.data
            FROM message m
            JOIN part p ON p.message_id = m.id
            WHERE m.session_id = ? AND p.time_created > ?
            ORDER BY p.time_created ASC
            """,
            (session_id, since_time_ms),
        )
        for row in cur.fetchall():
            ptime = int(row["time_created"])
            if ptime > max_time:
                max_time = ptime
            role = row["role"]
            try:
                pdata = json.loads(row["data"])
            except (json.JSONDecodeError, TypeError):
                continue
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
        return max_time, events
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
