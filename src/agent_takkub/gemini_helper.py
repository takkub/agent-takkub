"""Antigravity CLI (`agy`) wrapper - non-interactive one-shot mode.

Backs the cockpit's `gemini` role. Google retired the standalone
Gemini CLI on 2026-06-18 and replaced it with the **Antigravity CLI**,
whose binary is `agy`. The role keeps its `gemini` identity (the
"third brain" Google AI planning / second-opinion slot) but is now
powered by `agy` end to end.

Mirror of codex_helper.py. Lets the user fire Antigravity via the
cockpit's `takkub gemini` command for quick second-opinion / planning
/ brainstorm questions without spawning a full pane. No PTY, no
orchestrator IPC - just `subprocess.run(["agy", "-p", "<prompt>"])`
with the prompt text routed through and the result printed back.

Auth is whatever the Antigravity CLI itself uses (Google Sign-In on
first run or `ANTIGRAVITY_API_KEY` env var). The cockpit never touches
those credentials. If `agy` isn't logged in, its own stderr surfaces
the error verbatim.

Design rules (mirror codex_helper.py):
- Best-effort. Any failure returns `(False, <reason>)`.
- subprocess.run with cwd specified, never shell=True, default
  timeout 120 s.
- No file writes by this module. `agy` writes its own session
  artefacts under `~/.antigravity/` (or `%LOCALAPPDATA%\\agy\\`)
  independently.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW

# Install hint surfaced whenever the binary is missing. Antigravity ships
# as a native binary (NOT npm) — the Windows installer drops `agy` under
# %LOCALAPPDATA%\agy\bin and adds it to PATH.
_INSTALL_HINT = (
    "agy binary not on PATH. Install the Antigravity CLI from "
    "https://antigravity.google/download (Windows installer drops `agy` "
    "under %LOCALAPPDATA%\\agy\\bin), then run `agy` once to sign in."
)


def _default_agy_paths() -> list[Path]:
    """Known fixed install locations for `agy` (PATH-independent).

    The Antigravity Windows installer drops the binary under
    %LOCALAPPDATA%\\agy\\bin\\agy.exe but does NOT reliably add that dir
    to the user PATH (observed 2026-06-19 — the installer registered a
    stale `...\\Programs\\Antigravity\\bin` that doesn't exist, leaving
    the real `agy.exe` off PATH). Probing the canonical location keeps
    the cockpit from falsely degrading the `gemini` role to a Claude
    substitute when `agy` is in fact installed and working.

    L2 (cross-platform audit 2026-07-10): this fallback used to be
    Windows-only — a mac install with `agy` not on PATH had nothing to
    probe, so `gemini` silently degraded to Claude-substitute even when
    Antigravity was genuinely installed. Adds the mac equivalents: the
    `.app` bundle's CLI shim and the two common Homebrew prefixes
    (`/opt/homebrew` on Apple Silicon, `/usr/local` on Intel).
    """
    candidates: list[Path] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "agy" / "bin" / "agy.exe")
        candidates.append(Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Antigravity.app/Contents/MacOS/agy"))
        candidates.append(Path("/opt/homebrew/bin/agy"))
        candidates.append(Path("/usr/local/bin/agy"))
        candidates.append(Path.home() / ".local" / "bin" / "agy")
    return candidates


def find_agy_executable() -> str | None:
    """Return the absolute path to the `agy` binary, or None when it
    can't be located. Caller surfaces the friendly install message in
    the None case.

    Resolution order:
      1. `shutil.which("agy")` — the binary on PATH (uses %PATHEXT% so a
         bare `agy` matches `agy.exe`/`agy.cmd`). Works when the
         Antigravity installer registered PATH correctly.
      2. Fixed install location %LOCALAPPDATA%\\agy\\bin\\agy.exe — a
         fallback for the common case where the installer dropped the
         binary but didn't put its dir on PATH.
    """
    on_path = shutil.which("agy")
    if on_path:
        return on_path
    for candidate in _default_agy_paths():
        if candidate.is_file():
            return str(candidate)
    return None


def _normalize_path_for_compare(path: str) -> str:
    """Collapse a filesystem path (or a decoded `file://` URI tail) into a
    form comparable across the two shapes agy's own project registry uses:
    forward slashes, no trailing slash, and — ONLY on the platforms where the
    filesystem itself is case-insensitive (Windows, macOS/APFS default) —
    lowercased. Linux (ext4 etc.) is case-sensitive, so `Project` and
    `project` are two different directories there; lowercasing unconditionally
    would falsely match an unrelated cwd that merely differs in case (#132
    followup)."""
    normalized = path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    normalized = normalized.rstrip("/")
    if sys.platform in ("win32", "darwin"):
        normalized = normalized.lower()
    return normalized


# ── Gemini chat-store resolution ────────────────────────────────────────────
# agy stores conversation JSONL under ~/.gemini/tmp/<opaque-name>/chats/.
# The opaque directory name is not a stable API, but each directory carries a
# `.project_root` file with the original workspace path. Keep this provider-
# owned discovery beside the rest of the agy integration so core spawn logic
# and optional remote history can share it without core importing remote/.
_gemini_chats_cache: dict[str, Path | None] = {}


def _normalize_chat_store_cwd(cwd: str) -> str:
    """Match the normalization used by the original history resolver."""
    return Path(cwd).resolve().as_posix().rstrip("/").lower()


def find_gemini_chats_dir(cwd: str) -> Path | None:
    """Locate agy's ``chats/`` directory owned by the workspace at *cwd*.

    Positive hits are cached for the process lifetime while misses are
    deliberately re-scanned: agy creates ``chats/`` lazily after the first
    user turn. A cached directory that disappears is evicted and re-resolved.
    """
    normalized_cwd = _normalize_chat_store_cwd(cwd)

    cached = _gemini_chats_cache.get(normalized_cwd)
    if cached is not None:
        if cached.is_dir():
            return cached
        _gemini_chats_cache.pop(normalized_cwd, None)

    tmp_root = Path.home() / ".gemini" / "tmp"
    if not tmp_root.is_dir():
        return None

    best: Path | None = None
    best_mtime = 0.0
    try:
        for project_dir in tmp_root.iterdir():
            if not project_dir.is_dir():
                continue
            root_file = project_dir / ".project_root"
            if not root_file.is_file():
                continue
            try:
                root_content = root_file.read_text(encoding="utf-8").strip()
                normalized_root = _normalize_chat_store_cwd(root_content)
            except (OSError, ValueError):
                continue
            if normalized_root != normalized_cwd:
                continue
            chats_dir = project_dir / "chats"
            if not chats_dir.is_dir():
                continue
            try:
                latest = max(
                    (item.stat().st_mtime for item in chats_dir.glob("session-*.jsonl")),
                    default=0.0,
                )
            except OSError:
                latest = 0.0
            if latest >= best_mtime:
                best = chats_dir
                best_mtime = latest
    except OSError:
        pass

    if best is not None:
        _gemini_chats_cache[normalized_cwd] = best
    return best


def gemini_session_uuid(path: Path) -> str:
    """Read the canonical full conversation id from a Gemini JSONL header."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            rec = json.loads(fh.readline())
    except (OSError, ValueError, TypeError):
        return ""
    return str(rec.get("sessionId", "")).strip() if isinstance(rec, dict) else ""


def resolve_gemini_jsonl_for_cwd(cwd: str, session_uuid: str | None) -> Path | None:
    """Resolve a Gemini conversation inside the chat store owned by *cwd*.

    Filenames contain only the first eight UUID characters, so an exact
    lookup always confirms the full ``sessionId`` from the JSONL header.
    With no session id, return the newest conversation for history discovery.
    """
    base = find_gemini_chats_dir(cwd)
    if base is None:
        return None

    if session_uuid:
        short_uuid = session_uuid[:8]
        try:
            matches = base.glob(f"session-*-{short_uuid}.jsonl")
        except OSError:
            return None
        for match in matches:
            if gemini_session_uuid(match) == session_uuid:
                return match
        return None

    try:
        files = list(base.glob("session-*.jsonl"))
        if not files:
            return None
        return max(files, key=lambda item: item.stat().st_mtime)
    except OSError:
        return None


def _folder_uri_to_path(uri: str) -> str | None:
    """Decode an agy project config `folderUri` into a comparable path.

    Two shapes have been observed on disk under
    `~/.gemini/config/projects/<uuid>.json`:
      - `file:///c%3A/Users/...` — percent-encoded drive letter + colon,
        POSIX-style triple slash (the `gitFolder.folderUri` field).
      - `file://C:/Users/...`   — plain drive letter, double slash (the
        top-level `folderUri` field some entries use instead).
    Both decode to the same comparable path once normalized. macOS/Linux
    URIs (`file:///Users/name/project`) have no drive letter and pass
    through unaffected.
    """
    prefix = "file://"
    if not uri.startswith(prefix):
        return None
    rest = urllib.parse.unquote(uri[len(prefix) :])
    # Windows drive-letter form after stripping "file://": a leading slash
    # followed by "<letter>:" (e.g. "/c:/Users/..."). Strip that leading
    # slash so it lines up with the no-leading-slash "C:/Users/..." shape.
    if len(rest) >= 3 and rest[0] == "/" and rest[2] == ":":
        rest = rest[1:]
    return rest


# ── Antigravity CLI conversation store (agy, 2026-08 layout) ────────────────
#
# agy moved its conversation store. The `~/.gemini/tmp/<name>/chats/session-*.jsonl`
# files the resolver above reads stopped being written (last one on this
# machine: 2026-06-19); a live agy Lead now writes:
#
#   ~/.gemini/antigravity-cli/conversations/<id>.db          — sqlite metadata
#   ~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl
#
# with an entirely different record schema (see `remote/notify.py`'s
# Antigravity branch). Nothing errored when this changed — the old resolver
# simply kept resolving a two-month-old file, so Remote showed an empty chat
# for every gemini Lead. Both layouts are supported: new store first, legacy
# as fallback for machines still on the old agy.
def antigravity_root() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli"


def antigravity_transcript_path(session_id: str) -> Path:
    """Where agy keeps *session_id*'s transcript (may not exist yet)."""
    return (
        antigravity_root()
        / "brain"
        / session_id
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )


def _antigravity_workspace(db_path: Path) -> str | None:
    """The workspace folder recorded in one conversation db, or None.

    The row is a protobuf blob; rather than depend on agy's schema we pull
    the first ``file://`` URI out of it, which is the trajectory's workspace
    (verified against a live session: the blob's leading field pair is the
    workspace URI twice). Read-only and best-effort — agy holds this db open
    while a pane runs, so a lock or a schema change must degrade to "unknown
    workspace", never raise into the caller's scan.
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
    except sqlite3.Error:
        return None
    try:
        row = con.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not row or not isinstance(row[0], (bytes, bytearray)):
        return None
    match = re.search(rb"file://[^\x00-\x1f\"']+", bytes(row[0]))
    if match is None:
        return None
    try:
        uri = match.group(0).decode("utf-8", errors="replace")
    except Exception:
        return None
    return _folder_uri_to_path(uri)


def _antigravity_conversation_dbs() -> list[Path]:
    """Conversation dbs, newest first. `-wal`/`-shm` siblings are ignored."""
    conv = antigravity_root() / "conversations"
    if not conv.is_dir():
        return []
    try:
        files = [p for p in conv.glob("*.db") if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    return files


def find_antigravity_sessions(cwd: str, limit: int = 0) -> list[tuple[str, Path]]:
    """`(session_id, transcript_path)` for agy sessions owned by *cwd*.

    Newest first, and only sessions whose transcript actually exists — a db
    row with no transcript yet is a session agy has not written to, which
    would resolve to a file the tail could never read.
    """
    wanted = _normalize_chat_store_cwd(cwd) if cwd else ""
    if not wanted:
        return []
    out: list[tuple[str, Path]] = []
    for db_path in _antigravity_conversation_dbs():
        workspace = _antigravity_workspace(db_path)
        if not workspace or _normalize_chat_store_cwd(workspace) != wanted:
            continue
        session_id = db_path.stem
        transcript = antigravity_transcript_path(session_id)
        if not transcript.is_file():
            continue
        out.append((session_id, transcript))
        if limit and len(out) >= limit:
            break
    return out


def resolve_antigravity_transcript(cwd: str, session_uuid: str | None) -> Path | None:
    """Newest agy transcript for *cwd*, or the one matching *session_uuid*."""
    if session_uuid:
        transcript = antigravity_transcript_path(session_uuid)
        if not transcript.is_file():
            return None
        db_path = antigravity_root() / "conversations" / f"{session_uuid}.db"
        workspace = _antigravity_workspace(db_path) if db_path.is_file() else None
        if workspace and cwd:
            if _normalize_chat_store_cwd(workspace) != _normalize_chat_store_cwd(cwd):
                return None  # never mirror another project's conversation
        return transcript
    found = find_antigravity_sessions(cwd, limit=1)
    return found[0][1] if found else None


# ── concurrent-mint guard (#132 followup) ────────────────────────────────────
# agy has no server-side dedup: mint a project via `--new-project` twice for
# the same folder (two panes spawning against the same not-yet-registered cwd
# at once — e.g. a Multi-mode fan-out or `--shards` sharing one cwd) and agy
# creates two separate project ids for it, forking that project's
# conversation history in two. The cockpit can't create the id itself either
# — only `agy` mints one, and only after it finishes booting, well after our
# `spawn()` call has already returned. So the guard lives entirely in-process
# (single Qt event loop → these calls are never truly concurrent, only
# closely spaced in wall-clock time): the first caller for a given cwd claims
# it as "minting" and moves on; anyone else asking about that same cwd while
# the claim is fresh polls the on-disk registry briefly instead of racing its
# own `--new-project`, and gives up (accepting a possible duplicate over a
# stuck spawn) once its poll budget runs out.
_MINT_INFLIGHT: dict[str, float] = {}
_MINT_INFLIGHT_TTL_SEC = 60.0  # abandon a claim this old (crashed/never-finished mint)
_MINT_POLL_TIMEOUT_SEC = 4.0  # total bounded wait for a fresh mint to land
_MINT_POLL_INTERVAL_SEC = 0.25


def _project_registry_files() -> list[Path]:
    config_dir = Path.home() / ".gemini" / "config" / "projects"
    if not config_dir.is_dir():
        return []
    return sorted(config_dir.glob("*.json"))


def _find_project_id_for(target: str) -> str | None:
    """Scan `~/.gemini/config/projects/*.json` for a project whose folder
    matches the already-normalized `target` path. Best-effort: a corrupt
    project file is skipped rather than raised (must not block spawning the
    whole gemini role over one bad file)."""
    for config_file in _project_registry_files():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        project_id = data.get("id")
        if not project_id:
            continue
        resources = data.get("projectResources", {}).get("resources", [])
        for resource in resources:
            for uri in (
                resource.get("gitFolder", {}).get("folderUri"),
                resource.get("folderUri"),
            ):
                if not uri:
                    continue
                decoded = _folder_uri_to_path(uri)
                if decoded and _normalize_path_for_compare(decoded) == target:
                    return project_id
    return None


def resolve_agy_project_id(cwd: str) -> str | None:
    """Match `cwd` against agy's own project registry
    (`~/.gemini/config/projects/*.json`) and return the matching project
    id, or None when no existing agy project covers this folder (the caller
    then falls back to `--new-project`).

    Why this exists (#132): agy has no cwd-derived project scoping of its
    own — every spawn without an explicit `--project <id>` lands
    conversations in the shared pseudo-project `default-cli-project`,
    mixing every takkub project's Lead/teammate conversation history into
    one bucket. Antigravity IDE sessions DO get a real per-project uuid
    recorded here (`projectResources.resources[].gitFolder.folderUri`, or
    the older plain `.folderUri` sibling some entries use instead) —
    reusing that id when the folder matches keeps agy's existing
    conversation history for the project intact instead of the caller
    minting a duplicate id via `--new-project`.

    Concurrent-mint guard (#132 followup): a `cwd` with no existing match
    could mean "no agy project yet" OR "another pane just asked the same
    question a moment ago and is already getting one minted". Only the
    first caller for a given `cwd` is told "go mint one" (returns None with
    no in-flight claim in the way); anyone else asking again while that
    claim is still fresh (`_MINT_INFLIGHT_TTL_SEC`) polls the registry for
    up to `_MINT_POLL_TIMEOUT_SEC` instead of immediately requesting its own
    `--new-project` — so two panes spawning into the same brand-new cwd at
    nearly the same time converge on one project id instead of forking it in
    two. A poll that times out still returns None (accepting a possible
    duplicate beats blocking the spawn indefinitely); a stale claim past its
    TTL (the original minter crashed or never finished) is abandoned so the
    next caller becomes the new minter instead of polling forever.
    """
    target = _normalize_path_for_compare(cwd)
    found = _find_project_id_for(target)
    if found:
        _MINT_INFLIGHT.pop(target, None)
        return found

    now = time.monotonic()
    inflight_since = _MINT_INFLIGHT.get(target)
    if inflight_since is not None and (now - inflight_since) < _MINT_INFLIGHT_TTL_SEC:
        deadline = now + _MINT_POLL_TIMEOUT_SEC
        while time.monotonic() < deadline:
            time.sleep(_MINT_POLL_INTERVAL_SEC)
            found = _find_project_id_for(target)
            if found:
                _MINT_INFLIGHT.pop(target, None)
                return found
        return None

    _MINT_INFLIGHT[target] = now
    return None


def gemini_exec(
    prompt: str,
    *,
    cwd: str | None = None,
    timeout: float = 120.0,
    model: str | None = None,
) -> tuple[bool, str]:
    """Run `agy -p "<prompt>"` and return `(ok, output)`.

    Antigravity's non-interactive entry point is the `-p`/`--print`
    flag (same shape the old Gemini CLI used), NOT a subcommand like
    codex's `exec`. Do not reuse codex's argv shape - `agy exec "..."`
    would fail with "unknown command".

    `cwd` lets the caller scope `agy` to a specific project. Defaults
    to the process cwd so `takkub gemini` from inside any pane targets
    that pane's project naturally.

    `model` is optional and gets forwarded as `-m <name>` (e.g.
    `gemini-3.1-pro`); when None, `agy` uses whatever its config
    defaults to.

    `timeout` defaults to 120 s. Timeout returns
    (False, "agy exec timed out").

    The public name stays `gemini_exec` because it backs the cockpit's
    `gemini` role + `takkub gemini` subcommand; only the engine behind
    it changed (Gemini CLI → Antigravity `agy`).
    """
    binary = find_agy_executable()
    if binary is None:
        return False, _INSTALL_HINT
    if not (prompt or "").strip():
        return False, "empty prompt"
    # Bound agy's own print-mode wait (default 5m) to just under our subprocess
    # timeout so agy gives up first and we never sit on a dead call. agy's
    # `--print` mode is unreliable without a real TTY (returns empty / blocks),
    # so this is belt-and-suspenders against a hang.
    agy_print_timeout = f"{max(10, int(timeout) - 5)}s"
    argv: list[str] = [binary, "-p", prompt, "--print-timeout", agy_print_timeout]
    if model:
        argv = [binary, "-m", model, "-p", prompt, "--print-timeout", agy_print_timeout]
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
        return False, "agy exec timed out"
    except FileNotFoundError:
        return False, "agy binary disappeared from PATH"
    except Exception as e:
        return False, f"agy exec failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "agy exec failed").strip()
        return False, tail
    out = (proc.stdout or "").strip()
    if not out:
        # agy exited 0 but emitted nothing — its `--print` mode needs a real
        # terminal and silently no-ops when captured non-interactively. Turn the
        # misleading "empty success" into actionable guidance instead of handing
        # the caller a blank answer.
        return False, (
            "agy print mode returned no output. Antigravity's `agy -p` needs a "
            "real terminal (TTY) and produces nothing when run non-interactively "
            "(the cockpit captures output via a pipe). Use the interactive gemini "
            'pane instead: `takkub assign --role gemini "<task>"`.'
        )
    return True, out
