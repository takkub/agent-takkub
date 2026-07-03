"""Pure module-level helpers extracted from orchestrator.py (refactor round 2, step A).

All functions here are stateless (no ``self``, no Qt, no AgentPane refs) — they
depend only on stdlib, config leaf modules, and each other.  The original
``orchestrator.py`` re-exports everything via a ``from .orchestrator_text import
*``-style block so existing callers (tests, main_window, app) see no change.

**Import constraint:** this module MUST NOT import ``orchestrator``,
``main_window``, ``app``, or ``cli`` — it is a pure engine-layer leaf.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys as _sys
import time
from datetime import datetime

from .config import EVENTS_LOG, RUNTIME_DIR, ensure_runtime
from .lead_context import _allowed_project_roots
from .roles import LEAD


def _orch_attr(name: str, default):
    """Read a module-level attribute through the orchestrator façade at call time.

    Tests that patch ``agent_takkub.orchestrator.<name>`` (the re-export façade)
    would otherwise miss functions defined here that read from their own module
    namespace.  Delegating through ``sys.modules`` lets the patch propagate
    without creating a circular import.  Falls back to *default* when the
    orchestrator module is not yet loaded (e.g. in standalone unit tests that
    import orchestrator_text directly).
    """
    m = _sys.modules.get("agent_takkub.orchestrator")
    return getattr(m, name, default) if m is not None else default


# ── log rotation cap ──────────────────────────────────────────────────────────
# Cap events.log so it can never grow unbounded. The LogsPanel dock and any
# tail reader pay per-byte; a multi-MB log on the Qt main thread wedged the
# cockpit (see logs_panel._TAIL_BYTES). When the file crosses the cap we
# rotate it to events.log.old (single generation) and start fresh.
_EVENTS_LOG_MAX_BYTES = 2 * 1024 * 1024

# ── transcript retention ──────────────────────────────────────────────────────
_TRANSCRIPT_RETENTION_DAYS = 7

# ── artifact scan exclusions ──────────────────────────────────────────────────
# Artifact dirs excluded from harvest scans.
_HARVEST_EXCLUDE_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        "node_modules",
        ".venv",
        ".next",
        "dist",
        "build",
    }
)

# ── per-role model tier table ─────────────────────────────────────────────────
# Per-role model tier: (model, effort, fallback-model). Picked per role
# rather than one flat tier for all teammates because the cockpit owner runs
# on Claude Max (per-token cost irrelevant), so the only real tradeoff is
# latency. That lets us spend quality where a miss is expensive and stay snappy
# where it isn't:
#
#   • Gate roles (reviewer, critic) — the last line before something ships.
#     A missed bug / UX flaw leaks to production, and these run infrequently
#     at verify/pre-ship hops where the user is already waiting, so latency
#     barely matters. → Opus, high effort. Fallback Sonnet (not Haiku) so a
#     degraded gate is still strong.
#   • Correctness-sensitive impl (backend, devops) — API contracts, schema,
#     migrations, and irreversible deploy/infra. High frequency, so stay on
#     Sonnet for turn speed but raise effort to cut subtle-bug rework cycles.
#   • Everything else (frontend, mobile, qa, designer) — execution-heavy,
#     high frequency, low blast radius → Sonnet medium (the default tier) for
#     snappy turns.
#
# The global TAKKUB_TEAMMATE_MODEL / _EFFORT / _FALLBACK env vars still win
# when explicitly set — they override every role's per-role default at once.
_DEFAULT_TEAMMATE_TIER: tuple[str, str, str] = (
    "claude-sonnet-5",
    "medium",
    "claude-haiku-4-5",
)
_ROLE_MODEL_TIERS: dict[str, tuple[str, str, str]] = {
    "reviewer": ("claude-opus-4-8", "high", "claude-sonnet-5"),
    "critic": ("claude-opus-4-8", "high", "claude-sonnet-5"),
    "backend": ("claude-sonnet-5", "high", "claude-haiku-4-5"),
    "devops": ("claude-sonnet-5", "high", "claude-haiku-4-5"),
    # codex/gemini substitutes: when the real binary is unavailable, Claude
    # backs the role — use Opus/high so the cross-check has the same quality
    # as reviewer/critic rather than falling to the default Sonnet tier.
    "codex": ("claude-opus-4-8", "high", "claude-sonnet-5"),
    "gemini": ("claude-opus-4-8", "high", "claude-sonnet-5"),
}

# ── bracketed-paste framing ───────────────────────────────────────────────────
# Bracketed-paste threshold for messages injected into a pane via the
# orchestrator (assign / send / slash-command). Below this length we
# write raw text — claude code's interactive input handles short typing
# fine. At or above, we wrap with `ESC [200~ ... ESC [201~` so claude
# treats the whole block as a single atomic paste instead of typing
# char-by-char. Without this, long task specs occasionally lose the
# head of the message when the pane is mid-render at write time (the
# bug behind teammates complaining about "ข้อความถูกตัดส่วนต้น").
BRACKETED_PASTE_THRESHOLD = 200
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"

# C0 controls (incl. bare ESC 0x1b and CR 0x0d) plus DEL (0x7f) and the 8-bit
# C1 range (0x80-0x9f). C1 codepoints like U+009B (CSI), U+009D (OSC) and
# U+0090 (DCS) are single-byte escape introducers that some terminals honour,
# so a pane message containing them could start an escape sequence even after
# ESC is stripped. TAB (0x09) and LF (0x0a) are deliberately excluded — both
# are legitimate in multi-line task bodies.
_CONTROL_STRIP = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# ── codex task preamble ───────────────────────────────────────────────────────
_CODEX_TASK_NOTICE = (
    "[orchestrator note] อ่านก่อนเริ่มงาน:\n"
    "- `ห้าม spawn subagent` ใน ROLE prefix หมายถึง AI subagent\n"
    "  เท่านั้น (Task tool / codex delegation flags) — ไม่รวม shell\n"
    "  command ที่คุณรันเองในเทอร์มินัลนี้\n"
    "- เมื่อเสร็จงาน ต้อง **รัน shell command** ผ่าน Bash tool:\n"
    '      takkub done "<one-line summary>"\n'
    '  ห้ามพิมพ์ "takkub done" เป็นข้อความตอบในแชท (orchestrator\n'
    "  มองไม่เห็น → Lead ไม่ทราบว่างานเสร็จ → pane idle ตลอด)\n"
    "- review / analysis tasks: save findings ลงไฟล์ docs/ ก่อน\n"
    "  แล้วค่อย `takkub done` (pane auto-close ~2.5s หลัง done)\n"
    "\n"
    "------ task ------\n"
)

# ── paste enter-delay constants ───────────────────────────────────────────────
# Delay between writing the payload and writing the submitting `\r`.
# Claude Code v2.1.x collapses a bracketed-paste block into a
# `[Pasted text #N +M lines]` placeholder before it accepts Enter as a
# submit. Rendering that placeholder takes noticeably longer than the
# 200 ms used for short typing-style writes; an Enter that lands
# mid-render is consumed as a soft newline inside the paste and the
# task never actually submits (the bug surfaced when a teammate pane
# sat at `[Pasted text #1 +15 lines]` forever instead of running the
# spec). Pick the longer delay only when the payload actually came
# back from `_paste_payload` wrapped, so slash-command and short
# message latency stay snappy.
_PASTE_ENTER_DELAY_MS = 800
_TYPING_ENTER_DELAY_MS = 200
# Extra delay per KB of bracketed-paste payload. A very large paste renders
# its `[Pasted text]` placeholder slower than the fixed 800 ms window, so the
# submit \r can land mid-render and be swallowed as a soft newline (issue #22).
# Scale the wait with payload size, capped so a huge spec can't stall input.
_PASTE_PER_KB_DELAY_MS = 150
_PASTE_MAX_ENTER_DELAY_MS = 3000

# ── hot.md cadence ────────────────────────────────────────────────────────────
# How often the orchestrator rewrites `<vault>/hot.md`. The hot file is
# a low-stakes status snapshot — open Obsidian, see what cockpit is
# doing right now — so the cadence trades freshness for write churn.
# A minute is plenty: the panes themselves render to xterm in real time.
_HOT_MD_INTERVAL_MS = 60_000


# ── functions ─────────────────────────────────────────────────────────────────


def _read_tail_bytes(path: pathlib.Path, max_bytes: int) -> bytes:
    """Return at most the last ``max_bytes`` bytes of ``path`` without reading
    the whole file into memory. Pure (no Qt) so it can be unit-tested. Raises
    OSError on read failure (caller handles)."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        return fh.read()


def _log_event(event: str, **details) -> None:
    """Append a JSONL event line to runtime/events.log. Best-effort; never
    raises so an audit-log failure can't take down the orchestrator."""
    try:
        # Read via proxy so tests that patch orchestrator.EVENTS_LOG /
        # orchestrator._EVENTS_LOG_MAX_BYTES see their patches here.
        events_log = _orch_attr("EVENTS_LOG", EVENTS_LOG)
        max_bytes = _orch_attr("_EVENTS_LOG_MAX_BYTES", _EVENTS_LOG_MAX_BYTES)
        ensure_runtime()
        try:
            if events_log.exists() and events_log.stat().st_size > max_bytes:
                os.replace(events_log, events_log.parent / (events_log.name + ".old"))
        except OSError:
            pass
        line = json.dumps(
            {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **details},
            ensure_ascii=False,
        )
        with open(events_log, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def prune_old_transcripts(max_age_days: int = _TRANSCRIPT_RETENTION_DAYS) -> int:
    """Delete `*.transcript.log` files under runtime/sessions older than
    *max_age_days* (by mtime). Keeps `.md` session notes. Best-effort: never
    raises, returns the number of files removed."""
    import time as _time

    sessions = RUNTIME_DIR / "sessions"
    if not sessions.is_dir():
        return 0
    cutoff = _time.time() - max_age_days * 86_400
    removed = 0
    bytes_freed = 0
    try:
        for p in sessions.rglob("*.transcript.log"):
            try:
                st = p.stat()
                if st.st_mtime < cutoff:
                    bytes_freed += st.st_size
                    p.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    if removed:
        _log_event(
            "transcript_prune",
            removed=removed,
            mb_freed=round(bytes_freed / 1_048_576, 1),
            max_age_days=max_age_days,
        )
    return removed


def scan_artifacts(
    project_paths: list[pathlib.Path],
    since_ts: float,
    *,
    limit: int = 100,
) -> list[dict]:
    """Scan project paths for files modified at or after `since_ts`.

    Returns list[{path, mtime_ts, mtime_rel}] sorted by mtime descending,
    capped at `limit`. Skips symlinks, directories, and any path whose parts
    contain a name from _HARVEST_EXCLUDE_DIRS. Non-existent or unreadable
    paths are silently skipped.
    """
    found: list[tuple[float, pathlib.Path]] = []
    seen: set[pathlib.Path] = set()
    now = time.time()

    for base in project_paths:
        if not base.exists():
            continue
        try:
            for root, dirnames, filenames in os.walk(base, followlinks=False):
                dirnames[:] = [d for d in dirnames if d not in _HARVEST_EXCLUDE_DIRS]
                for fname in filenames:
                    p = pathlib.Path(root) / fname
                    if p.is_symlink():
                        continue
                    if p in seen:
                        continue
                    seen.add(p)
                    if any(part in _HARVEST_EXCLUDE_DIRS for part in p.parts):
                        continue
                    try:
                        mtime = p.stat().st_mtime
                    except OSError:
                        continue
                    if mtime >= since_ts:
                        found.append((mtime, p))
        except OSError:
            continue

    found.sort(key=lambda t: t[0], reverse=True)
    del found[limit:]

    result: list[dict] = []
    for mtime, p in found:
        age = now - mtime
        if age < 60:
            rel = f"{int(age)}s ago"
        elif age < 3600:
            rel = f"{int(age // 60)}m ago"
        else:
            rel = f"{int(age // 3600)}h ago"
        result.append({"path": str(p), "mtime_ts": mtime, "mtime_rel": rel})
    return result


def _teammate_tier(role_name: str) -> tuple[str, str, str]:
    """(model, effort, fallback) for a claude teammate role.

    Non-claude panes (codex/gemini/shell) never reach this — they spawn via a
    separate path that skips claude model flags entirely.
    """
    return _ROLE_MODEL_TIERS.get(role_name, _DEFAULT_TEAMMATE_TIER)


def _lead_model_override() -> str | None:
    """Explicit `--model` for the Lead pane, or None to inherit the user default.

    The Lead normally spawns with no `--model` flag and rides the owner's
    default model. On a Max account that default is often the `[1m]`
    1M-context variant — fine on Max, but a hard error on Pro ("Usage credits
    required for 1M context"). When the owner has marked the install as Pro
    (see plan_tier), pin the Lead to a standard-context model so it doesn't
    inherit a 1M default and fail. Max → None (unchanged: inherit user default).

    Env override TAKKUB_PRO_LEAD_MODEL swaps the pinned model per-install; set
    it to empty to disable the pin even under Pro (inherit user default again).
    """
    from . import plan_tier

    if not plan_tier.is_pro():
        return None
    return os.environ.get("TAKKUB_PRO_LEAD_MODEL", plan_tier.PRO_LEAD_MODEL).strip() or None


def _sanitize_pane_text(text: str) -> str:
    """Strip control sequences that could break out of bracketed-paste mode.

    A message body containing ``\\x1b[201~`` closes the bracketed-paste bracket
    early, letting the rest of the content execute as raw terminal input in any
    pane running with ``--dangerously-skip-permissions``. Strip both the opening
    and closing bracket sequences plus every C0/C1 control byte (incl. bare ESC,
    CR, and 8-bit CSI/OSC/DCS introducers) so every write path (send,
    _notify_lead, task inject) is safe regardless of input length.

    TAB and LF are preserved — both are intentional in multi-line task bodies and
    neither submits the input in bracketed-paste mode.
    """
    # Remove bracketed-paste control sequences first so their printable tail
    # ("[200~") goes together with its ESC before the blanket control strip.
    text = text.replace(_PASTE_END, "").replace(_PASTE_START, "")
    # Strip C0 control bytes (incl. ESC + CR), DEL, and 8-bit C1 controls.
    text = _CONTROL_STRIP.sub("", text)
    return text


def _paste_payload(text: str) -> str:
    """Return `text` wrapped in bracketed-paste escapes when long enough.

    Used by every cockpit-driven write into a pane's PTY (Lead's task
    specs, peer-to-peer takkub send, slash-command injection). Short
    inputs are returned unchanged so single-character prompts still
    feel like typing rather than a paste burst.
    """
    # M6#28: strip any embedded bracketed-paste markers from the content first.
    # An attacker-influenced task/message carrying an ESC[201~ end-marker would
    # otherwise terminate paste mode early, and the bytes after it (including a
    # \r) would be interpreted as LIVE keystrokes — auto-submitting an injected
    # command into the pane's TUI. The markers are never legitimate content, so
    # removing them is always safe (do it regardless of length, since the short
    # path writes the text straight through too).
    if _PASTE_START in text or _PASTE_END in text:
        text = text.replace(_PASTE_START, "").replace(_PASTE_END, "")
    if len(text) < BRACKETED_PASTE_THRESHOLD:
        return text
    return _PASTE_START + text + _PASTE_END


def _rewrite_task_for_codex(task: str) -> str:
    """Prepend an unambiguous override notice when sending a task to a codex pane.

    Codex tends to over-interpret Lead's standard
    `[ROLE: ... ห้าม spawn subagent]` prefix as forbidding any external
    orchestration — including the mandatory `takkub done` shell command.
    The planted AGENTS.md tries to prevent this but loses to the more-
    proximal inline ROLE prefix. We inject a same-proximity clarification
    before the task so the override cannot be ranked below the constraint.
    Idempotent: if the notice marker is already present we return unchanged
    (e.g. orchestrator replays the stored task after auto-respawn).
    """
    if _CODEX_TASK_NOTICE in task:
        return task
    return _CODEX_TASK_NOTICE + task


def _enter_delay_ms(payload: str) -> int:
    """Pick the post-write delay before sending Enter to submit input.

    Short/typed payloads use the snappy typing delay. Bracketed pastes use a
    base delay that grows with payload size — large pastes take longer to
    render their placeholder, and an Enter sent before the render completes is
    consumed inside the paste buffer instead of submitting (issue #22)."""
    if not payload.startswith(_PASTE_START):
        return _TYPING_ENTER_DELAY_MS
    kb = len(payload.encode("utf-8")) // 1024
    return min(_PASTE_ENTER_DELAY_MS + kb * _PASTE_PER_KB_DELAY_MS, _PASTE_MAX_ENTER_DELAY_MS)


def _render_daily_digest(
    project: str,
    when: datetime,
    sessions: list[tuple[str, str, str]],
    decisions: list[dict] | None = None,
) -> str:
    """Render one Finish-Job digest section for a project.

    `sessions` is a list of (HHMMSS, role, note_first_line) tuples
    drawn from `runtime/sessions/<date>/<project>/*.md`. Most recent
    first so the user scanning the daily note sees the latest work
    at the top.

    `decisions` (optional) is a list of {timestamp, heading, ...}
    dicts from `chatlog_scanner.extract_decisions` — assistant
    messages with H2 headings that look like recap / structured
    output. Surfaces under a "Decisions today" sub-bullet so the
    user can scan what was decided without opening any pane.

    Output is a single H2 section so multiple Finish Job invocations
    on the same day (different projects, different times) can append
    without clobbering each other.
    """
    lines: list[str] = []
    lines.append(f"## `{project}` · wrapped at {when.strftime('%H:%M:%S')}")
    lines.append("")
    if not sessions:
        lines.append("_No `takkub done` events recorded today for this project._")
        lines.append("")
    else:
        lines.append(f"**Sessions completed today: {len(sessions)}**")
        lines.append("")
        for stamp, role, note in sessions:
            # First line of the note is the human summary; collapse multi-line
            # notes to one line so the daily file stays scannable.
            first = (note or "").strip().splitlines()[0] if (note or "").strip() else ""
            if first:
                lines.append(f"- `{stamp}` **{role}** — {first}")
            else:
                lines.append(f"- `{stamp}` **{role}**")
        lines.append("")
    if decisions:
        lines.append(f"**Decisions today: {len(decisions)}**")
        lines.append("")
        for d in decisions:
            ts = d.get("timestamp") or ""
            ts_short = ts.replace("T", " ")[:16] if ts else ""
            heading = (d.get("heading") or "").strip()
            if heading:
                lines.append(f"- `{ts_short}` {heading}")
        lines.append("")
    return "\n".join(lines)


def _render_hot_md(
    panes_by_project: dict[str, dict[str, str]],
    active_project_name: str | None,
    recent_sessions: list[tuple[str, str, str]],
    now: datetime,
    hook_counts: dict[str, int] | None = None,
    friction: dict[str, int] | None = None,
) -> str:
    """Compose the body of `<vault>/hot.md` — the "what's happening
    right now in cockpit" snapshot the user opens to orient themselves.

    Inputs are plain values (no Pane / PtySession refs) so this can be
    unit-tested without spinning up Qt. `panes_by_project` is
    `{project: {role: state}}`. `recent_sessions` is a list of
    `(project, role, filename)` tuples — most recent first.
    `hook_counts` is `{hook_bucket: count}` from
    `chatlog_scanner.count_hook_fires` — surfaces noisy hooks
    (GateGuard, cost-critical, loop-warning, etc.) so the user can
    spot which hook is more annoying than useful and decide whether
    to mute it via ECC_DISABLED_HOOKS.
    """
    lines: list[str] = []
    lines.append("# Hot — cockpit live state")
    lines.append("")
    lines.append(f"_Last updated: {now.isoformat(timespec='seconds')}_")
    lines.append("")

    if active_project_name:
        lines.append(f"**Active project:** `{active_project_name}`")
    else:
        lines.append("**Active project:** _(none — projects.json `active` unset)_")
    lines.append("")

    if not panes_by_project:
        lines.append("## Panes")
        lines.append("")
        lines.append("_No projects open in cockpit._")
        lines.append("")
    else:
        lines.append("## Panes")
        lines.append("")
        for project in sorted(panes_by_project):
            lines.append(f"### `{project}`")
            roles = panes_by_project[project]
            if not roles:
                lines.append("- _(no panes)_")
            else:
                for role in sorted(roles):
                    lines.append(f"- **{role}** — {roles[role]}")
            lines.append("")

    lines.append("## Recent `takkub done` (last 10)")
    lines.append("")
    if not recent_sessions:
        lines.append("_(no done events this session)_")
    else:
        for project, role, fname in recent_sessions[:10]:
            lines.append(f"- `{project}` · **{role}** · {fname}")
    lines.append("")

    # Hook noise meter — only render the section when there's
    # something to report so a quiet day doesn't get a wall of zeros.
    if hook_counts:
        lines.append("## Hook noise today")
        lines.append("")
        # Loudest hook first so the eye lands on the worst offender.
        for hook, count in sorted(hook_counts.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"- **{hook}** — {count}")
        lines.append("")

    # Friction heatmap — surface "user corrected claude" and
    # "claude retried the same tool 3+ times" so the user sees
    # where workflow was rough. Same omit-when-empty rule.
    if friction and any(friction.values()):
        lines.append("## Friction today")
        lines.append("")
        c = int(friction.get("corrections", 0))
        r = int(friction.get("tool_retries", 0))
        if c:
            lines.append(f"- **user corrections** — {c}")
        if r:
            lines.append(f"- **tool retry storms** — {r}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_Auto-written by agent-takkub orchestrator every "
        f"{_HOT_MD_INTERVAL_MS // 1000}s. Edit-safe target is the project "
        "page; this file is overwritten on each tick._"
    )
    lines.append("")
    return "\n".join(lines)


def _cwd_within_project(cwd: str, project: str, role_name: str) -> bool:
    """True when `cwd` resolves under one of `project`'s configured roots.

    The cockpit repo-root bypass is intentionally restricted to Lead: teammates
    of unrelated projects must not inherit Lead's self-edit privileges.
    """
    # Read REPO_ROOT from the config module at call time so tests that
    # monkeypatch config.REPO_ROOT (or orch.REPO_ROOT) are honoured.
    from .config import REPO_ROOT as _repo_root

    target = pathlib.Path(cwd).resolve()
    if role_name == LEAD.name and (
        target == _repo_root.resolve() or _repo_root.resolve() in target.parents
    ):
        return True
    return any(target == root or root in target.parents for root in _allowed_project_roots(project))


def _exit_key(project: str, role: str) -> str:
    """Composite key for `_recent_exits` so the same role in different
    project tabs never shares a resume record."""
    return f"{project}::{role}"


def _resolve_project_memory(cwd: str | None) -> pathlib.Path | None:
    """Return the Lead's MEMORY.md path for the project rooted at *cwd*, or None.

    Claude Code encodes the project directory as the key under
    ``~/.claude/projects/`` by replacing the OS separator and colon with ``-``.
    For example ``C:\\Users\\alice\\web`` → ``C--Users-alice-web``.

    Returns None when *cwd* is absent or no memory file exists yet.
    """
    if not cwd:
        return None
    encoded = str(pathlib.Path(cwd).resolve())
    encoded = encoded.replace(os.sep, "-").replace(":", "-")
    mem = pathlib.Path.home() / ".claude" / "projects" / encoded / "memory" / "MEMORY.md"
    return mem if mem.exists() else None


def _build_transcript_path(project_ns: str, role_name: str) -> str | None:
    """Return an absolute path for the PTY byte-stream transcript file, or
    None to disable capture for this pane.

    The path mirrors the decision-log layout so the two artefacts live
    side-by-side under runtime/sessions/<date>/<project>/:
        <role>-<HHMMSS>.transcript.log   ← raw bytes (this function)
        <role>-<HHMMSS>.md               ← markdown summary (done())

    Setting TAKKUB_DISABLE_TRANSCRIPTS=1 returns None so no raw PTY bytes are
    persisted — an opt-out for sensitive projects whose panes may print
    tokens/.env/OAuth URLs that would otherwise land in a durable file and be
    re-injected into other agents via status/brief tails (issue #15). Every
    transcript reader already guards on a falsy path, so None is safe.
    """
    if os.environ.get("TAKKUB_DISABLE_TRANSCRIPTS", "").strip().lower() in ("1", "true", "yes"):
        return None
    now = datetime.now()
    day = RUNTIME_DIR / "sessions" / now.strftime("%Y-%m-%d") / project_ns
    try:
        day.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(day / f"{role_name}-{now.strftime('%H%M%S')}.transcript.log")
