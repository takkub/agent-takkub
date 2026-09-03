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
from .provider_spec import PROVIDER_REGISTRY
from .roles import LEAD
from .token_meter import encode_path_for_claude


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
#
# Model-id hygiene: this table hardcodes concrete Claude model ids as the
# *default* tier per role — separate from settings_window.py's
# _MODELS_BY_PROVIDER (a dropdown PRESET list that intentionally keeps older
# ids selectable) and plan_tier.py (usage-credit gating, keyed by whatever
# model a pane actually ends up running). When Anthropic ships a new
# generation, update the id HERE (the actual default everyone spawns with);
# _MODELS_BY_PROVIDER only needs the new id added as one more offered preset,
# it doesn't need the old ones removed. The two lists drifted once already
# (this table kept pointing at claude-opus-4-8 after claude-opus-5 shipped
# and became current) — check both when bumping.
_DEFAULT_TEAMMATE_TIER: tuple[str, str, str] = (
    "claude-sonnet-5",
    "medium",
    "claude-haiku-4-5",
)
_ROLE_MODEL_TIERS: dict[str, tuple[str, str, str]] = {
    "reviewer": ("claude-opus-5", "high", "claude-sonnet-5"),
    "critic": ("claude-opus-5", "high", "claude-sonnet-5"),
    # maintainer: full-system review + subtle-bug hunting on agent-takkub
    # itself — a gate-style workload, not high-frequency impl — so it sits
    # in the reviewer/critic tier rather than the backend/devops tier.
    "maintainer": ("claude-opus-5", "high", "claude-sonnet-5"),
    "backend": ("claude-sonnet-5", "high", "claude-haiku-4-5"),
    "devops": ("claude-sonnet-5", "high", "claude-haiku-4-5"),
    # codex/gemini substitutes: when the real binary is unavailable, Claude
    # backs the role — use Opus/high so the cross-check has the same quality
    # as reviewer/critic rather than falling to the default Sonnet tier.
    "codex": ("claude-opus-5", "high", "claude-sonnet-5"),
    "gemini": ("claude-opus-5", "high", "claude-sonnet-5"),
}

# ── #433 UI evidence gate for `takkub done` ──────────────────────────────────
# A frontend/mobile pane reporting done on a UI-shaped task must carry real
# screenshot evidence (user directive 2026-08-29: "ทำไมไม่ดูด้วยตาจริงตอนเก็บภาพเลย
# แล้วจะมา QA ทำไมสองรอบ"). Pure text/filesystem helper — no orchestrator state.
_UI_TASK_HINT_RE = re.compile(
    r"\b(ui|ux|css|layout|responsive|page|screen|component|modal|dialog|form|button|"
    r"navbar|sidebar|header|footer|carousel|banner|theme|dark mode|mobile|tailwind|"
    r"storybook|widget|render|redesign|design|style|animation|font|icon|dashboard)\b"
    r"|หน้า|จอ|ปุ่ม|ฟอร์ม|เมนู|สไตล์|ดีไซน์|เลย์เอาต์|คอมโพเนนต์",
    re.IGNORECASE,
)
_UI_NOT_VERIFIED_RE = re.compile(
    r"ยังไม่ได้เปิด|ไม่ได้เปิด\s*browser|route\s*ไป\s*qa|ส่งต่อ\s*qa|ให้\s*qa\s*(เช็ค|ตรวจ|ดู)|"
    r"not (?:visually )?verified|hand(?:ed)? (?:off )?to qa|for qa to (?:check|verify)",
    re.IGNORECASE,
)
_SCREENSHOT_PATH_RE = re.compile(r"[^\s\"'`<>()\[\]]+\.(?:png|jpe?g|webp)\b", re.IGNORECASE)
UI_NO_UI_MARKER = "[no-ui]"


def screenshot_paths_in_note(note: str) -> list[str]:
    return [m.group(0).strip("\"'`.,;:") for m in _SCREENSHOT_PATH_RE.finditer(note or "")]


def ui_evidence_gate(
    role: str,
    note: str,
    task_text: str | None,
    cwd: str | None = None,
    *,
    exists=None,
) -> str | None:
    """Return a rejection message when *role*'s done note lacks screenshot
    evidence for a UI-shaped task, else None.

    Gated only when the assigned task text reads as UI work (or the note
    itself admits the work was never opened in a browser) — a frontend pane
    finishing a pure-logic task with no visual impact is not asked for
    screenshots, and `[no-ui]` in the note opts out explicitly. `exists` is
    injectable for tests; defaults to `os.path.exists` against absolute
    paths or paths joined onto *cwd*.
    """
    from .pane_guard import UI_SELF_VERIFY_ROLES, normalise_role

    if normalise_role(role) not in UI_SELF_VERIFY_ROLES:
        return None
    text = note or ""
    if UI_NO_UI_MARKER in text.lower():
        return None
    admits_unverified = bool(_UI_NOT_VERIFIED_RE.search(text))
    ui_task = bool(task_text) and bool(_UI_TASK_HINT_RE.search(task_text or ""))
    if not ui_task and not admits_unverified:
        return None
    if admits_unverified:
        return (
            f"done ถูกปฏิเสธ (#433): note บอกว่ายังไม่ได้เปิดดูจริง/จะให้ qa ดู — งาน UI ต้อง "
            f"self-verify เอง: รัน app จับ screenshot (mobile 390px + desktop 1440px) ของทุกหน้าที่แตะ "
            f"บันทึกลง $TAKKUB_ARTIFACTS_DIR/screenshots/ ดูด้วยตาเทียบโจทย์ แล้วใส่ path ภาพใน note "
            f"(บรรทัดละไฟล์) ค่อย done ใหม่ · งานที่ไม่มีผลต่อหน้าจอ ใส่ {UI_NO_UI_MARKER} · "
            f"เสร็จจริงแต่ระบบบันทึกผิด → `takkub done --force`"
        )
    paths = screenshot_paths_in_note(text)
    if not paths:
        return (
            f"done ถูกปฏิเสธ (#433): งาน UI ของ {role} ต้องแนบ path screenshot จริงใน note "
            f"(mobile 390px + desktop 1440px ของทุกหน้า/คอมโพเนนต์ที่แตะ บันทึกลง "
            f"$TAKKUB_ARTIFACTS_DIR/screenshots/ แล้วใส่ path บรรทัดละไฟล์ — ไม่ต้อง embed รูป) · "
            f"ไม่มีผลต่อหน้าจอ → ใส่ {UI_NO_UI_MARKER} ใน note · `--force` ถ้าระบบบันทึกผิด"
        )
    if exists is None:

        def exists(p: str) -> bool:
            candidate = p if os.path.isabs(p) or not cwd else os.path.join(cwd, p)
            return os.path.isfile(candidate)

    missing = [p for p in paths if not exists(p)]
    if len(missing) == len(paths):
        return (
            f"done ถูกปฏิเสธ (#433): ไม่พบไฟล์ screenshot ตาม path ใน note ({missing[0]}) — "
            f"ต้องเป็นไฟล์จริงที่จับจาก app ที่รันอยู่ ไม่ใช่แค่ข้อความอ้างว่าเปิดดูแล้ว"
        )
    return None


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
# Sourced from PROVIDER_REGISTRY["codex"].task_notice_preamble (issue #103
# Phase 0) instead of owning the text here — provider_spec.py is now the
# single source of truth; this name stays for every existing call site
# (orchestrator.py re-export, _rewrite_task_for_codex, tests) to keep working
# unchanged.
_CODEX_TASK_NOTICE = PROVIDER_REGISTRY["codex"].task_notice_preamble

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


def _truncate_at_word_boundary(text: str, max_chars: int) -> str:
    """Truncate *text* to at most *max_chars*, backing up to the nearest
    preceding space so the cut never lands mid-word (#241 — a Lead-facing
    headline cut with a hard char slice could sever a word, or worse a
    number/identifier, in a way a reader can't reconstruct). Falls back to
    the hard cut only when no space exists in range (single long token)."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    space = cut.rfind(" ")
    # Only back up to a space if doing so doesn't throw away most of the
    # budget (a space very early in a long line means no real word boundary
    # exists nearby — keep the hard cut rather than truncating too eagerly).
    if space > max_chars * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _notice_fingerprint(body: str) -> str:
    """Stable short hash of a Lead-facing notice body (#241).

    Used to recognise "Lead already pulled this exact report via `takkub
    inbox`/`takkub wait`" so a later Lead Inbox Digest flush can collapse it
    to a one-line "(already read)" reference instead of re-pasting the full
    content Lead has already seen. Hashes the stripped body so leading/
    trailing whitespace differences between queue tiers don't miss a match."""
    import hashlib

    return hashlib.sha1(body.strip().encode("utf-8", errors="replace")).hexdigest()[:16]


def _looks_like_source_reference(line: str) -> bool:
    """True when *line* (already known to contain the #104 Windows Open-With
    dialog marker text) reads like source code/diff/log context rather than
    a live OS dialog surfacing through a pane's terminal (issue #199).

    The false positive that motivated this: the cockpit's OWN notify
    message (in `Orchestrator._check_shell_open_dialog`) quotes the marker
    text inside an f-string, so a pane editing or `Read`-ing that exact
    source line sees the marker echoed right back at it in its own
    transcript — the tripwire matching itself. Real dialog text never
    carries a Read-tool line-number prefix, a diff marker, or surrounding
    quote characters, so any of those is treated as source-code context and
    skipped. Pure string check — cheap enough to run per matching line on
    the scanning thread, no transcript file needed to unit-test it."""
    stripped = line.strip()
    if not stripped:
        return False
    if re.match(r"^\d+[:\t]", stripped):
        return True  # Read tool / `cat -n` line-number prefix
    if stripped[0] in "+-@":
        return True  # diff hunk line or `@@` header
    if "'" in line or '"' in line:
        return True  # quoted literal / f-string
    if "_SHELL_OPEN_DIALOG_MARKER" in line:
        return True  # the constant's own name
    return False


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
    """Return the configured ``(model, effort, fallback)`` role tier.

    The model and fallback IDs are Claude-specific. The low/medium/high effort
    value is also consumed by non-Claude providers whose ProviderSpec declares
    a supported session-scoped effort argument.
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
    # Multiline always takes the bracketed path regardless of length (PR #149,
    # @than-aa): a raw LF written to the PTY *is* Enter at the TUI layer (see
    # `lead_draft_state._ENTER_BYTES`, which counts 0x0A as submit), and
    # `_sanitize_pane_text` deliberately preserves \n in multi-line bodies. So a
    # short multi-line payload — a merged done-notice digest, a `takkub send`
    # message — used to submit at its first newline and arrive chopped across
    # turns. Bracketing it keeps the whole block atomic.
    if "\n" not in text and len(text) < BRACKETED_PASTE_THRESHOLD:
        return text
    return _PASTE_START + text + _PASTE_END


def _rewrite_task_for_codex(task: str) -> str:
    """Prepend an unambiguous override notice when sending a task to a codex pane.

    Codex tends to over-interpret Lead's standard
    `[ROLE: ... ห้าม spawn subagent เอง เว้นแต่ ... --mode subagent]` prefix as forbidding any external
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


# Verify/gate roles whose FAILING result should route back into a fix loop via
# `takkub done --fail`. critic is excluded on purpose: design critique always
# proposes improvements (it never "passes/fails"), so a --fail hint would make it
# always report failure.
_VERIFY_ROLES: frozenset[str] = frozenset({"qa", "reviewer"})

_VERIFY_FAIL_APPENDIX = (
    "\n\n------ verify reporting ------\n"
    "ถ้า verify/test **ไม่ผ่าน** (fail / regression / blocking issue): รายงานด้วย\n"
    '      takkub done --fail "<สรุป fail + root cause ถ้ารู้>"\n'
    "แทน `takkub done` ปกติ → Lead จะเสนอ fix loop ให้อัตโนมัติ "
    "(ผ่านหมด = `takkub done` ปกติ)"
)


def _append_verify_fail_hint(task: str, base_role: str) -> str:
    """For verify roles (qa/reviewer), append the `takkub done --fail` reporting
    instruction so a failed check routes back into a Lead-proposed fix loop.

    No-op for non-verify roles. Idempotent (marker-guarded) so the orchestrator
    replaying a stored task on auto-respawn doesn't stack duplicate copies.
    """
    if base_role not in _VERIFY_ROLES:
        return task
    if "verify reporting" in task:
        return task
    return task + _VERIFY_FAIL_APPENDIX


# Composed payloads shorter than this paste directly, same as before the
# file-based handoff existed — short tasks never hit paste-swallow issues
# and a pointer would just add a hop for no reason. Measured on the fully
# composed payload (goal block + role decl + verify hint), matching the
# final-review note (issue #1).
TASK_HANDOFF_THRESHOLD = 400


def _task_handoff_dir(project_ns: str) -> pathlib.Path:
    """Return today's task-handoff directory for *project_ns*, creating it.

    Mirrors ``_build_transcript_path``'s ``runtime/sessions/<date>/<project>/``
    convention. No ``<session>`` path segment: at assign time there is no
    live ``PaneState.session_uuid`` yet (the pane hasn't spawned/attached a
    claude session), so the file is addressed by timestamp+role only — good
    enough to be unique per assign (issue #1 final-review note).
    """
    day = RUNTIME_DIR / "tasks" / project_ns / datetime.now().strftime("%Y-%m-%d")
    try:
        day.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return day


def _task_handoff_pointer(
    task: str, project_ns: str, role_name: str, *, supports_file_read: bool = True
) -> tuple[str, str | None]:
    """Write *task* to a handoff file when it's long, returning what to paste.

    Returns ``(paste_text, task_file_path)``. For a short composed task
    (< ``TASK_HANDOFF_THRESHOLD`` chars) this is a no-op: ``paste_text is
    task`` and ``task_file_path is None``, so pasting behaves exactly as
    before the handoff mechanism existed. For a long task, the full text is
    written to ``RUNTIME_DIR/tasks/<project>/<date>/<HHMMSS>-<role>.md`` and
    a short pointer instructing the pane to ``Read`` it is returned instead —
    this is the only thing that gets pasted into the pane's PTY, so it can't
    hit the paste-swallow bug family (#22/#26) the way a multi-KB task can.

    ``supports_file_read`` — issue #273: pass the effective provider's
    ``ProviderSpec.supports_agent_file_read``. When False, the pointer is
    NEVER used regardless of length — a pane whose only file access is a
    disallowed shell tool cannot act on "read this file yourself" at all,
    so the handoff would just hand it a dead-end instruction and burn the
    whole assign as an instant, un-analyzable [FAILED] before any real work
    starts (confirmed live incident, #273). Falls back to the pre-#1 plain
    inline paste unconditionally for that provider instead — no chunking
    yet (``ponytail``: PTY paste already scales its enter-delay by content
    size via ``ProviderSpec.enter_delay_per_kb_ms``, so this is a return to
    a previously-working path, not a new risk; upgrade to chunked delivery
    if a provider without file-read AND with paste-swallow trouble shows up).

    The caller MUST still store the full, untouched *task* (not the pointer)
    in ``PaneState.last_assigned_task`` — that field is the crash-replay unit
    (``spawn_engine._auto_respawn``) and must keep working even if the
    handoff file is later deleted/moved.

    On a write failure (disk full, permissions) this degrades to returning
    the full task unchanged rather than losing the assignment.
    """
    if not supports_file_read:
        return task, None
    if len(task) < TASK_HANDOFF_THRESHOLD:
        return task, None
    day = _task_handoff_dir(project_ns)
    path = day / f"{datetime.now().strftime('%H%M%S')}-{role_name}.md"
    try:
        path.write_text(task, encoding="utf-8")
    except OSError:
        return task, None
    forward_path = str(path).replace(os.sep, "/")
    pointer = (
        f"[ROLE: {role_name}] อ่าน task spec เต็มจากไฟล์: {forward_path} "
        "เปิดอ่านไฟล์นี้ด้วยเครื่องมืออ่านไฟล์ของคุณ (file-read tool) — ห้ามรัน path "
        "เป็นคำสั่ง shell (#104) แล้วทำตามทั้งหมด · "
        "รายงาน takkub done เมื่อเสร็จ"
    )
    return pointer, forward_path


# #273: how long after assign a `done --fail` may still plausibly be about
# the pointer delivery itself, not the actual work. Real work — success or
# genuine failure — essentially never completes this fast; a report inside
# this window plus the wording check below is what distinguishes "couldn't
# open the handoff file" from an unusually-quick real task failure.
DELIVERY_POINTER_FAILURE_WINDOW_SEC = 120.0

# Matches the EXACT wording `_task_handoff_pointer` put in the pointer text
# above ("เครื่องมืออ่านไฟล์ของคุณ (file-read tool)"), plus its natural English
# echo — a pane reporting it can't act on that instruction quotes it back
# rather than inventing new phrasing. Provider-agnostic on purpose: this
# matches what COCKPIT ITSELF said, not any one CLI's own error text, so it
# keeps working regardless of which provider hits the gap.
_DELIVERY_POINTER_FAILURE_RE = re.compile(
    r"(file-?read\s*tool|เครื่องมืออ่านไฟล์)",
    re.IGNORECASE,
)


def is_delivery_pointer_failure(note: str, task_file: str | None, elapsed_sec: float) -> bool:
    """True when a `done --fail` report is really a TASK-DELIVERY failure
    (issue #273) — the pane couldn't open the file-pointer handoff
    `_task_handoff_pointer` wrote, so the assigned work never started —
    rather than a genuine failure of that work.

    Requires BOTH signals so a real (if unusually fast) task failure is
    never misclassified:
      - structural: this assign actually used the pointer mechanism
        (``task_file`` is not None) AND the report landed within
        ``DELIVERY_POINTER_FAILURE_WINDOW_SEC`` of the assign;
      - textual: the note echoes the pointer's own "file-read tool" wording.

    Root cause is fixed separately (`_task_handoff_pointer`'s
    ``supports_file_read`` gate skips the pointer entirely for a provider
    without one) — this is the belt-and-suspenders net for any pane that
    still hits an equivalent wall (a future provider, a mis-set capability
    flag, ...), so Lead is never sent chasing a "root cause" for work that
    never began.
    """
    if not task_file:
        return False
    if elapsed_sec > DELIVERY_POINTER_FAILURE_WINDOW_SEC:
        return False
    return bool(_DELIVERY_POINTER_FAILURE_RE.search(note or ""))


def _append_worktree_hint(
    task: str, branch: str, post_create: tuple[str, ...] = (), port: int = 0
) -> str:
    """For a `--isolation worktree` pane, append the commit-on-your-branch
    instruction (issue #81), plus the project's postCreate setup commands.

    Teammate role policy says "อย่า commit เอง รอ Lead" — correct for the SHARED
    tree, but on an isolated worktree the pane MUST commit its own branch or the
    done() finalize finds 0 commits + a dirty tree and can only keep-and-warn
    instead of sending the Lead a merge proposal (found live in the first e2e:
    backend staged the file then refused to commit, citing that policy). This
    hint scopes the override to the isolated branch only.

    postCreate commands (P2.2) run in the PANE's own shell rather than on the
    orchestrator's Qt thread: `pnpm install` can take minutes, the pane makes
    the output visible to the user, and a hang stalls one agent instead of the
    cockpit. Idempotent (marker-guarded) so auto-respawn replay doesn't stack.
    """
    if "workspace isolation" in task:
        return task
    setup = ""
    if post_create:
        cmds = "\n".join(f"  {c}" for c in post_create)
        setup = (
            "\n**ก่อนเริ่มงาน** รัน setup ของ worktree นี้ให้จบก่อน (ตามลำดับ · "
            "ถ้าตัวไหน fail ให้รายงาน Lead ผ่าน takkub send แล้วทำงานต่อเท่าที่ทำได้):\n"
            f"{cmds}\n"
        )
    if port:
        setup += (
            f"\n**dev server ของ worktree นี้ = port {port} เท่านั้น** "
            f"(`export PORT={port}` หรือ flag ของ framework) — ห้ามใช้ port default "
            "ของโปรเจค เพราะ pane อื่นใช้อยู่ · long-running ต้อง background เสมอ "
            f"(`nohup ... > /tmp/dev-{port}.log 2>&1 &`)\n"
        )
    return task + (
        "\n\n------ workspace isolation ------\n"
        f"คุณทำงานบน **git worktree + branch แยกของคุณเอง** (`{branch}`) — "
        "เมื่องานเสร็จ **ต้อง `git add` + `git commit` บน branch นี้ด้วยตัวเอง** "
        "(นโยบาย 'รอ Lead commit' ใช้กับ shared tree เท่านั้น — branch นี้ override) "
        "ถ้าต้องการงานล่าสุดจาก base ให้ `git merge <base-branch>` เข้า branch นี้เองได้ "
        "(#385 — worktree ของคุณเท่านั้น) · push ได้เฉพาะ branch นี้แบบระบุชื่อ "
        f"(`git push -u origin {branch}` — ห้าม force, #438) เมื่อ task ต้องให้ CI ตรวจก่อน done · "
        "ห้าม switch branch/rebase · "
        "Lead จะ review + merge กลับ base "
        "หลังคุณ `takkub done`" + setup
    )


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


def _project_root_dir(project: str) -> pathlib.Path | None:
    """The project's own root — the common parent directory of every path
    configured for `project` (mirrors the "common parent" step of
    `config.lead_cwd()`, but is not role-restricted).

    A monorepo-style project configures per-role paths (`web`, `api`, …)
    that all live under one shared root; a cwd landing exactly on that root
    is already implied to be legitimate (it's the parent of everything the
    project *does* allow), so any role — not just Lead — may use it.
    Returns None when the project has no configured paths, or when their
    common parent doesn't exist on disk.
    """
    roots = _allowed_project_roots(project)
    if not roots:
        return None
    try:
        common = pathlib.Path(os.path.commonpath([str(p) for p in roots]))
    except ValueError:
        return None
    if common.is_dir() and common.parent != common:
        return common
    return None


def _cwd_within_project(cwd: str, project: str, role_name: str) -> bool:
    """True when `cwd` resolves under one of `project`'s configured roots
    (or the project's own root — see `_project_root_dir`).

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
    # Per-pane git worktrees (issue #81) live under the cockpit-managed root
    # (<DATA_HOME>/worktrees/<project>), NOT under a configured project path, so
    # they'd fail the check below. They are a controlled location the
    # orchestrator itself creates for THIS project, so allow a cwd resolving
    # under this project's managed worktree root.
    try:
        from .worktree_manager import worktree_root

        wt_root = worktree_root(project)
        if target == wt_root or wt_root in target.parents:
            return True
    except Exception:
        pass
    if any(target == root or root in target.parents for root in _allowed_project_roots(project)):
        return True
    # #419: the cockpit-level skills store is a legal target for every role
    # of every project, so Lead can delegate "write a global skill" via
    # `takkub assign --cwd <GLOBAL_SKILLS_HOME>` instead of authoring it by
    # hand (which the Lead rules forbid). Read from config at call time so a
    # monkeypatched home is honoured.
    try:
        from .config import global_skills_dir

        g = global_skills_dir()
        if target == g or g in target.parents:
            return True
    except Exception:
        pass
    project_root = _project_root_dir(project)
    return project_root is not None and (target == project_root or project_root in target.parents)


def _resolve_pane_pretrust_root(cwd: str, project: str) -> pathlib.Path | None:
    """The narrowest project-registered root that *cwd* resolves under, for
    pre-trusting a claude-backed pane's spawn cwd (#476 — the generalization
    of #444 to plain ``assign --cwd``, not just ``--isolation worktree``).

    Only ever returns a root `_cwd_within_project` would already have
    accepted for *some* role of *project* — one of `_allowed_project_roots`
    or their common `_project_root_dir` — never the raw *cwd* itself, so a
    caller writing this into Claude Code's ``.claude.json`` (see
    `worktree_manager.pre_trust_pane_cwd`) never trusts a path just because a
    role happened to be pointed at it. Returns None when *cwd* is not under
    anything registered, or when it falls under this project's managed
    worktree root — that case is already covered by the wider shared
    `worktree_manager.pre_trust_worktrees_root`, and an extra narrower write
    here would only be redundant.

    "Narrowest" (not automatically the widest `_project_root_dir`) matters
    for a monorepo-style project with several disjoint registered paths —
    trusting the specific one *cwd* actually falls under is the smaller,
    more defensible scope."""
    try:
        target = pathlib.Path(cwd).resolve()
    except OSError:
        return None
    try:
        from .worktree_manager import worktree_root

        wt_root = worktree_root(project)
        if target == wt_root or wt_root in target.parents:
            return None
    except Exception:
        pass

    candidates = list(_allowed_project_roots(project))
    proot = _project_root_dir(project)
    if proot is not None:
        candidates.append(proot)

    best: pathlib.Path | None = None
    for root in candidates:
        try:
            root_r = root.resolve()
        except OSError:
            continue
        if target == root_r or root_r in target.parents:
            if best is None or len(str(root_r)) > len(str(best)):
                best = root_r
    return best


def _describe_valid_project_cwds(project: str) -> str:
    """Human-readable list of cwds that are legal for `project` — its
    per-role configured paths plus its own root, if one resolves (see
    `_project_root_dir`). Used to make "cwd rejected" errors actionable
    instead of just naming the bad path.

    A single-path project's root *is* that one configured path (the common
    parent of one path is itself), so appending it as a separate "(project
    root)" entry would just print the same path twice (#150). Only append
    when the root doesn't already match one of the configured paths; when it
    does, label that existing entry instead of duplicating it.
    """
    roots = _allowed_project_roots(project)
    project_root = _project_root_dir(project)
    valid_paths = [
        f"{p} (project root)" if project_root is not None and p == project_root else str(p)
        for p in roots
    ]
    if project_root is not None and project_root not in roots:
        valid_paths.append(f"{project_root} (project root)")
    try:
        from .config import global_skills_dir

        valid_paths.append(f"{global_skills_dir()} (global skills, #419)")
    except Exception:
        pass
    return ", ".join(valid_paths) if valid_paths else "(no paths configured for this project)"


def cwd_validation_error(cwd: str, project: str, role_name: str) -> str | None:
    """None when `cwd` is a legal spawn/assign target for `role_name` in
    `project`; otherwise a human-readable error naming the valid paths.

    Single source of truth for the "cwd escapes project" rejection —
    called from both the synchronous CLI-facing check (`cli_server.py`,
    before the request is acknowledged) and the async spawn-time safety net
    (`spawn_engine.py`, for callers that reach `assign()`/`spawn()` directly
    without going through the socket, e.g. worktree/auto-respawn paths).
    """
    if _cwd_within_project(cwd, project, role_name):
        return None
    return (
        f"cwd '{cwd}' is outside project '{project}' paths. "
        f"valid paths: {_describe_valid_project_cwds(project)}"
    )


# #422 item 1 — closed vocabulary for WHY a watchdog recovery fired. Every
# `*_pane_recover` event carries exactly one of these in `reason` so
# `takkub ma` / auto-issue can bucket recoveries without re-deriving the
# cause from surrounding events by hand (what closing #418 took).
RECOVERY_REASONS: tuple[str, ...] = (
    "content_static",  # screen (spinner-filtered) unchanged >= STUCK_THRESHOLD_S
    "idle_no_response",  # sat at its prompt through >=1 idle reminder, then went static
    "child_alive_grace_expired",  # a real child process was alive, grace ran out
    "no_first_content",  # never printed anything after spawn (NO_CONTENT_WATCHDOG_SEC)
    "no_first_content_retry_failed",  # same, on the retry → degrade to claude
    "auth_failed",  # provider printed an auth-failure marker
    "account_pending",  # provider stuck on account-verification gate (#346)
)


def classify_stuck_reason(*, idle_rounds: int, live_child_defer_since: float) -> str:
    """Pick the `reason` for a `stuck_pane_recover` from the two signals the
    watchdog already tracks (see `RECOVERY_REASONS`). Idle reminders win over
    a live-child grace expiry: a pane that was nudged and never answered is a
    stopped agent regardless of what its shell left running."""
    if idle_rounds > 0:
        return "idle_no_response"
    if live_child_defer_since > 0:
        return "child_alive_grace_expired"
    return "content_static"


def recovery_snapshot(
    session,
    *,
    now: float,
    last_output_ts: float | None,
    last_content_ts: float | None,
    assign_ts: float | None,
    children: list[str] | None = None,
    spinner_phrases: tuple[str, ...] = (),
    tail_lines: int = 5,
) -> dict:
    """Bounded diagnostic snapshot attached to every `*_pane_recover` event
    (#422): the last non-blank screen lines (spinner lines dropped, each
    clipped to 120 chars), the seconds since last PTY byte / last content
    change / last assign, and any live non-scaffolding children. Never
    raises — a dead session just yields an empty tail."""
    tail: list[str] = []
    try:
        lines = list(session.display_lines()) if session is not None else []
    except Exception:
        lines = []
    phrases = tuple(spinner_phrases)
    for ln in reversed(lines):
        stripped = ln.strip()
        if not stripped or any(p in stripped.lower() for p in phrases):
            continue
        tail.append(stripped[:120])
        if len(tail) >= tail_lines:
            break
    tail.reverse()

    def _since(ts: float | None) -> int | None:
        if not isinstance(ts, (int, float)) or ts <= 0:
            return None
        return int(now - ts)

    snap: dict = {
        "tail": tail,
        "since_last_byte_s": _since(last_output_ts),
        "since_content_change_s": _since(last_content_ts),
        "since_assign_s": _since(assign_ts),
    }
    if children:
        snap["children"] = list(children)[:10]
    return snap


def _exit_key(project: str, role: str) -> str:
    """Composite key for `_recent_exits` so the same role in different
    project tabs never shares a resume record."""
    return f"{project}::{role}"


def _resolve_project_memory(cwd: str | None) -> pathlib.Path | None:
    """Return the Lead's MEMORY.md path for the project rooted at *cwd*, or None.

    Claude Code encodes the project directory as the key under
    ``~/.claude/projects/`` by replacing every non-alphanumeric character
    with ``-``. The canonical token-meter encoder is shared here so separators,
    colons, underscores, and dots all match Claude's directory name.

    Returns None when *cwd* is absent or no memory file exists yet.
    """
    if not cwd:
        return None
    encoded = encode_path_for_claude(cwd)
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
