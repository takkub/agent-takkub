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

# Default context window for Claude 4 family. TAKKUB_CONTEXT_LIMIT is a
# process-wide fallback; callers with pane-specific metadata pass ``base`` to
# ``effective_context_limit`` instead.
_DEFAULT_LIMIT = 200_000

# Per-model context-window sizes, keyed by the *bare* model id — this is the
# only place in the cockpit that should know each model's context size.
# (`settings_window._MODELS_BY_PROVIDER` is just the Settings dropdown's
# preset list of model-id strings; it is not a source of truth for context
# size and must not duplicate this table.) Anything not listed falls back to
# _DEFAULT_LIMIT. Keep this in sync with shared/models.md in the claude-api
# skill when Anthropic ships a new model.
_MODEL_LIMITS: dict[str, int] = {
    "claude-opus-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-fable-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-4-5": 200_000,
}

# Some Code clients stamp a "[1m]" suffix onto the model id to flag the 1M
# runtime variant regardless of the base model's own default (e.g. a Max Lead
# opting into the 1M context flag). Match it generically instead of listing
# "<id>[1m]" pairs per model in _MODEL_LIMITS above — that generalizes to any
# base model without needing an extra table entry per model.
_SUFFIX_1M_RE = re.compile(r"\[1m\]$", re.IGNORECASE)


def context_limit_for_model(model: str | None) -> int:
    """Return the context-window cap for `model`, honouring process env."""
    env = os.environ.get("TAKKUB_CONTEXT_LIMIT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    if model:
        if _SUFFIX_1M_RE.search(model):
            return 1_000_000
        if model in _MODEL_LIMITS:
            return _MODEL_LIMITS[model]
    return _DEFAULT_LIMIT


def effective_context_limit(model: str | None, prompt: int, base: int | None = None) -> int:
    """Context-window cap to display for a turn of `prompt` tokens.

    `base` is a per-pane known cap (e.g. a Max Lead pinned to 1M); when None it
    falls back to the per-model/process-env limit. Either way, if the observed
    prompt already exceeds the cap we bump to 1M — a turn that sent more tokens
    than the resolved cap can only be a 1M-context session, which covers a
    model missing from `_MODEL_LIMITS` (new/unreleased) or a `base` guess that
    was wrong. This keeps the badge from showing an impossible ">100%" when
    the per-pane tier guess was wrong or absent.
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


def find_session_by_uuid(
    cwd: str | Path, session_uuid: str, config_dir: str | Path | None = None
) -> Path | None:
    """Return this pane's *exact* session JSONL —
    ``<cwd's encoded project dir>/<session_uuid>.jsonl`` — never a guess.

    `session_uuid` is the pane's own `PaneState.session_uuid` (stamped at
    spawn via `--session-id`/`--resume`, kept current across manual
    `/resume`/`/clear` by the `SessionStart` hook,
    `Orchestrator.consume_session_report`). Resolving the exact filename by
    uuid is what makes this safe when several panes share one cwd (issue
    #129: a single-repo project with every role pointed at the same project
    root) — `find_latest_session`'s newest-mtime heuristic picked up
    whichever *sibling* pane wrote most recently, showing that pane's
    context %/session-cap numbers on this one's badge instead of its own.

    Returns None if `session_uuid` is falsy or the file doesn't exist yet
    (fresh pane, not flushed). Callers must treat None as "unknown — hide
    the badge", never fall back to scanning for *some* file in the cwd's
    project dir.
    """
    if not session_uuid:
        return None
    enc = encode_path_for_claude(cwd)
    candidate = _claude_projects_dir(config_dir) / enc / f"{session_uuid}.jsonl"
    return candidate if candidate.is_file() else None


def find_latest_session(
    cwd: str | Path, since_ts: float = 0.0, config_dir: str | Path | None = None
) -> Path | None:
    """Return the most-recently-modified JSONL file matching `cwd`'s encoded
    project dir, optionally requiring mtime >= since_ts.

    `config_dir` scopes the lookup to a specific Claude config home (the
    pane's CLAUDE_CONFIG_DIR); None means the default `~/.claude`.

    Returns None if no file qualifies. This is a newest-mtime GUESS, not an
    identity lookup — it cannot tell two panes sharing the same cwd apart
    (issue #129 disproved the old claim that cockpit's pane layout makes
    that "effectively impossible": a single-repo project routinely has
    Lead and every teammate pointed at the same project root, and one
    pane's badge/session-cap watchdog picked up a sibling pane's transcript
    whenever it happened to write more recently). Callers that know a
    specific pane's `session_uuid` — which is every cockpit caller — must
    use `find_session_by_uuid` instead; this function is only safe when no
    uuid is available and "some session in this cwd, not necessarily this
    pane's" is an acceptable answer.
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
