"""OpenAI Codex CLI wrapper — non-interactive one-shot mode.

This is Option D from the Codex integration plan: a thin wrapper
that lets the user fire Codex via the cockpit's `takkub` CLI for
quick second-opinion / refactor / review questions without
spawning a full pane. No PTY, no orchestrator IPC — just
`subprocess.run(["codex", "exec", "<prompt>"])` with the prompt
text routed through and the result printed back.

Auth is whatever Codex itself uses (ChatGPT login via `codex login`
or `OPENAI_API_KEY` env var). The cockpit never touches Codex's
credentials. If Codex isn't logged in, its own stderr surfaces the
error verbatim and the user runs `codex login` once.

Design rules (mirror update_helper.py):
- Best-effort. Any failure returns `(False, <reason>)`.
- subprocess.run with cwd specified, never shell=True, default
  timeout 120 s (longer than git ops because Codex can think).
- No file writes by this module. Codex writes its own session
  artefacts under `~/.codex/` independently.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW


def codex_home() -> Path:
    """Return the CODEX_HOME an installed cockpit's codex panes actually use.

    Precedence — deliberately isolation-first:

    1. ``config.provider_home_env("codex")`` (installed build) — the same
       value ``pane_env.inject_provider_home_env`` exports into the pane, so
       this reader can never point somewhere no pane writes to.
    2. an inherited ``CODEX_HOME`` (dev checkout, or a user who set it).
    3. Codex's own default, ``~/.codex``.

    Order 1-before-2 matters: the cockpit process may itself have inherited a
    ``CODEX_HOME`` from the user's shell, but the pane's value is assigned,
    not defaulted — reading the inherited one here would send the Remote
    mirror hunting in a directory the pane never writes to.
    """
    from . import config

    isolated = config.provider_home_dir("codex", "CODEX_HOME")
    if isolated is not None:
        return isolated
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured) if configured else Path.home() / ".codex"


def codex_sessions_root() -> Path:
    """Return Codex's provider-owned interactive session store.

    ``CODEX_HOME`` is part of Codex's local-state contract; when it is not
    set the CLI uses ``~/.codex``.  Keeping this resolver in the core adapter
    lets both the optional Remote package and the spawn engine validate the
    same store without making core orchestration import ``agent_takkub.remote``.
    """
    return codex_home() / "sessions"


def codex_archived_sessions_root() -> Path:
    """Return Codex's archived-session store (``codex archive``, 0.148+).

    Verified on-disk (0.148.0): ``codex archive <id>`` MOVES the rollout file
    out of the day-sharded ``sessions/YYYY/MM/DD/`` tree into a flat
    ``archived_sessions/`` directory — no date subfolders, and `unarchive`
    moves it straight back. `codex_sessions_root()`'s day-sharded resolvers
    never look here, so an exact by-ID lookup for a session that got archived
    mid-flight would otherwise resolve to nothing (not an error — a silent
    "session vanished", the exact failure mode `provider-integration`'s row 4
    warns about). Callers doing an exact id+cwd match should fall back here
    when the primary root misses; the broader "newest session for this cwd"
    scan never needs to, because a session actively being spawned/resumed
    can't already be archived.
    """
    return codex_home() / "archived_sessions"


def normalize_codex_cwd(value: object) -> str:
    """Normalize a session-metadata cwd for exact, platform-aware matching."""
    if not isinstance(value, str) or not value.strip():
        return ""
    return os.path.normcase(os.path.abspath(os.path.expanduser(value.strip())))


def read_codex_session_meta(path: Path) -> dict:
    """Read the first ``session_meta`` payload from a Codex rollout file."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline().strip()
        rec = json.loads(first) if first else {}
    except (OSError, ValueError, TypeError):
        return {}
    payload = rec.get("payload") if isinstance(rec, dict) else None
    return payload if rec.get("type") == "session_meta" and isinstance(payload, dict) else {}


def resolve_codex_jsonl_for_cwd(
    cwd: str,
    session_id: str,
    *,
    root: Path | None = None,
) -> Path | None:
    """Resolve *session_id* only when its recorded cwd exactly matches *cwd*.

    The Remote resume endpoint calls this before closing a live Lead pane and
    the spawn engine calls it again before constructing ``codex resume``.
    Returning ``None`` for corrupt metadata, missing IDs, or cwd mismatch
    prevents a mobile client from resuming another project's conversation.
    """
    wanted_cwd = normalize_codex_cwd(cwd)
    wanted_id = str(session_id or "").strip()
    if not wanted_cwd or not wanted_id:
        return None
    # An explicit `root` (tests) searches only that root, no archived
    # fallback — callers relying on the real store also check `archived_sessions/`
    # (see `codex_archived_sessions_root`) since `codex archive` moves the file
    # there and this is an exact id+cwd lookup, not the newest-for-cwd scan.
    bases = [root] if root is not None else [codex_sessions_root(), codex_archived_sessions_root()]
    for base in bases:
        if not base.is_dir():
            continue
        try:
            candidates = base.rglob("rollout-*.jsonl")
            for path in candidates:
                meta = read_codex_session_meta(path)
                meta_id = str(meta.get("id") or meta.get("session_id") or "").strip()
                if meta_id != wanted_id:
                    continue
                if normalize_codex_cwd(meta.get("cwd")) == wanted_cwd:
                    return path
        except OSError:
            continue
    return None


def resolve_newest_codex_session_for_cwd(
    cwd: str,
    *,
    not_before: float = 0.0,
    root: Path | None = None,
) -> Path | None:
    """Newest codex rollout file recorded against *cwd*, with no known session
    id yet — the token meter's per-pane resolution path.

    Unlike claude, codex chooses its own session id after boot (there is no
    `--session-id`-equivalent flag the cockpit can pass at spawn, see
    `spawn_engine.py`'s generic provider branch), so a fresh pane has nothing
    exact to look up by. `not_before` (the pane's spawn timestamp) bounds the
    day-sharded walk so a stale prior run in the same cwd is never mistaken
    for the live one.

    Same isolation caveat as `token_meter.find_latest_session` (issue #129):
    a newest-match heuristic can't tell two panes sharing one cwd apart. A
    provider pane spawned into its own worktree (the common case in this
    cockpit) is unaffected.
    """
    wanted_cwd = normalize_codex_cwd(cwd)
    if not wanted_cwd:
        return None
    base = root if root is not None else codex_sessions_root()
    if not base.is_dir():
        return None

    from datetime import date as _date
    from datetime import datetime

    cutoff_date = None
    if not_before:
        cutoff_date = datetime.fromtimestamp(max(0.0, float(not_before) - 86400.0)).date()

    try:
        years = sorted((d for d in base.iterdir() if d.is_dir() and d.name.isdigit()), reverse=True)
    except OSError:
        return None
    for year in years:
        try:
            months = sorted(
                (d for d in year.iterdir() if d.is_dir() and d.name.isdigit()), reverse=True
            )
        except OSError:
            continue
        for month in months:
            try:
                days = sorted(
                    (d for d in month.iterdir() if d.is_dir() and d.name.isdigit()), reverse=True
                )
            except OSError:
                continue
            for day in days:
                if cutoff_date is not None:
                    try:
                        stamp = _date(int(year.name), int(month.name), int(day.name))
                    except ValueError:
                        stamp = None
                    if stamp is not None and stamp < cutoff_date:
                        return None
                try:
                    files = sorted(
                        day.glob("rollout-*.jsonl"),
                        key=lambda p: p.stat().st_mtime if p.is_file() else 0.0,
                        reverse=True,
                    )
                except OSError:
                    continue
                for f in files:
                    meta = read_codex_session_meta(f)
                    if normalize_codex_cwd(meta.get("cwd")) == wanted_cwd:
                        return f
    return None


# The token badge refreshes every 5 s per pane and only needs the most recent
# `token_count` event, which sits near EOF. Rollout files reach several MB on
# a long-running pane, so — same rationale as token_meter._TAIL_SCAN_BYTES —
# scan only the tail and fall back to a full scan only on a miss.
_CODEX_TAIL_SCAN_BYTES = 512 * 1024


def _scan_lines_for_codex_token_count(lines) -> dict | None:
    """Return the last `event_msg.payload.info` block seen in `lines`, or None."""
    last_info: dict | None = None
    for line in lines:
        if not line.strip():
            continue
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue
        if j.get("type") != "event_msg":
            continue
        payload = j.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if isinstance(info, dict):
            last_info = info
    return last_info


def read_codex_token_usage(jsonl: Path) -> dict | None:
    """Return the unified token_meter usage dict for the most recent
    `token_count` event in a codex rollout JSONL.

    Verified against a real rollout (codex-cli 0.151.0, 2026-08-30):
    `{"type":"event_msg","payload":{"type":"token_count","info":
    {"last_token_usage":{"input_tokens","cached_input_tokens",
    "cache_write_input_tokens","output_tokens","reasoning_output_tokens",
    "total_tokens"},"model_context_window":<int>}}}`.

    `prompt` = `last_token_usage.input_tokens + .cached_input_tokens` — the
    tokens actually sent in the most recent turn's request, mirroring
    claude's `input + cache_creation + cache_read` (context occupancy), NOT
    `total_token_usage` (that key is the whole session's cumulative sum).
    `limit` comes straight from the event's own `model_context_window` —
    codex reports it live, so no static per-model table is needed the way
    claude's is (see token_meter._MODEL_LIMITS).

    Returns `{"status": "no_data", ...}` when the file has no token_count
    event yet (fresh session, first turn still in flight), and
    `{"status": "no_data", ...}` with a schema-drift reason if a future codex
    release renames `last_token_usage`/`model_context_window` — never raises.
    """
    try:
        size = jsonl.stat().st_size
    except OSError:
        return None

    last_info: dict | None = None
    if size > _CODEX_TAIL_SCAN_BYTES:
        try:
            with open(jsonl, "rb") as f:
                f.seek(size - _CODEX_TAIL_SCAN_BYTES)
                raw = f.read()
            nl = raw.find(b"\n")
            if nl != -1:
                raw = raw[nl + 1 :]
            last_info = _scan_lines_for_codex_token_count(
                raw.decode("utf-8", "replace").splitlines()
            )
        except OSError:
            last_info = None

    if last_info is None:
        try:
            with jsonl.open("r", encoding="utf-8", errors="replace") as f:
                last_info = _scan_lines_for_codex_token_count(f)
        except OSError:
            return None

    if not last_info:
        return {"status": "no_data", "model": "codex", "reason": "no token_count event logged yet"}

    last = last_info.get("last_token_usage")
    if not isinstance(last, dict):
        return {
            "status": "no_data",
            "model": "codex",
            "reason": "token_count event missing last_token_usage (schema drift)",
        }
    inp = int(last.get("input_tokens") or 0)
    cr = int(last.get("cached_input_tokens") or 0)
    out = int(last.get("output_tokens") or 0)
    prompt = inp + cr
    limit = last_info.get("model_context_window")
    return {
        "status": "ok",
        "model": "codex",
        "input": inp,
        "cache_creation": 0,
        "cache_read": cr,
        "output": out,
        "prompt": prompt,
        "total": prompt + out,
        "limit": int(limit) if isinstance(limit, (int, float)) and limit else None,
    }


def find_codex_executable() -> str | None:
    """Return the absolute path to the `codex` binary, or None when
    it isn't on PATH. Caller surfaces a friendly "install with
    `npm install -g @openai/codex`" message in the None case.

    On Windows npm installs `codex` as a `.cmd` shim alongside the Node
    script; `shutil.which` resolves to that shim (verified on this
    machine's install — `where codex` → `codex.CMD`).

    L4 (cross-platform audit 2026-07-10): invoking the `.cmd` shim through
    pywinpty/ConPTY briefly flashes a visible cmd.exe console window
    (mitigated but not eliminated by `_win_console`'s hwnd-sweep).
    `find_claude_executable` avoids this by preferring claude's real
    `.exe` inside its npm package; codex ships the same shape — the
    `@openai/codex` package vendors a native `codex.exe` under a nested
    platform-specific optional-dependency package (confirmed present on
    this machine: `@openai/codex/node_modules/@openai/codex-win32-x64/
    vendor/x86_64-pc-windows-msvc/bin/codex.exe`). Prefer that binary when
    found; fall back to the `.cmd` shim (console flash, but still works)
    when it isn't — e.g. an older codex release without the vendored exe.
    """
    cmd_path = shutil.which("codex")
    if sys.platform == "win32" and cmd_path:
        cmd_dir = Path(cmd_path).resolve().parent
        native = (
            cmd_dir
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
            / "codex-win32-x64"
            / "vendor"
            / "x86_64-pc-windows-msvc"
            / "bin"
            / "codex.exe"
        )
        if native.is_file():
            return str(native)
    return cmd_path


def codex_exec(
    prompt: str,
    *,
    cwd: str | None = None,
    timeout: float = 120.0,
    model: str | None = None,
) -> tuple[bool, str]:
    """Run `codex exec "<prompt>"` and return `(ok, output)`.

    Output is Codex's combined stdout+stderr trimmed — for a
    successful exec the stdout carries the model's response, and
    for failures stderr carries the error. We pass both back so
    the caller can just print the result whichever path it took.

    `cwd` lets the caller scope Codex to a specific project (handy
    for "review this codebase" prompts). Defaults to the process
    cwd so `takkub codex` from inside any pane targets that pane's
    project naturally.

    `model` is optional and gets forwarded as `--model <name>`;
    when None, Codex uses whatever its config defaults to.

    `timeout` is generous (120 s) because Codex's reasoning runs
    can be long. Timeout returns (False, "codex exec timed out").
    """
    binary = find_codex_executable()
    if binary is None:
        return False, (
            "codex binary not on PATH. Install with "
            "`npm install -g @openai/codex`, then run `codex login` once."
        )
    if sys.platform == "win32" and Path(binary).suffix.lower() in {".cmd", ".bat"}:
        return False, (
            "native codex.exe not found; refusing the Windows command shim "
            "because prompts cannot be passed to .cmd/.bat safely"
        )
    if not (prompt or "").strip():
        return False, "empty prompt"
    argv: list[str] = [binary, "exec", prompt]
    if model:
        # Insert before the prompt so positional parsing in clap
        # treats `<prompt>` as the trailing positional, not as a
        # value for --model.
        argv = [binary, "exec", "--model", model, prompt]
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
        return False, "codex exec timed out"
    except FileNotFoundError:
        # PATH had the binary at which() time but it disappeared
        # between probes — rare but worth surfacing distinctly.
        return False, "codex binary disappeared from PATH"
    except Exception as e:
        return False, f"codex exec failed: {e}"
    if proc.returncode != 0:
        # Codex sometimes writes the response to stdout AND an
        # error to stderr (rate-limit, auth blob expired). Hand
        # back whichever has content so the caller has something
        # to show the user.
        tail = (proc.stderr or proc.stdout or "codex exec failed").strip()
        return False, tail
    return True, (proc.stdout or "").strip()
