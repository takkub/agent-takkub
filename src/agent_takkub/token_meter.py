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
from pathlib import Path

# Default context window for Claude 4 family. Override per pane with the
# TAKKUB_CONTEXT_LIMIT env var (e.g. set to 1000000 when using the [1m] flag).
_DEFAULT_LIMIT = 200_000

# Per-model overrides if Claude Code ever stamps a different family into the
# `model` field of the assistant message. Best-effort: anything not listed
# falls back to _DEFAULT_LIMIT.
_MODEL_LIMITS: dict[str, int] = {
    # 1M variants — stamped with bracket suffix in some Code clients
    "claude-opus-4-7[1m]": 1_000_000,
    "claude-sonnet-4-6[1m]": 1_000_000,
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


def encode_path_for_claude(cwd: str | Path) -> str:
    """Map a filesystem path to the directory name Claude Code uses under
    `~/.claude/projects/`.

    Observed encoding (Windows):
        C:\\Users\\monch\\WebstormProjects\\agent-takkub
        → C--Users-monch-WebstormProjects-agent-takkub

    Drive letter uppercased, `:\\` becomes `--`, all path separators become
    `-`. POSIX paths (no drive) get every `/` replaced with `-`.
    """
    p = str(Path(cwd).resolve()).replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        drive = p[0].upper()
        rest = p[2:]  # leading "/" included
        return drive + "-" + rest.replace("/", "-")
    return p.replace("/", "-")


def _claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def find_latest_session(cwd: str | Path, since_ts: float = 0.0) -> Path | None:
    """Return the most-recently-modified JSONL file matching `cwd`'s encoded
    project dir, optionally requiring mtime >= since_ts.

    Returns None if no file qualifies. Callers should cache the returned path
    for the lifetime of a pane spawn so a peer pane sharing the same cwd
    doesn't steal the meter.
    """
    enc = encode_path_for_claude(cwd)
    proj_dir = _claude_projects_dir() / enc
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


def read_last_usage(jsonl: Path) -> dict | None:
    """Stream the JSONL and return the last assistant turn's usage block.

    Returns a dict with the keys:
        input, cache_creation, cache_read, output, prompt, total, model
    where `prompt = input + cache_creation + cache_read` (i.e. tokens sent
    to the model this turn — the context occupancy) and `total = prompt +
    output`.
    """
    last_usage: dict | None = None
    last_model: str | None = None
    try:
        with jsonl.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
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
