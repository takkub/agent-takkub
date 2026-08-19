"""Cursor CLI (`cursor-agent` / `agent`) helper — transcript resolution, history, and live tailing.

Backs the cockpit's `cursor` provider role and Lead remote mirror. Cursor CLI
stores conversation JSONL transcripts under:
  ~/.cursor/projects/<encoded-cwd>/agent-transcripts/<session-uuid>/<session-uuid>.jsonl

Design rules (mirror codex_helper.py / gemini_helper.py / opencode_helper.py):
- Best-effort. Any file/parse failure returns clean fallback, never raises.
- Read-only. No modifications to ~/.cursor/ from here.
- Cross-platform paths via pathlib.Path (macOS, Linux, Windows).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW

_MAX_EVENT_CHARS = 16000
_SESSION_PREVIEW_CHARS = 140
_TEAMMATE_TASK_PREFIXES = ("[ROLE:", "[SESSION GOAL")
_REMOTE_PREFIX = "[remote → lead] "
_HISTORY_MAX_BYTES = 256 * 1024

_TOOL_ACTIVITY: dict[str, str] = {
    "read": "reading",
    "readfile": "reading",
    "read_file": "reading",
    "glob": "reading",
    "grep": "reading",
    "rg": "reading",
    "search": "reading",
    "semanticsearch": "reading",
    "semantic_search": "reading",
    "edit": "editing",
    "editfile": "editing",
    "edit_file": "editing",
    "write": "editing",
    "writefile": "editing",
    "write_file": "editing",
    "todowrite": "editing",
    "notebookedit": "editing",
    "bash": "running",
    "shell": "running",
    "powershell": "running",
    "terminal": "running",
    "webfetch": "web",
    "websearch": "web",
    "task": "delegating",
    "agent": "delegating",
    "workflow": "delegating",
    "skill": "skill",
    "question": "working",
}

_cursor_proj_dir_cache: dict[str, Path | None] = {}


def _default_cursor_paths() -> list[Path]:
    """Known fallback install locations for `cursor-agent` / `agent`."""
    candidates: list[Path] = []
    home = Path.home()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "cursor" / "bin" / "agent.exe")
            candidates.append(Path(local) / "cursor" / "bin" / "cursor-agent.exe")
            candidates.append(
                Path(local) / "Programs" / "cursor" / "resources" / "app" / "bin" / "cursor.cmd"
            )
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "npm" / "cursor-agent.cmd")
            candidates.append(Path(appdata) / "npm" / "agent.cmd")
        candidates.append(home / ".cursor" / "bin" / "agent.exe")
        candidates.append(home / ".cursor" / "bin" / "cursor-agent.exe")
    else:
        candidates.append(home / ".local" / "bin" / "cursor-agent")
        candidates.append(home / ".local" / "bin" / "agent")
        candidates.append(home / ".cursor" / "bin" / "cursor-agent")
        candidates.append(home / ".cursor" / "bin" / "agent")
        candidates.append(Path("/opt/homebrew/bin/cursor-agent"))
        candidates.append(Path("/opt/homebrew/bin/agent"))
        candidates.append(Path("/usr/local/bin/cursor-agent"))
        candidates.append(Path("/usr/local/bin/agent"))
    return candidates


def find_cursor_executable() -> str | None:
    """Return the absolute path to the `cursor-agent` / `agent` binary, or None when it
    can't be located."""
    for name in ("cursor-agent", "cursor-agent.exe", "agent", "agent.exe"):
        on_path = shutil.which(name)
        if on_path:
            return on_path
    for candidate in _default_cursor_paths():
        if candidate.is_file():
            return str(candidate)
    return None


def cursor_projects_root() -> Path:
    """Return Cursor's per-project transcript root directory (`~/.cursor/projects`)."""
    override = os.environ.get("CURSOR_HOME", "").strip()
    if override:
        return Path(override) / "projects"
    return Path.home() / ".cursor" / "projects"


def normalize_cursor_cwd(value: object) -> str:
    """Normalize a cwd path for robust matching across OS separators and case."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        p = Path(value.strip()).resolve()
        norm = p.as_posix().rstrip("/")
        if sys.platform in ("win32", "darwin"):
            norm = norm.lower()
        return norm
    except Exception:
        return str(value).strip().replace("\\", "/").rstrip("/").lower()


def find_cursor_project_dir(cwd: str, *, root: Path | None = None) -> Path | None:
    """Locate the Cursor project directory under `~/.cursor/projects/` corresponding to *cwd*."""
    norm_cwd = normalize_cursor_cwd(cwd)
    if not norm_cwd:
        return None

    base_root = root if root is not None else cursor_projects_root()
    if not base_root.is_dir():
        return None

    if root is None:
        cached = _cursor_proj_dir_cache.get(norm_cwd)
        if cached is not None:
            if cached.is_dir():
                return cached
            _cursor_proj_dir_cache.pop(norm_cwd, None)

    try:
        p = Path(cwd).resolve()
        posix_path = p.as_posix()
        encoded = re.sub(r"[^a-zA-Z0-9]+", "-", posix_path).strip("-")

        # 1. Direct encoded directory
        direct = base_root / encoded
        if direct.is_dir():
            if root is None:
                _cursor_proj_dir_cache[norm_cwd] = direct
            return direct

        # 2. Case-insensitive or Windows drive variants / .workspace-trusted inspection
        for item in base_root.iterdir():
            if not item.is_dir():
                continue
            trusted = item / ".workspace-trusted"
            if trusted.is_file():
                try:
                    data = json.loads(trusted.read_text(encoding="utf-8", errors="replace"))
                    ws = data.get("workspacePath")
                    if ws and normalize_cursor_cwd(ws) == norm_cwd:
                        if root is None:
                            _cursor_proj_dir_cache[norm_cwd] = item
                        return item
                except Exception:
                    pass
            if item.name.lower() == encoded.lower():
                if root is None:
                    _cursor_proj_dir_cache[norm_cwd] = item
                return item
    except OSError:
        pass

    return None


def resolve_cursor_jsonl_for_cwd(
    cwd: str,
    session_uuid: str | None = None,
    not_before: float = 0.0,
    *,
    root: Path | None = None,
) -> Path | None:
    """Resolve the JSONL transcript file for a Cursor session in *cwd*.

    If *session_uuid* is given, looks for:
      `<project_dir>/agent-transcripts/<session_uuid>/<session_uuid>.jsonl`
      or `<project_dir>/agent-transcripts/<session_uuid>/*.jsonl`
      or `<project_dir>/agent-transcripts/<session_uuid>.jsonl`.
    If *session_uuid* is omitted, returns the newest transcript matching *not_before*.
    """
    proj_dir = find_cursor_project_dir(cwd, root=root)
    if proj_dir is None:
        return None

    transcripts_dir = proj_dir / "agent-transcripts"
    if not transcripts_dir.is_dir():
        return None

    wanted_uuid = str(session_uuid or "").strip()
    if wanted_uuid:
        candidate = transcripts_dir / wanted_uuid / f"{wanted_uuid}.jsonl"
        if candidate.is_file():
            return candidate
        session_subdir = transcripts_dir / wanted_uuid
        if session_subdir.is_dir():
            try:
                for jsonl in session_subdir.glob("*.jsonl"):
                    if jsonl.is_file():
                        return jsonl
            except OSError:
                pass
        flat_candidate = transcripts_dir / f"{wanted_uuid}.jsonl"
        if flat_candidate.is_file():
            return flat_candidate

    earliest = max(0.0, float(not_before or 0.0) - 15.0) if not_before else 0.0
    best: Path | None = None
    best_mtime = 0.0

    try:
        candidates: list[Path] = []
        for item in transcripts_dir.iterdir():
            if item.is_dir():
                candidates.extend(item.glob("*.jsonl"))
            elif item.suffix == ".jsonl":
                candidates.append(item)

        for path in candidates:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if earliest and mtime < earliest:
                continue
            if mtime >= best_mtime:
                best = path
                best_mtime = mtime
    except OSError:
        pass

    return best


def parse_cursor_record_message(rec: object) -> tuple[str, str] | None:
    """Parse one Cursor JSONL record into `(kind, text)` where `kind` is `"me"` or `"lead"`.

    Returns None for tool_use-only turns, turn metadata, or unparseable lines.
    """
    if not isinstance(rec, dict):
        return None

    role = rec.get("role") or rec.get("type")
    msg = rec.get("message")
    content = None
    if isinstance(msg, dict):
        content = msg.get("content")
    elif "content" in rec:
        content = rec.get("content")

    if role == "user":
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, str):
                    texts.append(c)
                elif isinstance(c, dict) and c.get("type") == "text":
                    t = c.get("text", "")
                    if isinstance(t, str) and t:
                        texts.append(t)
        raw = "\n".join(texts).strip()
        if not raw:
            return None
        # Clean user markup like <timestamp>...</timestamp> and <user_query>...</user_query>
        cleaned = re.sub(r"<timestamp>.*?</timestamp>\s*", "", raw, flags=re.DOTALL)
        cleaned = re.sub(r"<\/?user_query>\s*", "", cleaned).strip()
        if cleaned:
            return "me", cleaned

    elif role == "assistant":
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, str):
                    texts.append(c)
                elif isinstance(c, dict) and c.get("type") == "text":
                    t = c.get("text", "")
                    if isinstance(t, str) and t:
                        t_clean = t.replace("[REDACTED]", "").strip()
                        if t_clean:
                            texts.append(t_clean)
        raw = "\n".join(texts).strip()
        if raw:
            return "lead", raw

    return None


def cursor_live_text_blocks(rec: dict) -> list[str]:
    """Extract assistant response text from a Cursor JSONL record."""
    parsed = parse_cursor_record_message(rec)
    if parsed is not None and parsed[0] == "lead":
        return [parsed[1]]
    return []


def _strip_remote_prefix(text: str) -> str:
    return text[len(_REMOTE_PREFIX) :] if text.startswith(_REMOTE_PREFIX) else text


def _live_user_payload(text: str | None) -> list[dict]:
    if not text:
        return []
    remote = text.startswith(_REMOTE_PREFIX)
    clean = _strip_remote_prefix(text).strip()
    return [{"text": clean[:_MAX_EVENT_CHARS], "remote": remote}] if clean else []


def cursor_live_users(rec: dict) -> list[dict]:
    """Extract user message payload from a Cursor JSONL record for SSE streaming."""
    parsed = parse_cursor_record_message(rec)
    if parsed is not None and parsed[0] == "me":
        return _live_user_payload(parsed[1])
    return []


def cursor_live_activity(rec: dict) -> str | None:
    """Coarse activity category for an assistant record containing tool_use blocks."""
    if not isinstance(rec, dict):
        return None
    role = rec.get("role") or rec.get("type")
    if role != "assistant":
        return None
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else rec.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = str(block.get("name") or "").lower().replace("_", "")
            return _TOOL_ACTIVITY.get(name, "working")
    return None


def cursor_first_user_preview(path: Path) -> tuple[str, str]:
    """Return `(session_uuid, preview_text)` from the first user turn in *path*."""
    session_uuid = path.stem
    if session_uuid == path.parent.name:
        session_uuid = path.parent.name

    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                parsed = parse_cursor_record_message(rec)
                if parsed is not None and parsed[0] == "me":
                    text = _strip_remote_prefix(parsed[1]).strip()
                    return session_uuid, text[:_SESSION_PREVIEW_CHARS]
    except OSError:
        pass
    return session_uuid, ""


def read_recent_cursor_messages(
    path: Path,
    limit: int = 200,
) -> list[dict]:
    """Read recent user and assistant messages from a Cursor JSONL transcript."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    truncated = size > _HISTORY_MAX_BYTES
    try:
        with path.open("rb") as fh:
            if truncated:
                fh.seek(size - _HISTORY_MAX_BYTES)
            raw = fh.read()
    except OSError:
        return []
    lines = raw.split(b"\n")
    if truncated:
        lines = lines[1:]

    out: list[dict] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        parsed = parse_cursor_record_message(rec)
        if parsed is None:
            continue
        kind, text = parsed
        if kind == "me":
            text = _strip_remote_prefix(text)
        out.append({"text": text[:_MAX_EVENT_CHARS], "kind": kind})
    return out[-limit:]


def list_recent_cursor_sessions(
    cwd: str,
    limit: int = 20,
    *,
    root: Path | None = None,
) -> list[dict]:
    """List recent Cursor sessions for *cwd*, newest first."""
    proj_dir = find_cursor_project_dir(cwd, root=root)
    if proj_dir is None:
        return []

    transcripts_dir = proj_dir / "agent-transcripts"
    if not transcripts_dir.is_dir():
        return []

    found: list[tuple[float, Path]] = []
    try:
        candidates: list[Path] = []
        for item in transcripts_dir.iterdir():
            if item.is_dir():
                candidates.extend(item.glob("*.jsonl"))
            elif item.suffix == ".jsonl":
                candidates.append(item)

        for path in candidates:
            try:
                found.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return []

    found.sort(key=lambda t: t[0], reverse=True)
    capped = max(1, min(limit, 20))
    out: list[dict] = []

    for mtime, jsonl in found:
        session_uuid, preview = cursor_first_user_preview(jsonl)
        if not session_uuid:
            continue
        if preview.startswith(_TEAMMATE_TASK_PREFIXES):
            continue
        out.append({"uuid": session_uuid, "mtime": mtime, "preview": preview})
        if len(out) >= capped:
            break

    return out


def cursor_exec(
    prompt: str,
    *,
    cwd: str | None = None,
    timeout: float = 120.0,
    model: str | None = None,
) -> tuple[bool, str]:
    """Run `agent "<prompt>"` or `cursor-agent "<prompt>"` non-interactively and return `(ok, output)`."""
    binary = find_cursor_executable()
    if binary is None:
        return False, (
            "Cursor CLI binary not on PATH. Install manually from https://cursor.com/install"
        )
    if not (prompt or "").strip():
        return False, "empty prompt"

    argv: list[str] = [binary, "-f", "--print", prompt]
    if model:
        argv = [binary, "--model", model, "-f", "--print", prompt]

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
        return False, "cursor exec timed out"
    except FileNotFoundError:
        return False, "cursor executable not found"
    except OSError as e:
        return False, f"cursor exec failed: {e}"

    combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return proc.returncode == 0, combined
