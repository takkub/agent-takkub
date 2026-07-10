"""Read Claude Code session JSONL files to surface live token usage per pane.

Each `claude` process writes its conversation to
`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`, one JSON line per
turn. Every assistant turn carries a `message.usage` block with the prompt
size (`input_tokens + cache_creation_input_tokens + cache_read_input_tokens`)
and the response size (`output_tokens`).

The prompt size of the *most recent* assistant turn equals the current
context-window occupancy — that's what we show on each pane header and sum
into the status-bar total.

Reading strategy: open the JSONL, stream lines forwards, keep replacing
`last_usage`. Even chatty sessions are usually < 5 MB; doing this every few
seconds in the GUI thread is cheap. If a session grows huge, the user can
just /clear inside claude.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# Default context window for Claude 4 family. Override per pane with the
# TAKKUB_CONTEXT_LIMIT env var (e.g. set to 1000000 when using the [1m] flag).
_DEFAULT_LIMIT = 200_000

# Per-model overrides if Claude Code ever stamps a different family into the
# `model` field of the assistant message. Best-effort: anything not listed
# falls back to _DEFAULT_LIMIT.
_MODEL_LIMITS: dict[str, int] = {
    # 1M variants — stamped with bracket suffix in some Code clients
    "claude-opus-4-8[1m]": 1_000_000,
    "claude-sonnet-5[1m]": 1_000_000,
}


def context_limit_for_model(model: str | None) -> int:
    """Return the context-window cap for `model`, honouring env override."""
    env = os.environ.get("TAKKUB_CONTEXT_LIMIT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    if model and model in _MODEL_LIMITS:
        return _MODEL_LIMITS[model]
    return _DEFAULT_LIMIT


def effective_context_limit(model: str | None, prompt: int, base: int | None = None) -> int:
    """Context-window cap to display for a turn of `prompt` tokens.

    `base` is a per-pane known cap (e.g. a Max Lead pinned to 1M); when None it
    falls back to the per-model/env limit. Either way, if the observed prompt
    already exceeds the cap we bump to 1M — a turn that sent >200k tokens can
    only be a 1M-context session, and the bare model name stamped in the JSONL
    (`claude-opus-4-8`, no `[1m]`) doesn't encode the 1M runtime flag. This
    keeps the badge from showing an impossible ">100%" when the per-pane tier
    guess was wrong or absent.
    """
    cap = base if base is not None else context_limit_for_model(model)
    return 1_000_000 if prompt > cap else cap


# Claude Code replaces every char that is not [A-Za-z0-9] with '-' when it
# builds the project dir name under ~/.claude/projects/ — separators AND '_'
# and '.'. Matching that exactly is what lets the token meter find the session
# JSONL.
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


def encode_path_for_claude(cwd: str | Path) -> str:
    """Map a filesystem path to the directory name Claude Code uses under
    `~/.claude/projects/`.

    Claude replaces every non-alphanumeric character with '-' (drive ':',
    separators '\\' '/', and crucially '_' and '.'), keeping alphanumerics and
    the original drive-letter case that `Path.resolve()` produces:

        C:\\Users\\alice\\WebstormProjects\\my_app_web\\client
        → C--Users-alice-WebstormProjects-my-app-web-client

    The earlier version only rewrote '\\', '/' and ':', so any project whose
    path contained '_' or '.' (e.g. my_app_web) resolved to a directory
    that doesn't exist — its token badge silently never appeared.
    """
    return _NON_ALNUM_RE.sub("-", str(Path(cwd).resolve()))


def session_project_dir_for_cwd(config_dir: str | Path | None, cwd: str | Path) -> Path:
    """Return the exact ``<config_dir>/projects/<encoded-cwd>`` directory
    Claude Code writes `cwd`'s session JSONLs into.

    Callers that need "sessions belonging to this cwd" (resume pickers,
    resume-uuid validation) should list/glob straight from this directory
    instead of scanning every project dir and reverse-decoding names for an
    equality check. `chatlog_scanner.decode_project_dir()` is lossy — Claude
    maps *every* non-alphanumeric char (not just separators) to '-', so
    decoding can't tell a literal '-' in the original path apart from an
    encoded separator. Encoding forward (this function) and comparing
    directory names/globbing directly sidesteps that ambiguity entirely.
    """
    return _claude_projects_dir(config_dir) / encode_path_for_claude(cwd)


def _claude_projects_dir(config_dir: str | Path | None = None) -> Path:
    """Return the `projects/` dir holding Claude Code session JSONLs.

    `config_dir` is the pane's CLAUDE_CONFIG_DIR. When None (the default
    profile, which never sets that env var) it falls back to `~/.claude`.
    A pane running under a non-default user profile writes its sessions to
    `<config_dir>/projects/`, NOT `~/.claude/projects/` — so the meter must
    honour it or the badge silently never appears (the per-profile
    context-% regression).
    """
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    return base / "projects"


def find_latest_session(
    cwd: str | Path, since_ts: float = 0.0, config_dir: str | Path | None = None
) -> Path | None:
    """Return the most-recently-modified JSONL file matching `cwd`'s encoded
    project dir, optionally requiring mtime >= since_ts.

    `config_dir` scopes the lookup to a specific Claude config home (the
    pane's CLAUDE_CONFIG_DIR); None means the default `~/.claude`.

    Returns None if no file qualifies. Cockpit callers re-poll on every
    refresh rather than caching the first hit — `/clear` inside claude
    rolls the conversation over to a new session file, and a sticky lock
    on the pre-clear file would pin the token meter forever. Cockpit's
    one-pane-per-cwd discipline (Lead at project root, teammates at
    distinct sub-paths, no two Leads share a project) makes peer-pane
    contamination effectively impossible.
    """
    enc = encode_path_for_claude(cwd)
    proj_dir = _claude_projects_dir(config_dir) / enc
    if not proj_dir.is_dir():
        return None
    best: tuple[float, Path] | None = None
    try:
        for f in proj_dir.iterdir():
            if f.suffix != ".jsonl" or not f.is_file():
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime < since_ts:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, f)
    except OSError:
        return None
    return best[1] if best else None


# The token badge refreshes every 5 s per pane and only needs the *last*
# assistant turn, which sits at the end of the file. Scanning the whole JSONL
# (claude sessions reach tens of MB) on the Qt main thread caused periodic UI
# hitches — same failure mode as the events.log full-read. So we scan only the
# tail and fall back to a full scan only if the tail held no assistant turn.
_TAIL_SCAN_BYTES = 512 * 1024


def _scan_lines_for_usage(lines) -> tuple[dict | None, str | None]:
    """Return (last_usage_block, last_model) from an iterable of JSONL lines."""
    last_usage: dict | None = None
    last_model: str | None = None
    for line in lines:
        if not line.strip():
            continue
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue
        if j.get("type") != "assistant":
            continue
        msg = j.get("message")
        if not isinstance(msg, dict):
            continue
        u = msg.get("usage")
        if not isinstance(u, dict):
            continue
        last_usage = u
        last_model = msg.get("model") or last_model
    return last_usage, last_model


def read_last_usage(jsonl: Path) -> dict | None:
    """Return the last assistant turn's usage block.

    Returns a dict with the keys:
        input, cache_creation, cache_read, output, prompt, total, model
    where `prompt = input + cache_creation + cache_read` (i.e. tokens sent
    to the model this turn — the context occupancy) and `total = prompt +
    output`.
    """
    try:
        size = jsonl.stat().st_size
    except OSError:
        return None

    last_usage: dict | None = None
    last_model: str | None = None

    # Fast path: scan only the tail. The newest assistant turn is near EOF.
    if size > _TAIL_SCAN_BYTES:
        try:
            with open(jsonl, "rb") as f:
                f.seek(size - _TAIL_SCAN_BYTES)
                raw = f.read()
            nl = raw.find(b"\n")  # drop the partial leading line
            if nl != -1:
                raw = raw[nl + 1 :]
            last_usage, last_model = _scan_lines_for_usage(
                raw.decode("utf-8", "replace").splitlines()
            )
        except OSError:
            last_usage = None

    # Full scan when the file is small, or the tail held no assistant turn
    # (e.g. a very large final turn pushed it past the window).
    if last_usage is None:
        try:
            with jsonl.open("r", encoding="utf-8", errors="replace") as f:
                last_usage, last_model = _scan_lines_for_usage(f)
        except OSError:
            return None

    if not last_usage:
        return None
    inp = int(last_usage.get("input_tokens") or 0)
    cc = int(last_usage.get("cache_creation_input_tokens") or 0)
    cr = int(last_usage.get("cache_read_input_tokens") or 0)
    out = int(last_usage.get("output_tokens") or 0)
    prompt = inp + cc + cr
    return {
        "input": inp,
        "cache_creation": cc,
        "cache_read": cr,
        "output": out,
        "prompt": prompt,
        "total": prompt + out,
        "model": last_model or "unknown",
    }


def format_tokens(n: int) -> str:
    """Human-friendly token count: 1234 → '1.2k', 147500 → '147k'."""
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k"
    if n < 1_000_000:
        return f"{n // 1000}k"
    return f"{n / 1_000_000:.1f}M"


def usage_color(pct: float) -> str:
    """Map a 0..1 context-fill ratio to a status colour (hex), matching the
    palette used elsewhere in the cockpit.
    """
    if pct < 0.5:
        return "#9ca3af"  # neutral grey
    if pct < 0.8:
        return "#facc15"  # yellow
    if pct < 0.95:
        return "#f97316"  # orange
    return "#ef4444"  # red
