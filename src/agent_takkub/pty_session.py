"""PTY session: pywinpty + pyte screen model.

A PtySession owns:
  - a pywinpty PtyProcess running `claude.exe` (or any command)
  - a background QThread reading bytes from the pty
  - a pyte.Screen for ANSI parsing
  - signals: outputUpdated (whenever pyte screen changes), processExited

The terminal widget consumes the screen state, the orchestrator triggers writes.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import IntEnum

import pyte
from PyQt6.QtCore import QCoreApplication, QObject, QThread, QTimer, pyqtSignal
from wcwidth import wcwidth

from ._pty_backend import spawn_pty_bounded
from ._win_console import hide_hwnds, hide_own_console_windows, snapshot_console_hwnds
from .job_object_manager import JobObjectManager
from .provider_spec import (
    AUTH_TRANSIENT_GRACE_SEC,
    PROVIDER_REGISTRY,
    account_pending_markers_for,
    auth_error_markers_for,
    auth_transient_markers_for,
    quota_markers_for,
    tool_running_markers_for,
)
from .provider_spec import GENERIC_QUOTA_MARKERS as _DEFAULT_RATE_LIMIT_MARKERS
from .provider_spec import READY_HARD_BLOCKERS as _READY_HARD_BLOCKERS
from .provider_spec import READY_RULES as _READY_RULES

# CREATE_NO_WINDOW so the helper taskkill doesn't flash a console window.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Bound on the native pty-constructor call itself (issue #139): a wedged
# pywinpty/ptyprocess spawn once blocked the Qt main thread — and the
# spawn-in-progress FIFO arbiter behind it (spawn_engine.py) — for 47+
# minutes with no way to recover. The native call normally completes in well
# under a second (it just creates a process handle); anything past this is
# already pathological. Overrideable for slow/loaded CI hardware.
PTY_SPAWN_TIMEOUT_SEC = float(os.environ.get("TAKKUB_PTY_SPAWN_TIMEOUT_SEC", "30"))
PTY_WRITER_QUEUE_MAX = max(16, int(os.environ.get("TAKKUB_PTY_WRITER_QUEUE_MAX", "128")))
PTY_WRITER_CONTROL_RESERVE = max(
    1, min(PTY_WRITER_QUEUE_MAX // 2, int(os.environ.get("TAKKUB_PTY_CONTROL_RESERVE", "8")))
)
PTY_BATCH_MS = max(10, int(os.environ.get("TAKKUB_PTY_BATCH_MS", "50")))
PTY_BATCH_BYTES = max(4096, int(os.environ.get("TAKKUB_PTY_BATCH_BYTES", str(64 * 1024))))
TRANSCRIPT_FLUSH_MS = max(25, int(os.environ.get("TAKKUB_TRANSCRIPT_FLUSH_MS", "200")))
TRANSCRIPT_FLUSH_BYTES = max(
    4096, int(os.environ.get("TAKKUB_TRANSCRIPT_FLUSH_BYTES", str(64 * 1024)))
)


def _safe_screen_display(screen: pyte.Screen) -> list[str]:
    """``pyte.Screen.display`` rendered defensively against orphaned wide-char stubs.

    pyte writes a ``data=""`` stub into the cell *after* a wide (width-2)
    character. When a TUI redraw overwrites that wide char with a narrower one —
    ubiquitous in the claude/agy/codex spinner + status lines that repaint a
    progress glyph (emoji / CJK / box-drawing) many times a second — the stub is
    orphaned, and pyte's own ``display`` property then crashes with
    ``IndexError: string index out of range`` on ``wcwidth(char[0])`` (it indexes
    ``char[0]`` without guarding ``char == ""``).

    That exception fires on *every* idle-watchdog tick via ``display_lines()``
    (``is_at_ready_prompt`` / ``has_unparsed_tool_call`` / ``is_blocked_on_tty_prompt``).
    The per-pane try/except in ``_check_idle_teammates`` then swallowed it, which:
      • on a teammate pane — skipped the forgot-``takkub done`` reminder + harvest
        hint, so the pane sat idle until the user closed it, never reporting; and
      • on a Lead pane — made ``is_at_ready_prompt`` raise inside the notify pump /
        reaper, so queued done notices never reached the Lead.
    Both surface as "pane finished, closed, never reported back" — and get worse
    with many panes/projects open (more terminals → higher odds one holds a poison
    stub). Rendering the stub as empty instead of indexing it removes the crash at
    the source. See runtime/events.log ``idle_watchdog_pane_error`` entries.
    """
    rows: list[str] = []
    for y in range(screen.lines):
        line = screen.buffer[y]
        chars: list[str] = []
        skip_stub = False
        for x in range(screen.columns):
            if skip_stub:  # the legitimate stub right after a wide char
                skip_stub = False
                continue
            data = line[x].data
            if not data:  # orphaned stub / empty cell — pyte would IndexError here
                continue
            skip_stub = wcwidth(data[0]) == 2
            chars.append(data)
        rows.append("".join(chars))
    return rows


# ── Content-change fingerprint (#248/#247) ──────────────────────────────────
# `_last_output_ts` (seconds_since_output()) used to stamp on every raw PTY
# byte chunk, which made two very different states look identical to any
# caller polling it: a freshly-spawned pane whose terminal-init escape
# sequence (cursor-position report, mouse-mode enables — no visible glyph at
# all) renders nothing, and a CLI wedged on a static status line ("Signing
# in...") whose only motion is an animated spinner glyph. Both kept bumping
# the "last real output" clock forever, so lead_inbox.py's delivery busy-wait
# (#130/#144) and the stale-marker watchdog (#20) treated a hung pane as
# "still making progress" for the full BUSY_WAIT_CEILING_SEC/STUCK_THRESHOLD_S
# window instead of surfacing it.
#
# _content_fingerprint normalizes away the animation (spinner glyph, trailing
# ellipsis dots) and drops blank rows before comparing, so two frames that
# only differ by which spinner glyph is showing collapse to the same
# fingerprint — stamping becomes a strict "the visible content actually
# changed" signal instead of "a byte arrived".
_SPINNER_GLYPH_RE = re.compile(
    r"[⠀-⣿]"  # braille spinner block (⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷ … — claude/agy/codex all use some subset)
    r"|[●○◐◑◒◓]"  # dot/circle spinner frames
    r"|(?<!\S)[|/\\-](?!\S)"  # isolated ascii spinner glyph — bounded by
    # whitespace/edges so it can't eat a real hyphen inside a word/flag/path
    # ("a-b", "--force", "a/b" are left untouched)
)
_TRAILING_DOTS_RE = re.compile(r"\.+\s*$")
# #301/#308: a live elapsed-seconds counter next to a busy indicator ("esc to
# cancel · 412s)", "Running command... (5s)") changes every tick just like a
# spinner glyph, but isn't a glyph — it's plain digits — so it defeated the
# spinner-only normalization above. Real incident: an agy pane wedged on
# "Running command..." kept reading `takkub status` as "last progress: 1s
# ago" the whole time it was stuck, because the growing counter alone was
# enough to change the fingerprint every second. `\bNs\b` matches the counter
# regardless of what follows it (paren, "·", end of line) — deliberately NOT
# scoped to a specific provider's busy phrase the way orchestrator.py's
# `_check_stuck_panes` line-filter is, since this fingerprint has no
# provider context to key off of.
_ELAPSED_COUNTER_RE = re.compile(r"\b\d+s\b")


def _content_fingerprint(lines: list[str]) -> str:
    """Cheap fingerprint of the non-blank screen content with spinner
    animation normalized away. "" means the screen is entirely blank —
    nothing rendered yet (e.g. only a terminal-init escape sequence has been
    fed so far)."""
    rows: list[str] = []
    for line in lines:
        stripped = line.rstrip()
        if not stripped:
            continue
        stripped = _SPINNER_GLYPH_RE.sub("", stripped)
        stripped = _ELAPSED_COUNTER_RE.sub("", stripped)
        stripped = _TRAILING_DOTS_RE.sub("", stripped).rstrip()
        if stripped:
            rows.append(stripped)
    return "\n".join(rows)


def _tree_kill(pid: int | None) -> None:
    """Force-kill `pid` and its entire descendant process tree.

    The PTY backend's terminate() only reaps the root command (claude); grand-
    children spawned by a teammate — most painfully a `next dev` / `npm run dev`
    server and the postcss / jest-worker node subprocesses it forks — are left
    orphaned and accumulate into thousands of zombie node procs.

    Windows: `taskkill /T` walks the parent→child tree by PID.
    POSIX (macOS/Linux): ptyprocess spawns the child via ``setsid`` so it is the
    leader of its own process group; ``killpg`` on that group reaps the whole
    descendant chain in one signal — the POSIX equivalent of `taskkill /T`.

    Best-effort and non-blocking-ish: short timeout, never raises (a failure
    here must not prevent the rest of terminate() from running).
    """
    if pid is None:
        return
    if sys.platform == "win32":
        # Synchronous: `taskkill /T` must finish walking the LIVE parent→child
        # tree before the caller kills the root, or the descendants orphan (a
        # leaked `next dev` then survives — observed 3170 procs / 18 GB). This
        # blocks, so terminate() runs it on a background thread (see terminate),
        # NOT on the Qt main thread — keeping the ordering correct AND the UI
        # responsive. (An earlier fire-and-forget Popen here broke the ordering.)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
                timeout=10,
                check=False,
            )
        except Exception:
            pass
        return
    # POSIX: kill the child's whole process group (it is the session leader).
    try:
        import signal

        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        pass


# ── Malformed tool-call XML detection ───────────────────────────────────────
# When a model outputs tool-call XML without the required `antml:` namespace
# prefix (using bare `<invoke>` / `<parameter>` / `<function_calls>` instead
# of `<invoke>` etc.) the harness cannot parse and execute it — the XML
# simply renders as plain text and the pane appears to hang silently.
#
# Key invariant: a *well-formed* tool call is consumed by the harness before
# reaching the terminal renderer, so it never appears as literal text on-screen.
# Any occurrence of these tag patterns as visible text therefore means the tool
# call is malformed and was never executed.
#
# Patterns cover both opening and closing tags, with or without the `antml:`
# prefix, since either variant appearing as screen text signals a parse failure.
_MALFORMED_XML_RE = re.compile(
    r"<\s*/?\s*(antml:)?(invoke|parameter|function_calls)\b",
    re.IGNORECASE,
)
# Scan this many rows ending at the cursor row (tool-call XML tends to sit just
# above the cursor after the model finishes outputting it).
_MALFORMED_XML_TAIL_ROWS = 10


# ── Interactive TTY-prompt detection ────────────────────────────────────────
# When a shell command (npx, git, rm -rf) pauses for user input the pane
# output stops scrolling and the cursor sits on a y/N or credential line.
# The idle watchdog cannot tell this apart from "agent finished but forgot
# takkub done" — it fires reminders into the blocked pane indefinitely.
# `is_blocked_on_tty_prompt()` lets the watchdog distinguish the two states
# so it can log a warning and suppress repeated reminders instead.
#
# Patterns are anchored to the bottom _TTY_PROMPT_TAIL_ROWS rows (where the
# cursor rests after a prompt is emitted) to avoid matching identical text
# in earlier scrollback that the agent has already answered and moved past.
_TTY_PROMPT_RE = re.compile(
    r"ok to proceed\? \(y\)"  # npx first-install download gate
    r"|\[[yY]/[nN]\]"  # [Y/n] [y/N] [y/n] [Y/N]
    r"|\([yY]/[nN]\)"  # (y/n) (Y/n) (y/N)
    r"|press any key"  # generic pause / paginator
    r"|overwrite\?"  # rsync, cp -i, create-react-app
    r"|are you sure"  # git push --force guard, rm -rf guard
    r"|password:\s*$"  # git credential / SSH passphrase
    r"|username:\s*$",  # git credential username
    re.IGNORECASE | re.MULTILINE,
)
_TTY_PROMPT_TAIL_ROWS = 5

# Claude Code's own tool-permission approval dialog (Bash/Edit/Write/WebFetch,
# etc.) — a numbered "1. Yes / 2. Yes, and don't ask again / 3. No" menu. Unlike
# a generic shell y/N prompt (_TTY_PROMPT_RE above) it carries no [y/N] bracket
# at all, so a pane wedged here matched neither is_at_ready_prompt() (a hard-
# blocker footer like "esc to cancel" IS on screen, so it correctly reads not-
# ready) nor is_blocked_on_tty_prompt() (regex miss) — it just silently read as
# ordinary busy generation forever (#236: a pane sat here 2h51m with `takkub
# status` reporting "working, progress 0s ago" throughout, because the raw
# transcript-mtime progress clock kept bumping on the dialog's own redraw).
# The *question* line varies per tool (Bash: "do you want to proceed?", Edit:
# "do you want to make this edit to <file>?", Write: "do you want to create
# <file>?", ...) and isn't exhaustively enumerable, so detection anchors on the
# numbered options instead: option 1 ("1. Yes") together with a confirming "No"
# option or "Esc to cancel" footer nearby — both render within the same dialog
# box but on separate screen rows, hence the two-pattern AND (not a single
# multi-line regex) over a joined window.
_PERMISSION_MENU_OPTION1_RE = re.compile(r"^\s*[❯>]?\s*1\.\s*yes\b", re.IGNORECASE | re.MULTILINE)
_PERMISSION_MENU_CONFIRM_RE = re.compile(
    r"esc\s*to\s*cancel|^\s*[❯>]?\s*\d\.\s*no\b", re.IGNORECASE | re.MULTILINE
)
_PERMISSION_MENU_TAIL_ROWS = 12

# "Enter [to] Confirm" — claude/codex word it "Enter to confirm", agy words
# its own trust modal "enter Confirm" with no "to" (#186). Tolerate both so
# is_at_trust_prompt() doesn't silently miss the agy variant.
_ENTER_CONFIRM_RE = re.compile(r"enter\s+(?:to\s+)?confirm", re.IGNORECASE)


# ── Ready-prompt detection markers (M4#17) ──────────────────────────────────
# is_at_ready_prompt() decides whether a pane is idle at its input prompt. The
# markers are provider-specific bottom-row UI strings; when an upstream CLI
# rewords its prompt, detection silently breaks and that provider's idle
# watchdog / done-gate stalls — this happened 3x (gemini input hint, gemini
# update footer #51, codex splash). Centralised here as ONE ordered table so a
# reword is a one-line patch, with an env override to rescue a reworded prompt
# without shipping code, and a doctor self-test that flags a stale marker.
#
# Version-dependence registry (#20) — every marker below is natural-language UI
# text owned by an upstream CLI, so ALL are version-dependent. By blast radius:
#   HIGH  'esc to interrupt' / 'esc to cancel' — the busy indicators. A reword
#         makes a working pane read idle (premature done-nudge / early gate).
#   HIGH  'bypass permissions' / 'shift+tab to cycle' (claude), '? for shortcuts'
#         (agy), 'type your message or' (gemini), 'fast off' / 'fast on' (codex
#         composer status bar) — the idle footers. A reword makes an idle pane
#         read busy (watchdog never nudges; #70-style stall).
#   MED   'update available!' (codex splash), 'trust this folder' / 'do you trust'
#         / 'press enter to continue' (trust modals) — transient spawn-time gates.
#   LOW   'gemini cli update available!' — passive banner. ('openai codex (v'
#         is intentionally NOT a ready marker — see #99 note by _READY_RULES.)
# Replacing this text with structural signals (exit codes / pty mode / ANSI) is
# largely infeasible: the CLI is one long-lived interactive TUI (no exit code
# while running) always in raw mode (no discriminating pty flag). The mitigation
# is layered instead of a rewrite:
#   1. _ready_region() scopes matching to the bottom footer rows so conversation
#      body text quoting a marker can't poison the verdict (#70 root fix).
#   2. TAKKUB_EXTRA_READY_MARKERS lets an operator rescue a reword with no deploy.
#   3. ready_marker_selftest() (takkub doctor) catches a stale shipped marker.
#   4. The orchestrator's structural stale-marker detector (output-quiescence +
#      no-marker-match → 'ready_marker_possibly_stale' log with the real footer)
#      turns a field reword from a SILENT idle-watchdog stall into a loud,
#      actionable diagnostic. See Orchestrator._check_stale_markers.
#
# Hard blockers: any present → NEVER ready (active interrupt / modal), even if a
# ready marker is also on screen.
#
# _READY_HARD_BLOCKERS / _READY_RULES now come from provider_spec.py's
# PROVIDER_REGISTRY (issue #103 Phase 0) — imported above as
# READY_HARD_BLOCKERS / READY_RULES and aliased back to these names for every
# existing call site (is_at_ready_prompt, ready_marker_selftest, etc.) to keep
# working unchanged. provider_spec.py builds them by ORDERED CONCAT across the
# three shipped specs (gemini, then codex, then claude) — reproducing this
# exact table byte-for-byte — instead of a hand-written flat tuple, so a new
# provider only needs a new ProviderSpec entry, not an edit here.
#
# The concat order still matters and is still enforced there: gemini's
# persistent input hint + passive update footer must beat codex's bare
# "update available!" splash blocker (substring collision — gemini's "gemini
# cli update available!" contains codex's "update available!"). Codex's
# startup banner ("OpenAI Codex (vX.Y.Z)") is deliberately NOT a ready marker
# (#99) — it paints before codex finishes auto-booting its MCP servers, so
# treating the banner alone as ready raced task delivery into a still-busy
# composer. Codex readiness depends solely on its composer status bar ("Fast
# off"/"Fast on"), verified by direct pty capture to only render once codex
# has actually reached its interactive prompt. See provider_spec.py's
# codex_spec.ready_rules comment for the full detail.


def _extra_ready_markers() -> tuple[str, ...]:
    """Operator-supplied extra ready markers (lower-case substrings) to rescue an
    upstream-reworded prompt WITHOUT a code change. Checked after the hard
    blockers so an active interrupt/modal still wins. (M4#17)"""
    override = os.environ.get("TAKKUB_EXTRA_READY_MARKERS", "").strip()
    if not override:
        return ()
    return tuple(m.strip().lower() for m in override.split(",") if m.strip())


def _classify_ready(text_lower: str) -> bool:
    """Pure ready-prompt verdict over already-lowercased screen text. Shared by
    is_at_ready_prompt() and the doctor self-test so the two can't drift. (M4#17)

    Faithful to the original if/return chain: hard blockers (all → not ready)
    came first there too, so grouping them is equivalent; the ordered rules then
    reproduce the exact first-match-wins precedence."""
    for b in _READY_HARD_BLOCKERS:
        if b in text_lower:
            # REMOVED (#346): a 'verifying your account' + 'please try again
            # shortly' bypass used to live here on the assumption that phrase
            # combo means the account check failed and dropped back to a
            # normal composer. Live evidence proved it can appear while the
            # CLI is genuinely frozen accepting no input — see
            # provider_spec.gemini_spec's account_pending_markers comment.
            return False
    for marker in _extra_ready_markers():
        if marker in text_lower:
            return True
    for ready_when, marker in _READY_RULES:
        if marker in text_lower:
            return ready_when
    return False


# Ready/blocker markers are bottom-row TUI chrome — the footer hint, the spinner
# status line ('esc to interrupt'), the input box. Conversation BODY text scrolls
# ABOVE that region. Scoping detection to the bottom rows stops body text that
# merely *quotes* a marker string (e.g. a Lead discussing "esc to interrupt" or
# "bypass permissions") from poisoning the verdict — the root of the #70 false-
# busy stall and the #20 text-marker fragility. Mirrors _TTY_PROMPT_TAIL_ROWS,
# which already anchors tty-prompt detection to the bottom for the same reason.
_READY_TAIL_ROWS = 6

# #284: the boot-phase probe gets its OWN, taller window. `_READY_TAIL_ROWS` is
# sized for claude's single-line footer; codex draws a bordered composer plus a
# status bar above it, so its boot line ("Booting MCP server: … esc to
# interrupt") sits several rows higher and falls out of a 6-row window as soon
# as the composer grows by one line — at which point only "Fast off" remains
# visible and the pane reads READY while it is demonstrably still starting.
#
# Widening the READY scan is not the fix: that window is deliberately tight
# because body text quoting "esc to interrupt" poisoned the verdict (#70/#20).
# The boot strings are safe to look for further up — they are provider startup
# chrome, not phrases that show up in a conversation — so only this probe gets
# the taller window.
_BOOT_MARKER_TAIL_ROWS = 20

# Provider BOOT chrome: the CLI has not finished starting up, so there is no
# composer on screen yet to receive anything.
_BOOT_PHASE_MARKERS: tuple[str, ...] = (
    "booting mcp server",
    "starting mcp server",
)

# #380: codex 0.149's TUI paints its banner box FIRST — `model: loading` /
# `directory: loading` — with the composer ("Ask Codex to do anything",
# "? for shortcuts") already drawn under it. Every ready rule matches that
# footer, so the pane read READY while the CLI had not finished
# initialising; the pasted task was typed character-by-character into a
# TUI that was still booting and got dropped, delivery verify failed, and
# auto-recover restarted the pane into the exact same race (3 respawns →
# stuck-capped, never started). Headless `codex exec` from the same cwd
# answered in seconds, so this is purely the boot-window race. The banner
# pads the value column with a variable run of spaces, hence a regex, not a
# substring.
_BOOT_PHASE_MARKER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bmodel:\s+loading\b"),
    re.compile(r"\bdirectory:\s+loading\b"),
)


def _has_boot_phase_marker(region: str) -> bool:
    """*region* is already lowercased by `_tail_region`."""
    return any(m in region for m in _BOOT_PHASE_MARKERS) or any(
        r.search(region) for r in _BOOT_PHASE_MARKER_RES
    )


# Provider QUEUED-MESSAGE chrome: the composer exists, but the CLI is mid-turn,
# so input is queued rather than run. codex shows this whenever it is working —
# NOT only during boot.
#
# #281: these two used to be one list, and every consumer inherited "boot" as
# the meaning of all of it. That is right for the idle watchdog (the original
# consumer — neither state deserves a forgot-`takkub done` nag) and wrong for
# everything that reused it afterwards. Proven from a live day of events.log:
# panes flagged `[delivery-boot-stall] ค้างอยู่ที่ boot phase` at 110s went on
# to finish their work and call `done` minutes later (13:59→14:03,
# 15:45→15:54, 20:07→20:17) — they were never booting, they were WORKING with
# a queued message showing. Left unsplit, #276's new boot ceiling would have
# closed those panes and failed their tasks at 300s.
_QUEUED_MESSAGE_MARKERS: tuple[str, ...] = ("tab to queue message",)

# Union — the historical meaning, still what `shows_startup_marker` reports and
# what the idle watchdog wants: "not a genuine work turn to nag about".
_STARTUP_MARKERS: tuple[str, ...] = _BOOT_PHASE_MARKERS + _QUEUED_MESSAGE_MARKERS


def _tail_region(lines: list[str], rows: int) -> str:
    """Lowercased bottom *rows* non-blank-trailing screen rows."""
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    start = max(0, end - rows)
    return "\n".join(lines[start:end]).lower()


def _boot_marker_region(lines: list[str]) -> str:
    """Window the boot-phase probe scans — taller than the ready window so a
    provider whose composer is several rows tall cannot push its own boot line
    out of view (#284)."""
    return _tail_region(lines, _BOOT_MARKER_TAIL_ROWS)


def _ready_region(lines: list[str]) -> str:
    """Lowercased bottom _READY_TAIL_ROWS non-blank rows of the screen — the
    footer/status/input region where ready & blocker markers actually render.

    Trailing blank rows are stripped first so the window lands on real chrome on
    a partially-filled screen rather than empty padding. If a full-screen TUI has
    a pinned bottom status line at the terminal floor with blank padding above it
    (e.g. OpenCode), also scan the content block above the blank gap."""
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    candidate_ends = [end]
    # Check for a pinned floor status bar separated by blank lines in a full-height screen
    if end >= len(lines) - 2 and end > 1 and not lines[end - 2].strip():
        inner_end = end - 1
        while inner_end > 0 and not lines[inner_end - 1].strip():
            inner_end -= 1
        if inner_end > 0:
            candidate_ends.append(inner_end)

    chunks = []
    for e in candidate_ends:
        s = max(0, e - _READY_TAIL_ROWS)
        chunks.append("\n".join(lines[s:e]).lower())
    return "\n".join(chunks)


# ── Structural empty-composer fallback for claude (#343) ────────────────────
# #343: a claude pane sat "unrecognised" (is_at_ready_prompt() False, no other
# state predicate matching either) for ~9h straight. Live telemetry from that
# episode (134 consecutive ready_marker_possibly_stale captures, same pane,
# same shape every single time) shows the real screen was NOT garbled or
# mid-render — it was claude's ordinary bordered input box with an EMPTY
# prompt and no hint row at all:
#     ────────────────────────────────────────────  (border)
#     ❯                                              (prompt, no hint text)
#     ────────────────────────────────────────────  (border)
# i.e. the "bypass permissions" / "shift+tab to cycle" hint text this
# provider's ready_rules depend on (provider_spec.claude_spec) simply was not
# part of that particular repaint — confirmed by a live-captured OTHER footer
# from the same incident, "❯ ← for agents", which shows claude's footer is
# not one fixed hint string but several that come and go, none of which our
# marker table enumerates. Chasing the exact wording is the fragile game #20
# already tried once; this instead recognises the STRUCTURE that is common to
# all of them — an empty composer box — regardless of which hint (if any) is
# painted above it.
#
# Deliberately narrow and claude-ONLY (wired only where the caller already
# knows the provider — see Orchestrator._nudge_stale_marker /
# ._resolve_stale_marker_nudge, #343):
#   - only "❯" (claude's own prompt glyph, never the "❯>" leniency used
#     elsewhere in this file for other providers' menus) so an unrelated
#     shell's "$"/">" prompt can't collide;
#   - the prompt line must be EXACTLY the glyph after stripping — any
#     leftover typed/pasted text fails the match, so a genuine unsent
#     paste is never swallowed into "ready";
#   - requires the pattern on BOTH sides of a forced resize-redraw (see
#     PtySession.resize()'s #343 note) before a caller treats it as
#     confirmed-idle, so a screen caught mid-repaint (which could
#     coincidentally show a bare "❯" for one frame) doesn't false-positive.
_COMPOSER_BORDER_RE = re.compile(r"^[─━═-]{8,}$")
_COMPOSER_EMPTY_PROMPT_RE = re.compile(r"^❯$")


def _is_claude_empty_composer(lines: list[str]) -> bool:
    """True when the bottom of the screen is claude's bordered input box with
    a bare, empty prompt — see the module note above this function."""
    tail = [ln.strip() for ln in lines[-_READY_TAIL_ROWS:] if ln.strip()]
    for i in range(len(tail) - 2):
        top, mid, bot = tail[i], tail[i + 1], tail[i + 2]
        if (
            _COMPOSER_BORDER_RE.match(top)
            and _COMPOSER_EMPTY_PROMPT_RE.match(mid)
            and _COMPOSER_BORDER_RE.match(bot)
        ):
            return True
    return False


# Placeholder claude renders in its input box for a bracketed multi-line paste,
# e.g. "[Pasted text +42 lines]". Its presence in the input region confirms the
# paste actually landed (vs a swallowed paste that leaves the box empty — #26).
_PASTED_PLACEHOLDER = "[pasted text"
# Leading chars of the content to look for as a fallback presence signal when a
# short paste rendered inline (no placeholder).
_INPUT_FRAGMENT_LEN = 24


def _input_has_content(region: str, fragment: str) -> bool:
    """True when the bottom input region shows pasted/typed content.

    Two signals: the multi-line paste placeholder, or — for short inline
    content with no placeholder — a leading fragment of the expected text. The
    region is already lowercased by ``_ready_region``."""
    if _PASTED_PLACEHOLDER in region:
        return True
    frag = fragment.strip().lower()[:_INPUT_FRAGMENT_LEN]
    return bool(frag) and frag in region


# Canonical sample screens with their expected verdict — bake the behaviour so a
# marker going stale is caught by `takkub doctor` instead of silently breaking
# the idle watchdog. Each tuple is (screen_text, expected_is_ready, provider) —
# the provider tag (issue #103 Phase 0, design §5.6) says which spec the case
# "belongs to" and is used by ready_marker_selftest() to ALSO verify the case
# classifies correctly under that provider's own rules alone (no help from
# the other two specs' markers) — see _classify_ready_for_provider below.
_READY_SELFTEST_CASES: tuple[tuple[str, bool, str], ...] = (
    ("> \n? for shortcuts            Gemini 3.5 Flash (Medium)", True, "gemini"),  # agy idle
    # agy account gates can coexist with the normal idle footer but still
    # swallow Enter; both observed 2026-07-24 in issue #126 transcripts.
    ("⣷  Signing in...\n? for shortcuts", False, "gemini"),
    ("⚠ Verifying your account...\n? for shortcuts", False, "gemini"),
    # CORRECTED (#346): this case used to expect ready=True on the theory
    # that "please try again shortly" means the account check failed and
    # dropped back to a normal composer. A live incident (2026-08-22)
    # disproved that — this exact screen was frozen, accepting no input,
    # while cockpit's old rule read it as idle and blind-delivered a task
    # into it (silently lost, later reported as `delivery-uncertain`). The
    # trailing ">" is decorative chrome on this banner, not a live prompt —
    # see PtySession.account_pending_reason() for the correct handling.
    (
        "⚠ Verifying your account...\n"
        "  L We're finishing verifying your account eligibility.\n"
        "    This usually takes a moment. Please try again shortly.\n\n"
        "> ",
        False,
        "gemini",
    ),
    # agy busy: even if the '? for shortcuts' footer persists, an active
    # interrupt indicator is a hard blocker → not ready (no premature done-nudge).
    ("Thinking... (esc to interrupt)\n? for shortcuts", False, "gemini"),  # agy busy
    ("Type your message or @path/to/file", True, "gemini"),  # gemini CLI (legacy) idle
    ("Thinking... (esc to cancel, 12s)\nType your message or @path", False, "gemini"),  # busy
    (
        "Gemini CLI update available! 0.46.0 -> 0.47.0\nType your message or @path",
        True,
        "gemini",
    ),
    ("Gemini CLI update available! 0.46.0 -> 0.47.0", True, "gemini"),  # passive footer alone
    (
        "OpenAI Codex (v1.2.3)\nupdate available! run npm i -g @openai/codex",
        False,
        "codex",
    ),  # codex splash
    # codex banner alone, no composer status bar yet (#99): the earliest paint
    # after spawn, before codex has finished booting its own MCP servers. Must
    # NOT read ready — task delivery racing this window pasted into a composer
    # that was about to go busy with its own "esc to interrupt" MCP-boot
    # indicator, stranding the paste unsubmitted (root cause, issue #99).
    ("OpenAI Codex (v1.2.3)", False, "codex"),  # codex banner alone (no status bar)
    # codex idle: banner has scrolled off, only the composer status bar remains
    # in the tail region (captured via direct pty spawn, issue #26).
    (
        "gpt-5.5 medium · ~/project · 5h 79% left · weekly 86% left · Fast off",
        True,
        "codex",
    ),
    # codex busy: "esc to interrupt" hard blocker overrides the status bar,
    # which is still on screen mid-turn.
    (
        "Thinking...(esc to interrupt)\ngpt-5.5 medium · ~/project · Fast off",
        False,
        "codex",
    ),
    # codex MCP boot: same hard blocker covers the "Booting MCP server:
    # codex_apps" phase, which also renders the status bar underneath.
    (
        "• Booting MCP server: codex_apps (0s • esc to interrupt)\n"
        "gpt-5.5 medium · ~/project · Fast off",
        False,
        "codex",
    ),
    ("bypass permissions", True, "claude"),  # claude idle
    ("(esc to interrupt) building...\nbypass permissions", False, "claude"),  # claude busy
    # --- #26 hardening (558fcbe cross-check, gemini+codex) ---
    # codex update splash + status bar footer: the 'update available!' blocker
    # precedes 'fast off/on' in _READY_RULES → stays not-ready.
    (
        "update available! run npm i -g @openai/codex\ngpt-5.5 medium · ~/project · Fast off",
        False,
        "codex",
    ),
    # codex idle, 'Fast on' variant — the on/off toggle both mark ready.
    (
        "gpt-5.5 medium · ~/project · weekly 86% left · Fast on",
        True,
        "codex",
    ),  # codex idle (fast on)
    # cross-provider contamination: a busy pane whose bottom rows quote
    # 'fast off' still shows its hard blocker, which wins before ready rules.
    # (_classify_ready checks hard blockers first, before any ready marker.)
    # Tagged "claude" for the per-provider pass — claude's own hard blockers
    # include 'esc to interrupt' too, so the case is self-contained there
    # without needing codex's 'fast off' rule at all.
    ("(esc to interrupt) building...\nsomeone mentions fast off", False, "claude"),
    # kimi idle footer (#257), captured via direct ConPTY capture against a
    # signed-in kimi-cli 1.49.x session on Windows, 2026-08-16 — see
    # kimi_spec.ready_rules's comment for the full captured line.
    (
        "main  @: mention files | ctrl-x: toggle mode | shift-tab: plan mode | ctrl+o: editor",
        True,
        "kimi",
    ),
)


def _classify_ready_for_provider(text_lower: str, provider: str) -> bool:
    """Same precedence as _classify_ready, scoped to ONE provider's own spec.

    Used only by ready_marker_selftest() to prove a tagged case classifies
    correctly from that provider's rules alone — i.e. it doesn't secretly
    depend on another provider's marker being present in the compat concat.
    Ignores TAKKUB_EXTRA_READY_MARKERS, like the shipped-table self-test
    itself. (issue #103 Phase 0, design §5.6)"""
    spec = PROVIDER_REGISTRY[provider]
    for b in spec.ready_hard_blockers:
        if b in text_lower:
            # REMOVED (#346): see the matching note in _classify_ready above.
            return False
    for rule in spec.ready_rules:
        if rule.marker in text_lower:
            return rule.ready_when
    return False


def ready_marker_selftest() -> list[str]:
    """Run the canned ready/busy screens through _classify_ready and return a
    list of human-readable failures (empty = all good). Called by doctor so a
    stale ready marker surfaces as a diagnostic rather than a silent stall. The
    env override is intentionally ignored here — the self-test validates the
    SHIPPED table. (M4#17)

    Each case is also checked against its tagged provider's spec ALONE
    (issue #103 Phase 0) — this is the check that would have caught the
    "gemini cli update available!" / "update available!" substring collision
    independent of concat order, since it fails if a case secretly relies on
    another provider's rule being present in the combined table."""
    failures: list[str] = []
    saved = os.environ.pop("TAKKUB_EXTRA_READY_MARKERS", None)
    try:
        for text, expected, provider in _READY_SELFTEST_CASES:
            got = _classify_ready(text.lower())
            if got != expected:
                first = text.splitlines()[0] if text else ""
                failures.append(
                    f"ready-marker selftest: {first!r} expected ready={expected}, got {got}"
                )
            got_spec_only = _classify_ready_for_provider(text.lower(), provider)
            if got_spec_only != expected:
                first = text.splitlines()[0] if text else ""
                failures.append(
                    f"ready-marker selftest ({provider} spec alone): {first!r} "
                    f"expected ready={expected}, got {got_spec_only}"
                )
    finally:
        if saved is not None:
            os.environ["TAKKUB_EXTRA_READY_MARKERS"] = saved
    return failures


# ── Claude usage-limit detection ────────────────────────────────────────────
# When a claude pane hits the plan's usage limit it stops producing output and
# prints a "limit reached … resets <time>" banner. Without detection the idle
# watchdog mistakes this for "finished but forgot takkub done" and nags every
# 90s (and eventually force-respawns into the same limit). These markers let
# the orchestrator recognise the state, suppress the watchdog, and notify when
# the limit resets instead.
#
# ⚠ The exact wording + time format are Claude-Code-version-dependent and must
# be verified against a real limit banner. Override without a code change via
# TAKKUB_RATE_LIMIT_MARKERS (comma-separated substrings, lower-case).
#
# Markers must be REACHED-STATE phrases, never the bare topic words. The bare
# "usage limit" marker false-positived on Claude Code v2.1.198's Fable-5 promo
# notice ("…use up to 50% of your plan's weekly usage limit on Fable 5. If you
# hit your limit…"), shown on EVERY fresh pane during the promo window. The
# false flag suppressed the idle watchdog for the 5 h fallback, which starved
# the reminder that rescues a swallowed task submit — panes sat on
# "[Pasted text +N lines]" forever (the QA fan-out stuck-paste incident,
# events.log 2026-07-02 09:20 resets_in_s=18000 ×3). Promo/marketing text talks
# about limits hypothetically ("if you hit your limit"); a real banner declares
# the limit HIT ("limit reached", "you've reached your usage limit") or names
# the reset ("your limit will reset at 3pm").
#
# #301: this table now lives as provider_spec.GENERIC_QUOTA_MARKERS (imported
# above as _DEFAULT_RATE_LIMIT_MARKERS) so a non-claude provider can OR its
# own confirmed wording into the same baseline via quota_markers_for() — see
# that module's "quota/usage-limit detection" section for the content itself
# and the per-provider tables (gemini/agy's field-verified "individual quota
# reached", codex's provisional reached-state phrasing).


def _rate_limit_markers() -> tuple[str, ...]:
    override = os.environ.get("TAKKUB_RATE_LIMIT_MARKERS", "").strip()
    if override:
        return tuple(m.strip().lower() for m in override.split(",") if m.strip())
    return _DEFAULT_RATE_LIMIT_MARKERS


# Reset clock-time, e.g. "resets 3pm", "resets at 3:30pm", "reset at 14:00".
_RESET_TIME_RE = re.compile(
    r"reset[s]?(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE
)
# Reset DURATION, e.g. gemini/agy's "Resets in 1h53m57s" (#301, field-verified
# 2026-08-18) — a countdown from now, not a clock-time-of-day like claude's
# banner above, so it needs its own parser rather than reusing _RESET_TIME_RE.
# At least one of h/m/s must be present with a real digit (an empty match —
# "resets in" with nothing after it — must not silently mean "0 seconds").
_DURATION_RESET_RE = re.compile(
    r"resets?\s+in\s+(?:(?P<h>\d+)\s*h)?\s*(?:(?P<m>\d+)\s*m)?\s*(?:(?P<s>\d+)\s*s)?",
    re.IGNORECASE,
)
# Fallback window when the banner is present but no parseable time is found —
# Anthropic's rolling window is ~5h, so wait that long before re-checking.
_RATE_LIMIT_FALLBACK_SEC = 5 * 60 * 60


def _parse_duration_reset(text: str, now: float) -> float | None:
    """Given lower-cased pane text, return `now` + the "resets in XhYmZs"
    countdown, or None if no such duration phrase is present / it has no
    digits at all (see _DURATION_RESET_RE's module note)."""
    m = _DURATION_RESET_RE.search(text)
    if not m:
        return None
    h = int(m.group("h") or 0)
    mnt = int(m.group("m") or 0)
    s = int(m.group("s") or 0)
    if h == 0 and mnt == 0 and s == 0:
        return None
    return now + h * 3600 + mnt * 60 + s


def _parse_rate_limit_reset(
    text: str, now: float, markers: tuple[str, ...] | None = None
) -> float | None:
    """Given lower-cased pane text, return the epoch the usage limit resets at,
    or None if no limit banner is present.

    *markers* defaults to the claude-only env-overridable table
    (`_rate_limit_markers()`) for back-compat; a caller checking a specific
    provider passes its own resolved list instead (#301) —
    `PtySession.rate_limit_reset_at()` does this via `_resolve_quota_markers()`.

    If a banner is present, tries the duration form first ("resets in
    XhYmZs" — gemini/agy, #301) then the clock-time form ("resets 3pm" —
    claude); if neither parses, falls back to now + ~5h so the watchdog
    still backs off rather than nagging forever.
    """
    active_markers = markers if markers is not None else _rate_limit_markers()
    if not any(m in text for m in active_markers):
        return None
    duration = _parse_duration_reset(text, now)
    if duration is not None:
        return duration
    m = _RESET_TIME_RE.search(text)
    if not m:
        return now + _RATE_LIMIT_FALLBACK_SEC
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return now + _RATE_LIMIT_FALLBACK_SEC
    lt = time.localtime(now)
    target = time.struct_time(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst)
    )
    epoch = time.mktime(target)
    if epoch <= now:  # clock time already passed today → it means tomorrow
        epoch += 24 * 60 * 60
    return epoch


def _resolve_quota_markers(provider: str) -> tuple[str, ...]:
    """Markers to check for `provider`'s quota/usage-limit state (#301):
    the `TAKKUB_RATE_LIMIT_MARKERS` env override if set (same override
    `_rate_limit_markers()` honors, useful for debugging any provider), else
    `provider_spec.quota_markers_for(provider)` (that provider's own
    confirmed list + the generic cross-provider baseline)."""
    override = os.environ.get("TAKKUB_RATE_LIMIT_MARKERS", "").strip()
    if override:
        return tuple(m.strip().lower() for m in override.split(",") if m.strip())
    return quota_markers_for(provider)


# gemini/agy footer model label, e.g. "Gemini 3.1 Pro · high" (idle) or
# "Gemini 3.5 Flash (Medium)" (legacy CLI, see the ready-marker fixture at
# module top) — confirmed shape only; used by PtySession.current_model_label()
# to surface a silent Pro→Flash quota-downgrade (#301).
_GEMINI_MODEL_RE = re.compile(
    r"(Gemini [\d.]+ (?:Pro|Flash)(?:\s*\([^)]*\))?(?:\s*[·:]\s*\w+)?)", re.IGNORECASE
)


# Console windows opened AFTER a pane spawns (#330 follow-up). The spawn-time
# sweep above only watches the 3.5 s around the PTY starting, which was enough
# for the conhost pywinpty surfaces — but codex opens a `pwsh.exe` shell-tool
# host on every shell command it runs, minutes into a session, and nothing was
# left watching for those. One process-wide timer handles every pane (a timer
# per pane would multiply an EnumWindows sweep by the pane count for no gain),
# and `hide_own_console_windows` restricts hiding to this process's own tree
# so a terminal the USER opened is never touched.
# 250ms, not the original 2s: this sweeper HIDES a console window after it has
# already been shown, so its interval IS the worst-case flash the user sees.
# The cost that made 2s look prudent isn't there — measured on this box,
# `hide_own_console_windows` in steady state (nothing new to rule on; the
# parent walk only runs for genuinely new hwnds) is **0.53 ms**, i.e. 0.2% of
# one core at 250ms. A window that appears right after a sweep is now visible
# for ~0.25s instead of ~2s.
#
# This does NOT make the flash impossible — only short. Eliminating it needs
# the window to never be shown in the first place, which we cannot do for a
# console created by a process we did not spawn (codex -> pwsh, npm -> cmd);
# a `SetWinEventHook(EVENT_OBJECT_SHOW)` hook would hide it on the event
# instead of on a poll. Not done here: unlike this timer it is a global hook
# with no CI mileage, and the first version of this sweeper already took the
# Windows CI job down with a silent Qt abort (#330 follow-up).
_CONSOLE_SWEEP_MS = 250
_console_sweeper: QTimer | None = None
_console_hwnds_seen: set[int] = set()


def _ensure_console_sweeper() -> None:
    """Start the process-wide console sweeper once (real Windows GUI only).

    Two guards, both learned the hard way when the first version of this took
    CI's Windows job down with a silent Qt abort at 78% of the suite:

    * `offscreen` platform → don't start at all. Under the test QPA there are
      no OS windows to hide, so the timer is pure cost — and a process-wide
      timer created inside one test keeps firing through every later test,
      across QApplication teardown, which is exactly how a Qt process dies
      with no traceback to point at the cause.
    * parent it to the application object, so it is destroyed with the app
      instead of outliving it as an ownerless QObject.
    """
    global _console_sweeper
    if sys.platform != "win32" or _console_sweeper is not None:
        return
    app = QCoreApplication.instance()
    if app is None:
        return
    platform = getattr(app, "platformName", None)
    if callable(platform) and platform() == "offscreen":
        return

    import os

    root_pid = os.getpid()

    def _sweep_owned() -> None:
        try:
            hide_own_console_windows(root_pid, _console_hwnds_seen)
        except Exception:
            # A sweep is cosmetic; never let it reach the Qt event loop.
            pass

    timer = QTimer(app)
    timer.setInterval(_CONSOLE_SWEEP_MS)
    timer.timeout.connect(_sweep_owned)
    timer.start()
    _console_sweeper = timer


class WritePriority(IntEnum):
    CONTROL = 0
    USER = 10
    TASK = 20
    BACKGROUND = 30


@dataclass(slots=True)
class PtyWriteMessage:
    data: str
    priority: WritePriority = WritePriority.USER
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    session_generation: int | None = None
    delivery_id: str | None = None
    kind: str = "user"
    message_id: str = field(default_factory=lambda: f"write-{time.time_ns()}")
    cancelled: bool = False
    validator: Callable[[], bool] | None = field(default=None, repr=False)


class _WriterThread(QThread):
    """Bounded, priority-aware, non-blocking PTY writer.

    Control traffic keeps reserved capacity and can evict queued background/task
    work. Staleness is checked again immediately before the native write, which
    is the only point that can protect a replacement pane session from delayed
    bytes after a host/PTY stall.
    """

    def __init__(
        self,
        proc,
        parent: QObject | None = None,
        *,
        maxsize: int = PTY_WRITER_QUEUE_MAX,
        control_reserve: int = PTY_WRITER_CONTROL_RESERVE,
        generation_getter=None,
    ) -> None:
        super().__init__(parent)
        self._proc = proc
        self._maxsize = max(4, int(maxsize))
        self._control_reserve = max(1, min(self._maxsize - 1, int(control_reserve)))
        self._generation_getter = generation_getter
        self._queues: dict[WritePriority, deque[PtyWriteMessage]] = {
            priority: deque() for priority in WritePriority
        }
        self._condition = threading.Condition()
        self._stopping = False
        self.queue_full_count = 0
        self.stale_drop_count = 0

    @property
    def queue_depth(self) -> int:
        with self._condition:
            return sum(len(items) for items in self._queues.values())

    def _pop_next(self) -> PtyWriteMessage | None:
        for priority in WritePriority:
            items = self._queues[priority]
            if items:
                return items.popleft()
        return None

    def _evict_for(self, priority: WritePriority) -> bool:
        candidates = (
            (WritePriority.BACKGROUND, WritePriority.TASK)
            if priority == WritePriority.USER
            else (WritePriority.BACKGROUND, WritePriority.TASK, WritePriority.USER)
        )
        for candidate in candidates:
            items = self._queues[candidate]
            if items:
                items.pop()
                return True
        return False

    def run(self) -> None:
        while True:
            with self._condition:
                while not self._stopping and not any(self._queues.values()):
                    self._condition.wait()
                if self._stopping:
                    break
                message = self._pop_next()
            if message is None:
                continue
            if message.cancelled or (
                message.expires_at is not None and time.time() >= message.expires_at
            ):
                self.stale_drop_count += 1
                continue
            if message.validator is not None:
                try:
                    if not message.validator():
                        self.stale_drop_count += 1
                        continue
                except Exception:
                    self.stale_drop_count += 1
                    continue
            if message.session_generation is not None and self._generation_getter is not None:
                try:
                    if int(self._generation_getter()) != int(message.session_generation):
                        self.stale_drop_count += 1
                        continue
                except Exception:
                    self.stale_drop_count += 1
                    continue
            try:
                self._proc.write(message.data)
            except Exception as e:
                print(f"[pty_session] write error: {e!r}", flush=True)

    def write(self, data: str | PtyWriteMessage, **metadata) -> bool:
        message = (
            data if isinstance(data, PtyWriteMessage) else PtyWriteMessage(data=data, **metadata)
        )
        message.priority = WritePriority(message.priority)
        with self._condition:
            if self._stopping:
                return False
            depth = sum(len(items) for items in self._queues.values())
            non_control_cap = self._maxsize - self._control_reserve
            over_limit = depth >= self._maxsize or (
                message.priority != WritePriority.CONTROL and depth >= non_control_cap
            )
            if over_limit:
                can_evict = message.priority in (WritePriority.CONTROL, WritePriority.USER)
                if not can_evict or not self._evict_for(message.priority):
                    self.queue_full_count += 1
                    return False
            self._queues[message.priority].append(message)
            self._condition.notify()
        return True

    def cancel_generation(self, generation: int) -> int:
        removed = 0
        with self._condition:
            for priority, items in self._queues.items():
                kept = deque()
                for message in items:
                    if message.session_generation == int(generation):
                        removed += 1
                    else:
                        kept.append(message)
                self._queues[priority] = kept
        return removed

    def request_stop(self) -> None:
        with self._condition:
            self._stopping = True
            for items in self._queues.values():
                items.clear()
            self._condition.notify_all()


class _ReaderThread(QThread):
    bytesReceived = pyqtSignal(bytes)
    finished_clean = pyqtSignal()
    _MAX_CONSECUTIVE_READ_ERRORS = 25

    def __init__(
        self,
        proc,
        on_data=None,
        parent: QObject | None = None,
        *,
        batch_ms: int = PTY_BATCH_MS,
        batch_bytes: int = PTY_BATCH_BYTES,
    ) -> None:
        super().__init__(parent)
        self._proc = proc
        # Called in THIS reader thread for each chunk — does the heavy pyte
        # parse + transcript write off the Qt main thread so many panes don't
        # serialise on it (see docs/cockpit-freeze-rca-2026-05-29.md).
        self._on_data = on_data
        self._stop = False
        self._batch_sec = max(0.001, batch_ms / 1000.0)
        self._batch_bytes = max(1024, int(batch_bytes))

    def run(self) -> None:
        # pywinpty 3.x semantics: read(size) returns whatever is buffered, but
        # can raise EOFError when the buffer is momentarily empty even though
        # the child process is still alive and just waiting for input. We
        # only treat EOF as termination after isalive() confirms the process
        # has actually exited.
        import time

        consecutive_errors = 0
        pending = bytearray()
        last_flush = time.monotonic()

        def _flush() -> None:
            nonlocal last_flush
            if not pending:
                return
            batch = bytes(pending)
            pending.clear()
            if self._on_data is not None:
                self._on_data(batch)
            self.bytesReceived.emit(batch)
            last_flush = time.monotonic()

        while not self._stop:
            # Snapshot once per iteration: `_teardown_resources` can null
            # `self._proc` from another thread after a bounded `.wait(2000)`
            # join times out (#179) — re-reading `self._proc` after the
            # except below observed AttributeError on the resulting None.
            # A local reference is stable for the rest of this iteration
            # regardless of what `self._proc` becomes concurrently.
            proc = self._proc
            if proc is None:
                break
            try:
                data = proc.read(4096)
            except EOFError:
                if pending and time.monotonic() - last_flush >= self._batch_sec:
                    _flush()
                if not proc.isalive():
                    break
                time.sleep(0.04)
                continue
            except Exception as e:
                print(f"[pty_session] read error: {e!r}", flush=True)
                if pending and time.monotonic() - last_flush >= self._batch_sec:
                    _flush()
                if not proc.isalive():
                    break
                consecutive_errors += 1
                if consecutive_errors >= self._MAX_CONSECUTIVE_READ_ERRORS:
                    break
                time.sleep(0.04)
                continue

            consecutive_errors = 0
            if not data:
                if pending and time.monotonic() - last_flush >= self._batch_sec:
                    _flush()
                if not proc.isalive():
                    break
                time.sleep(0.02)
                continue

            if isinstance(data, str):
                data = data.encode("utf-8", "replace")
            pending.extend(data)
            if (
                len(pending) >= self._batch_bytes
                or time.monotonic() - last_flush >= self._batch_sec
            ):
                _flush()
        _flush()
        self.finished_clean.emit()

    def request_stop(self) -> None:
        self._stop = True


class PtySession(QObject):
    # Raw PTY bytes — consumed by the xterm.js TerminalWidget for rendering.
    bytesIn = pyqtSignal(bytes)
    # pyte screen mutated — still used by state-detection helpers
    # (is_at_trust_prompt, is_at_ready_prompt) and display_lines() export.
    outputUpdated = pyqtSignal()
    processExited = pyqtSignal(int)  # exit code (best effort)

    def __init__(
        self,
        cols: int = 100,
        rows: int = 36,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.cols = cols
        self.rows = rows
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        # Guards every read/write of the pyte screen. stream.feed() now runs in
        # the reader thread while the main thread reads display_lines() /
        # is_at_*_prompt() — without this lock those race.
        self._screen_lock = threading.Lock()
        # Rendering pyte is O(rows * columns). Orchestrator timers call several
        # predicates per pane per tick, so memoize rows by a generation bumped
        # after every successful byte feed. Represent the initial blank screen
        # without walking pyte at all.
        self._output_generation = 0
        self._display_cache_generation = 0
        self._display_lines_cache = tuple(" " * cols for _ in range(rows))
        self._proc = None
        # Root PID of the spawned command (claude.exe), captured at spawn so
        # terminate() can tree-kill descendants even after _proc is torn down.
        self._pid: int | None = None
        self._job_object: JobObjectManager | None = None
        self._reader: _ReaderThread | None = None
        self._writer: _WriterThread | None = None
        self._alive = False
        self._transcript = None  # open file handle; None = not capturing
        # CLAUDE_CONFIG_DIR this session was spawned with, captured from the
        # spawn env (None = default profile / ~/.claude). The token meter reads
        # it so a pane on a non-default user profile finds its session JSONL
        # under <config_dir>/projects/ instead of ~/.claude/projects/.
        self._claude_config_dir: str | None = None
        self._claude_project_dir_name: str | None = None
        # Monotonic timestamp of the last *content-changing* PTY output chunk
        # (#248/#247) — a structural idle/busy signal (independent of TUI text
        # markers, #20): a generating CLI streams visibly-changing output
        # continuously (spinner repaint + token stream), so a long gap since
        # the screen last actually changed means the pane has gone quiet.
        # Stamped only when _content_fingerprint(...) differs from the
        # previous chunk's — a redrawing spinner glyph or a terminal-init
        # escape sequence that renders nothing must NOT advance this (see
        # _feed_and_log). 0.0 = no content seen yet. Written in the reader
        # thread, read on the main thread; a plain float read/write is atomic
        # under the GIL so no lock is needed.
        self._last_output_ts: float = 0.0
        # Raw PTY liveness (#248/#247): stamped on EVERY chunk, unlike
        # _last_output_ts above — this is "the process is still writing to
        # the pty", not "the pane is making progress". Kept separate so a
        # caller that only needs to know the PTY hasn't died isn't fooled by
        # content-fingerprint normalization, and vice versa.
        self._last_byte_ts: float = 0.0
        # Running total of raw PTY bytes read since spawn. Monotonic, never
        # reset. The proactive-compact watchdog compares it against the
        # value it saw when the previous `/compact` settled, so a pane that
        # produced nothing new since is never compacted again (user report
        # 2026-08-25: overnight, the same idle Lead pane got `/compact`
        # every ~25-45 min with nothing to compact).
        self.output_bytes_total: int = 0
        # Fingerprint of the last chunk's normalized screen content — see
        # _content_fingerprint. "" until the first non-blank content arrives.
        self._last_content_fingerprint: str = ""
        # Monotonic timestamp of the first content-changing chunk this
        # session ever produced, or None before that (the CLI process is up
        # but hasn't rendered anything yet — still mid-boot / terminal-init
        # only). Public accessor: first_content_ts().
        self._first_content_ts: float | None = None
        self._output_rate_bps: float = 0.0
        self._output_rate_window_started: float = time.monotonic()
        self._output_rate_window_bytes: int = 0
        # Ready-state computed by the reader thread from the SAME feed that
        # just updated the screen (#106) — see is_at_ready_prompt_cached().
        # Plain bool read/write is atomic under the GIL, same reasoning as
        # _last_output_ts above, so main-thread reads need no lock either.
        self._cached_ready: bool = False
        self.session_generation: int = 0
        self._transcript_last_flush: float = time.monotonic()
        self._transcript_pending_bytes: int = 0

    # ──────────────────────────────────────────────────────────────
    # lifecycle
    # ──────────────────────────────────────────────────────────────
    def spawn(
        self,
        argv: Sequence[str],
        cwd: str | None = None,
        env: dict | None = None,
        transcript_path: str | None = None,
    ) -> None:
        # Remember which Claude config home this pane uses so the token meter
        # can locate its session JSONL (non-default profiles redirect it).
        self._claude_config_dir = (env or {}).get("CLAUDE_CONFIG_DIR")
        self._claude_project_dir_name = (env or {}).get("CLAUDE_CODE_PROJECT_DIR_NAME")

        # Snapshot console windows before spawn so we can hide whatever new
        # console window (cmd.exe / conhost) pywinpty surfaces (Windows only).
        pre_hwnds = snapshot_console_hwnds() if sys.platform == "win32" else set()

        # Cross-platform PTY: pywinpty (ConPTY) on Windows, ptyprocess on
        # macOS/Linux. The backend wrapper normalises read→bytes and accepts
        # str/bytes on write, so the reader/writer threads below stay identical.
        # Bounded (#139): the native constructor call is blocking with no
        # timeout of its own on either backend — spawn_pty_bounded runs it on
        # a worker thread and raises PtySpawnTimeout (a plain Exception,
        # caught by every existing spawn-failure handler) instead of letting
        # a wedged call freeze this call forever.
        self._proc = spawn_pty_bounded(
            argv,
            cwd=cwd,
            env=env,
            rows=self.rows,
            cols=self.cols,
            timeout_sec=PTY_SPAWN_TIMEOUT_SEC,
        )
        self._alive = True
        try:
            self._pid = int(self._proc.pid)
        except Exception:
            self._pid = None
        if self._pid is not None and sys.platform == "win32":
            self._job_object = JobObjectManager()
            if not self._job_object.assign(self._pid):
                self._job_object.close()
                self._job_object = None

        # The console window can take a moment to appear. Retry hiding on a
        # short backoff so we catch it whenever it shows up.
        if sys.platform == "win32":

            def _sweep() -> None:
                new = snapshot_console_hwnds() - pre_hwnds
                if new:
                    hide_hwnds(new)

            for delay in (150, 400, 900, 1800, 3500):
                QTimer.singleShot(delay, _sweep)
            _ensure_console_sweeper()

        if transcript_path is not None:
            try:
                import logging

                self._transcript = open(transcript_path, "wb")
                self._transcript_last_flush = time.monotonic()
                self._transcript_pending_bytes = 0
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "transcript open failed (%s): %r — PTY still running", transcript_path, exc
                )
                self._transcript = None

        self._reader = _ReaderThread(self._proc, on_data=self._feed_and_log, parent=self)
        self._reader.bytesReceived.connect(self._on_bytes)
        self._reader.finished_clean.connect(self._on_exit)
        self._reader.start()

        self._writer = _WriterThread(
            self._proc,
            parent=self,
            generation_getter=lambda: self.session_generation,
        )
        self._writer.start()

    def _feed_and_log(self, data: bytes) -> None:
        """Runs in the reader thread. Does the heavy work off the Qt main
        thread: write the transcript, feed pyte, and classify the ready state
        (all under the screen lock). Best-effort — never raises so a bad
        chunk can't kill the reader."""
        # Raw PTY liveness (#248/#247): every chunk bumps this unconditionally
        # — see _last_byte_ts's docstring. NOT the content-progress signal;
        # that's _last_output_ts, stamped further down only on a real
        # content-fingerprint change.
        now = time.monotonic()
        self._last_byte_ts = now
        # __dict__ idiom like the rate-window fields below: tests build the
        # session via __new__ (no QObject __init__), where attribute access
        # raises RuntimeError.
        self.__dict__["output_bytes_total"] = int(self.__dict__.get("output_bytes_total", 0)) + len(
            data
        )
        window_bytes = int(self.__dict__.get("_output_rate_window_bytes", 0)) + len(data)
        window_started = float(self.__dict__.get("_output_rate_window_started", now))
        self.__dict__["_output_rate_window_bytes"] = window_bytes
        rate_elapsed = now - window_started
        if rate_elapsed >= 1.0:
            self.__dict__["_output_rate_bps"] = window_bytes / rate_elapsed
            self.__dict__["_output_rate_window_bytes"] = 0
            self.__dict__["_output_rate_window_started"] = now
        if self._transcript is not None:
            try:
                self._transcript.write(data)
                pending_bytes = int(self.__dict__.get("_transcript_pending_bytes", 0)) + len(data)
                last_flush = float(self.__dict__.get("_transcript_last_flush", now))
                self._transcript_pending_bytes = pending_bytes
                if (
                    pending_bytes >= TRANSCRIPT_FLUSH_BYTES
                    or now - last_flush >= TRANSCRIPT_FLUSH_MS / 1000.0
                ):
                    self._transcript.flush()
                    self._transcript_last_flush = now
                    self._transcript_pending_bytes = 0
            except Exception:
                # disk full / handle closed — stop trying rather than blocking the PTY
                self._transcript = None
        try:
            with self._screen_lock:
                self.stream.feed(data)
                self._output_generation += 1
                lines = self._display_lines_locked()
                # Classify ready state here, while we already hold the lock
                # feed() just used — the reader thread pays this cost instead
                # of the Qt main thread (#106: agent_pane._sync_idle_flag was
                # calling is_at_ready_prompt() on every outputUpdated, taking
                # this SAME lock and contending with this exact feed()).
                self._cached_ready = _classify_ready(_ready_region(lines))
                # Structural quiescence signal (#248/#247): stamp
                # _last_output_ts only when the rendered screen actually
                # changed — reuses `lines` above instead of re-joining the
                # full screen a second time.
                fingerprint = _content_fingerprint(lines)
                if fingerprint != self._last_content_fingerprint:
                    self._last_content_fingerprint = fingerprint
                    self._last_output_ts = now
                    if fingerprint and self._first_content_ts is None:
                        self._first_content_ts = now
        except Exception:
            # pyte sometimes chokes on partial sequences; skip and continue
            pass

    def _on_bytes(self, data: bytes) -> None:
        # Runs on the Qt main thread (queued from the reader). pyte parsing and
        # the transcript write already happened in _feed_and_log on the reader
        # thread, so here we only forward raw bytes to xterm.js (rendering must
        # touch QWebEngine on the main thread) and notify state-detection
        # consumers that the screen changed.
        self.bytesIn.emit(data)
        self.outputUpdated.emit()

    def _on_exit(self) -> None:
        code = 0
        try:
            if self._proc is not None:
                exit_status = self._proc.exitstatus
                if exit_status is not None:
                    code = int(exit_status)
                else:
                    signal_status = getattr(self._proc, "signalstatus", None)
                    if signal_status is None:
                        signal_status = getattr(
                            getattr(self._proc, "_proc", None), "signalstatus", None
                        )
                    if signal_status is not None:
                        code = -int(signal_status)
        except Exception:
            pass
        self._teardown_resources(kill_process=False, wait=False)
        self.processExited.emit(code)

    def write(
        self,
        data: bytes | str,
        *,
        priority: WritePriority = WritePriority.USER,
        kind: str = "user",
        expires_at: float | None = None,
        session_generation: int | None = None,
        delivery_id: str | None = None,
        validator: Callable[[], bool] | None = None,
    ) -> bool:
        if not self._alive or self._proc is None or self._writer is None:
            return False
        # pywinpty 3.x .write() expects str (it does its own UTF-8 encoding
        # internally). Passing bytes raises TypeError.
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        return self._writer.write(
            data,
            priority=priority,
            kind=kind,
            expires_at=expires_at,
            session_generation=(
                self.session_generation if session_generation is None else session_generation
            ),
            delivery_id=delivery_id,
            validator=validator,
        )

    @property
    def writer_queue_depth(self) -> int:
        writer = self._writer
        return writer.queue_depth if writer is not None else 0

    @property
    def writer_stale_drop_count(self) -> int:
        writer = self._writer
        return writer.stale_drop_count if writer is not None else 0

    @property
    def writer_queue_full_count(self) -> int:
        writer = self._writer
        return writer.queue_full_count if writer is not None else 0

    @property
    def output_rate_bps(self) -> float:
        return float(self.__dict__.get("_output_rate_bps", 0.0))

    @property
    def pid(self) -> int | None:
        """Root PID of the spawned provider CLI process (#364 lever 6's
        `takkub doctor --ram` — the only public accessor for `_pid`, which
        used to be read only inside this class)."""
        return self.__dict__.get("_pid")

    def resize(self, cols: int, rows: int) -> None:
        # (#343) Also used as a "force a real redraw" nudge: called with an
        # unchanged size, setwinsize() would be a no-op the OS/ConPTY layer
        # may not even forward to the child (no genuine size change to
        # report), so Orchestrator._nudge_stale_marker's recovery probe
        # always calls resize(cols+1, rows) then resize(cols, rows) — two
        # REAL size changes bracketing the original — never a bare repeat of
        # the current size.
        if cols < 20 or rows < 5:
            return
        self.cols = cols
        self.rows = rows
        with self._screen_lock:
            self.screen.resize(rows, cols)
            # Resize mutates the visible screen without a byte feed; invalidate
            # so the next reader preserves the existing resize semantics.
            self._display_cache_generation = -1
        if self._alive and self._proc is not None:
            try:
                self._proc.setwinsize(rows, cols)
            except Exception:
                pass

    def terminate(self, wait: bool = False) -> None:
        """Tear the session down: kill the process tree, stop the reader/writer
        threads, close the transcript.

        The heavy part — ``taskkill /T`` (which can take seconds on a big leaked
        tree) plus the thread joins — runs on a background daemon thread by
        default so the Qt main thread (orchestrator.close → terminate) never
        freezes. Pass ``wait=True`` on application shutdown so the teardown
        finishes inline before the process exits, otherwise the detached daemon
        thread would be killed mid-``taskkill`` and orphan the tree on exit.

        PyQt6 raises RuntimeError (not AttributeError) for attribute access on a
        QObject built via __new__ without __init__ (some test fixtures), so every
        attribute read is guarded — terminate() is always safe to call.
        """
        self._teardown_resources(kill_process=True, wait=wait)

    def _teardown_resources(self, *, kill_process: bool, wait: bool) -> None:
        """Detach and release this session's process, threads and transcript.

        Natural child exit and explicit termination share this path so neither
        can leave a writer blocked on its queue or a native handle retained.
        """
        try:
            _writer = self._writer
        except (AttributeError, RuntimeError):
            _writer = None
        try:
            _reader = self._reader
        except (AttributeError, RuntimeError):
            _reader = None
        if _writer is not None:
            _writer.request_stop()  # enqueue sentinel → writer loop exits
        if _reader is not None:
            _reader.request_stop()  # set stop flag
        try:
            _pid = self._pid
        except (AttributeError, RuntimeError):
            _pid = None
        try:
            _proc = self._proc
        except (AttributeError, RuntimeError):
            _proc = None
        try:
            _job_object = self._job_object
        except (AttributeError, RuntimeError):
            _job_object = None
        try:
            _transcript = self._transcript
        except (AttributeError, RuntimeError):
            _transcript = None
        # On natural exit the reader loop has already finished, so closing the
        # transcript synchronously is race-free and releases the fd before the
        # processExited/respawn chain runs. Explicit terminate closes it after
        # joining the still-active reader below.
        if not kill_process and _transcript is not None:
            try:
                _transcript.close()
            except Exception:
                pass
            _transcript = None
        try:
            self._alive = False
        except (AttributeError, RuntimeError):
            pass
        try:
            self._transcript = None
        except (AttributeError, RuntimeError):
            pass
        for attr in ("_writer", "_reader", "_proc", "_pid", "_job_object"):
            try:
                setattr(self, attr, None)
            except (AttributeError, RuntimeError):
                pass

        def _teardown() -> None:
            # ORDER MATTERS. `taskkill /T` walks the live parent→child tree, so
            # it must finish while the root is still alive; pywinpty's
            # terminate(force=True) only reaps claude.exe itself and would orphan
            # the node dev-server subtree if it ran first. Running both here, in
            # sequence, preserves that ordering off the Qt main thread.
            if kill_process:
                # Native ownership first. Closing a KILL_ON_JOB_CLOSE job is
                # deterministic; PID-scoped tree-kill below remains the fallback.
                if _job_object is not None:
                    _job_object.close()
                _tree_kill(_pid)
            elif _job_object is not None:
                # Root exited naturally; closing the job reaps any descendants
                # it left behind and releases the native handle.
                _job_object.close()
            if kill_process and _proc is not None:
                try:
                    _proc.terminate(force=True)  # unblocks reader's proc.read()
                except Exception:
                    pass
            # Join the reader/writer so they don't accumulate across many
            # close/respawn cycles. Bounded so a wedged thread can't hang exit.
            # Both are QThread children of this session, so app shutdown can
            # delete their C++ side out from under this background thread
            # between the parent-chain teardown and this call (#316) — quit()
            # / wait() on an already-deleted wrapper raises RuntimeError, and
            # since the C++ thread object is gone there's nothing left to stop.
            if _writer is not None:
                try:
                    _writer.quit()
                    _writer.wait(2000)
                except RuntimeError:
                    pass
            if _reader is not None:
                try:
                    _reader.quit()
                    _reader.wait(2000)
                except RuntimeError:
                    pass
            # Thread QObjects are children of the session.  Once their loops
            # stop, release their last references to the native PTY object.
            for thread_obj in (_writer, _reader):
                if thread_obj is not None:
                    try:
                        thread_obj._proc = None
                    except Exception:
                        pass
            if _transcript is not None:
                try:
                    _transcript.close()
                except Exception:
                    pass

        if wait:
            _teardown()
        else:
            threading.Thread(target=_teardown, name="pty-teardown", daemon=True).start()

    @property
    def is_alive(self) -> bool:
        return self._alive

    # ──────────────────────────────────────────────────────────────
    # screen access
    # ──────────────────────────────────────────────────────────────
    def _display_lines_locked(self) -> list[str]:
        """Return memoized rows; caller must hold ``_screen_lock``."""
        generation = self._output_generation
        if self._display_cache_generation != generation:
            self._display_lines_cache = tuple(_safe_screen_display(self.screen))
            self._display_cache_generation = generation
        return list(self._display_lines_cache)

    def display_lines(self) -> list[str]:
        """Return the visible screen as a list of rows (top → bottom)."""
        with self._screen_lock:
            return self._display_lines_locked()

    def cursor(self) -> tuple[int, int]:
        with self._screen_lock:
            c = self.screen.cursor
            return c.x, c.y

    def display_rich(self) -> list[list[tuple[str, str, str, bool, bool, bool, bool]]]:
        """Return rows as lists of style-runs.

        Each run is (text, fg, bg, bold, italic, underline, reverse). fg/bg
        are pyte color strings ('default', a named colour like 'red', or a
        6-char hex). Adjacent cells with identical attrs are merged into one
        run, keeping the row's total run count low for fast rendering.
        """
        rows: list[list[tuple[str, str, str, bool, bool, bool, bool]]] = []
        with self._screen_lock:
            for y in range(self.screen.lines):
                line = self.screen.buffer[y]
                runs: list[tuple[str, str, str, bool, bool, bool, bool]] = []
                cur_text = ""
                cur_key: tuple = ()
                for x in range(self.screen.columns):
                    cell = line[x]
                    key = (
                        cell.fg,
                        cell.bg,
                        cell.bold,
                        cell.italics,
                        cell.underscore,
                        cell.reverse,
                    )
                    if key != cur_key:
                        if cur_text:
                            runs.append((cur_text, *cur_key))
                        cur_text = cell.data
                        cur_key = key
                    else:
                        cur_text += cell.data
                if cur_text:
                    runs.append((cur_text, *cur_key))
                rows.append(runs)
        return rows

    # ──────────────────────────────────────────────────────────────
    # state detection: helps orchestrator auto-trust / wait-for-ready
    # ──────────────────────────────────────────────────────────────
    def shows_startup_marker(self) -> bool:
        """True when the pane is in a provider *startup / message-queue* phase
        rather than actively running a task turn.

        Scoped to the same bottom footer/status rows as the ready markers
        (`_ready_region`) — NOT the whole screen. A boot line that has scrolled
        up into the visible conversation body must not keep reading as
        "startup" after the pane moved on, or the idle watchdog would reset its
        streak on every tick and a pane that finished-but-forgot
        `takkub done` would never be reminded (the exact regression this
        suppression exists to avoid).

        codex/agy cold-boot their MCP servers and, if a message arrives during
        that window, QUEUE it ("tab to queue message") instead of processing it
        immediately. Between boot phases the composer status bar can read idle
        ("Fast off") with no "esc to interrupt" — so `is_at_ready_prompt()`
        returns True and the forgot-`takkub done` watchdog mistakes a booting/
        queued pane for one that finished-but-forgot, stacking auto-reminders
        into the composer (diagnosed 2026-07-21 from a wash-locker codex
        transcript: task queued through MCP boot, ran to completion, called
        `takkub done` — the reminders were pure boot-window noise). The idle
        watchdog uses this to tell a genuine work turn ("Working…") apart from
        a boot/queue phase, so it only arms after the task actually started.

        #281: for "is this pane still BOOTING", use `shows_boot_phase_marker`
        instead — this one is deliberately the wider union and answers a
        different question."""
        return any(m in _ready_region(self.display_lines()) for m in _STARTUP_MARKERS)

    def shows_boot_phase_marker(self, *, rows: int = _READY_TAIL_ROWS) -> bool:
        """True only while the provider CLI is still starting up — its
        composer does not exist yet, so anything written to the pane lands on
        a splash screen instead of an input box.

        Narrower than `shows_startup_marker` on purpose (#281): that one also
        reports True for a pane that is simply mid-turn with a queued message
        ("tab to queue message"), which codex shows whenever it is working.
        Callers deciding "is delivery stuck at boot / may I paste yet" must
        use this one — treating a working pane as a stuck boot is what
        produced `[delivery-boot-stall]` warnings for panes that went on to
        finish their tasks normally.

        *rows* widens the scanned window (#284). The default matches the ready
        window, which keeps a boot line that has scrolled up into the
        conversation body from reading as "still booting" forever. Delivery
        passes `_BOOT_MARKER_TAIL_ROWS` instead, because there the tight window
        is the bug: codex's bordered composer pushes its own live boot line out
        of 6 rows, leaving a READY verdict for a pane that has not started. The
        two errors are not symmetric — a false "still booting" costs a bounded
        wait that the existing delivery timeout already backstops, while a
        false "ready" pastes the task into a pane that cannot receive it.
        """
        return _has_boot_phase_marker(_tail_region(self.display_lines(), rows))

    def boot_phase_detail(self) -> str:
        """The actual boot line on screen, e.g. ``Starting MCP servers (0/3):
        codex_apps, context7, figma (12s • esc to interrupt)`` — or "" when the
        pane is not in a boot phase.

        #281: Lead used to be told only the word "booting" while the pane
        itself was displaying exactly which servers it was waiting on. That
        line is the one piece of information that makes a stuck boot
        actionable (it names the server to remove or pin), and the obvious
        alternative — `codex mcp list` — cannot see cockpit-injected MCPs at
        all. Trimmed to a single line and bounded so a redraw artifact can
        never paste a screenful into a notice.
        """
        for line in reversed(_boot_marker_region(self.display_lines()).splitlines()):
            if _has_boot_phase_marker(line):
                return " ".join(line.split())[:200]
        return ""

    def is_at_trust_prompt(self) -> bool:
        """True when claude, codex, OR agy is showing a trust-directory modal.

        All three default-select "Yes/trust" so a single Enter keypress
        accepts. Patterns:
          - claude: "Yes, I trust this folder" + "Enter to confirm"
          - codex:  "Do you trust the contents of this directory"
                    + "Press enter to continue"
          - agy:    "Yes, I trust this folder" + "up/down Navigate . enter
                    Confirm" — same "trust this folder" phrase as claude but
                    NO "to" before "Confirm" (live-captured screen text,
                    issue #186). The old exact-substring "enter to confirm"
                    match silently never fired for this wording, so
                    _auto_trust's poller never pressed Enter and a worktree
                    spawn sat on the modal until someone noticed and sent a
                    bare Enter by hand. _ENTER_CONFIRM_RE tolerates the
                    missing "to" instead of requiring the exact phrase.
        """
        text = "\n".join(self.display_lines()).lower()
        if "trust this folder" in text and _ENTER_CONFIRM_RE.search(text):
            return True
        if "do you trust the contents of this directory" in text:
            return True
        return False

    def is_at_claude_empty_composer(self) -> bool:
        """(#343) Structural claude-only fallback: bordered input box with a
        bare, empty prompt — see the module note by _is_claude_empty_composer.
        NOT part of is_at_ready_prompt()/the marker table — callers must know
        the pane's provider is claude before trusting this (wired only in
        Orchestrator._nudge_stale_marker / ._resolve_stale_marker_nudge)."""
        return _is_claude_empty_composer(self.display_lines())

    def is_at_ready_prompt(self) -> bool:
        """True when the underlying TUI is idle at its main input prompt.

        Handles claude, codex, and gemini(agy) panes:
          - claude: bottom hint 'bypass permissions' or 'shift+tab to cycle',
                    never 'esc to interrupt' (working) or trust modal.
          - codex:  composer status bar 'fast off'/'fast on' visible (the
                    startup banner alone does NOT count — #99, it renders
                    before codex finishes booting its MCP servers), no modal
                    (`update available!`, `do you trust`, `press enter
                    to continue`) and no active interrupt indicator.
          - gemini: now the Antigravity `agy` TUI — idle footer
                    '? for shortcuts' visible, no active 'esc to
                    interrupt/cancel' indicator. (The legacy Gemini CLI
                    marker 'type your message or @path' is kept for
                    backward-compat.) Without a matching idle marker the
                    watchdog never fires (root cause of 'gemini forgot
                    takkub done' incidents 2026-05-20).
        """
        # Detection markers + their precedence live in the central table
        # (_READY_HARD_BLOCKERS / _READY_RULES) so an upstream reword is a
        # one-line patch and `takkub doctor` can self-test them. See M4#17.
        # Scoped to the bottom footer/status region (_ready_region) so a marker
        # string quoted in the conversation body can't poison the verdict — the
        # #70 false-busy stall / #20 fragility root fix.
        return _classify_ready(_ready_region(self.display_lines()))

    def is_at_ready_prompt_cached(self) -> bool:
        """Lock-free read of the ready state the reader thread already
        computed for the current screen (#106).

        `is_at_ready_prompt()` takes `_screen_lock` to read the live pyte
        screen — correct when a caller needs the freshest possible verdict
        (e.g. the self-healing submit verify loop), but a UI poll that just
        wants "is this pane idle right now" doesn't need that freshness, and
        taking the lock on the Qt main thread contends with the reader
        thread's own `stream.feed()` under the SAME lock. This reads the
        value `_feed_and_log` classified while it already held the lock for
        the feed — staleness is bounded by one PTY read chunk (during active
        output, several times a second; while idle, the last computed value
        holds, which is what "idle" means). NOT a substitute for
        `is_at_ready_prompt()` anywhere correctness depends on the freshest
        read."""
        return self._cached_ready

    def shows_pending_input(self, fragment: str = "") -> bool:
        """True when the bottom input region holds unsent content.

        After a bracketed paste claude renders a ``[Pasted text +N lines]``
        placeholder (or, for short inline content, the literal text) in its
        input box. Detecting this lets the delivery self-heal tell a swallowed
        *Enter* (content present but not submitted — #22) apart from a swallowed
        *paste* (input box empty — #26): the first needs a CR resend, the second
        needs the payload re-pasted (a CR resend can't recover a missing paste).
        Scoped to the same bottom footer/input region as is_at_ready_prompt() so
        conversation-body text quoting the content can't poison the verdict. (#79)
        """
        return _input_has_content(_ready_region(self.display_lines()), fragment)

    def is_at_update_splash(self) -> bool:
        """True when a codex 'update available!' startup splash is blocking the prompt.

        The codex splash modal ('update available! run npm i -g @openai/codex')
        prevents the CLI from reaching its ready state.  Distinguished from the
        passive Gemini update footer ('gemini cli update available!') which is
        already classified ready=True by _READY_RULES and must NOT match here.

        Caller note: this is only meaningful when is_at_ready_prompt() is False;
        pairing both ensures the splash, not some other block, is the cause. (#62)

        Scoped to the bottom region (_ready_region) for the same reason as
        is_at_ready_prompt: a conversation that merely mentions "update
        available!" must not read as a live splash. (#70/#20)
        """
        text = _ready_region(self.display_lines())
        return "update available!" in text and "gemini cli update available!" not in text

    def is_at_limit_choice_modal(self) -> bool:
        """True when claude's usage/session-limit chooser is blocking the pane.

        Claude Code v2.1.198 pairs the limit banner with an interactive modal
        ("What do you want to do?" · "1. Stop and wait for limit to reset" ·
        "2./3. Upgrade …" · "Enter to confirm · Esc to cancel"). Option 1 is
        preselected, so a single Enter confirms stop-and-wait — the pane then
        idles out the window and auto-resumes at reset instead of blocking on
        the modal forever (field-verified screenshot 2026-07-02).

        Whole-screen scan is safe here: the option string is imperative UI
        chrome ("stop and wait for limit to reset") that conversation text has
        no reason to quote verbatim, and the caller additionally gates on
        rate_limit_reset_at() having detected a live banner."""
        text = "\n".join(self.display_lines()).lower()
        return "stop and wait for limit to reset" in text

    def seconds_since_output(self) -> float:
        """Monotonic seconds since the PTY last produced output — a structural
        idle/busy signal that does NOT depend on TUI text wording (#20).

        A generating CLI streams output continuously (animated spinner + token
        stream), so a large value means the pane has gone quiet. Used to
        corroborate text-marker detection and, in the orchestrator, to flag a
        pane that is quiet-but-unrecognised (the signature of an upstream prompt
        reword that silently broke the markers). Returns ``inf`` before any
        output has been seen."""
        ts = self._last_output_ts
        if not ts:
            return float("inf")
        return max(0.0, time.monotonic() - ts)

    def last_output_monotonic(self) -> float:
        """Monotonic timestamp of the last PTY output, or 0.0 if none yet.

        Unlike ``seconds_since_output`` (which says *how long ago*), this exposes
        the raw timestamp so a caller can capture a baseline and later test
        whether output arrived *after* it — i.e. did the TUI react to something
        we wrote. The delivery self-heal uses this to tell a paste that LANDED
        (claude rendered a placeholder / streamed a reply → timestamp advanced)
        from one that was SWALLOWED (#26 — bytes dropped, pane stayed silent →
        timestamp unchanged), which a same-clock comparison decides reliably."""
        return self._last_output_ts or 0.0

    def seconds_since_byte(self) -> float:
        """Monotonic seconds since the PTY last delivered ANY byte chunk,
        including ones that rendered no visible content change (a terminal-
        init escape sequence, a redrawing spinner glyph — see
        seconds_since_output() for the content-filtered version of this).

        This is the raw "is the process still writing to the pty" signal
        (#248/#247) — for a caller that wants to distinguish a dead/wedged
        PTY from one that's alive but visibly stuck, not for progress
        detection (use seconds_since_output() for that). Returns ``inf``
        before any byte has been seen."""
        ts = self._last_byte_ts
        if not ts:
            return float("inf")
        return max(0.0, time.monotonic() - ts)

    def first_content_ts(self) -> float | None:
        """Monotonic timestamp of the first content-changing PTY output this
        session ever produced, or ``None`` before that.

        ``None`` means the CLI process is spawned and the PTY is open, but
        nothing has rendered on screen yet — still mid-boot, or so far only
        terminal-init escape sequences (cursor-position report, mouse-mode
        enables) have been fed, which is exactly the state a hung/wedged
        spawn is indistinguishable from without this (#248/#247). Orchestrator
        status reporting (`takkub list`/`status`) uses this to tell
        "spawning" apart from "active"/"ready"."""
        return self._first_content_ts

    def rate_limit_reset_at(self, provider: str = "claude") -> float | None:
        """If the pane is showing `provider`'s quota/usage-limit banner,
        return the epoch the limit resets at; else None.

        Used by the orchestrator's idle watchdog to tell "rate-limited, can't
        work until reset" apart from "idle, forgot takkub done" so it suppresses
        the reminder loop and notifies at reset time instead. See the marker
        notes at module top — detection wording needs real-banner verification.

        `provider` defaults to "claude" for back-compat with existing call
        sites; #301 generalized detection to every provider via
        `_resolve_quota_markers(provider)` — gemini/agy's field-verified
        "individual quota reached" (duration-style reset) and codex's
        provisional reached-state phrasing are checked the same way.
        """
        text = "\n".join(self.display_lines()).lower()
        return _parse_rate_limit_reset(text, time.time(), _resolve_quota_markers(provider))

    def quota_stall_marker(self, provider: str = "claude") -> str | None:
        """The matched quota/usage-limit phrase currently shown for
        `provider`, or None (#301). Companion to `rate_limit_reset_at()` —
        same detection, exposes WHICH phrase matched so a caller can quote it
        in the Lead-facing notice instead of a bare "quota hit"."""
        text = "\n".join(self.display_lines()).lower()
        for marker in _resolve_quota_markers(provider):
            if marker in text:
                return marker
        return None

    def current_model_label(self, provider: str) -> str | None:
        """Best-effort model name currently shown on this pane's screen, or
        None (#301). Exists so `takkub status`/`list` can surface a silent
        model downgrade — real incident: a gemini/agy pane hit its quota,
        auto-downgraded Pro→Flash, and kept working with Lead never told
        which model actually produced the result.

        Only gemini is calibrated (its footer prints e.g. "Gemini 3.1 Pro ·
        high" / "Gemini 3.5 Flash (Medium)" — confirmed wording, see
        `_ready_rules` fixtures). Every other provider returns None until a
        real screen is captured — never guess a regex from docs alone, same
        policy as every other marker table in this module."""
        if provider != "gemini":
            return None
        text = "\n".join(self.display_lines())
        m = _GEMINI_MODEL_RE.search(text)
        return m.group(1) if m else None

    def auth_failure_reason(self, provider: str) -> str | None:
        """Return the matched marker if this pane's screen currently shows
        `provider`'s CLI stuck on an auth failure, else ``None`` (#248/#247
        round 2).

        Two tiers, both scoped to `_ready_region` (bottom footer rows) like
        every other prompt-state check in this class — conversation body
        text merely quoting one of these phrases must not poison the
        verdict, same reasoning as `_classify_ready`:

          1. `provider_spec.auth_error_markers_for(provider)` — an
             unambiguous failure phrase (provider-specific confirmed list +
             the generic cross-provider baseline). Fires instantly, no
             grace period — this is meant to beat
             ``orchestrator.BUSY_WAIT_CEILING_SEC`` (1800s) by a wide
             margin, not tune it.
          2. `provider_spec.auth_transient_markers_for(provider)` — a
             normally-transient boot-time marker (e.g. gemini/agy's
             "Signing in..."). Only counts once the screen has been static
             for ``AUTH_TRANSIENT_GRACE_SEC`` — measured via
             ``seconds_since_output()``, the same spinner-normalized
             content-change clock used everywhere else in this module — so
             an animating spinner next to the same text still reads as a
             normal cold boot, not a failure.
        """
        text = _ready_region(self.display_lines())
        for marker in auth_error_markers_for(provider):
            if marker in text:
                return marker
        transient = auth_transient_markers_for(provider)
        if transient and self.seconds_since_output() >= AUTH_TRANSIENT_GRACE_SEC:
            for marker in transient:
                if marker in text:
                    return marker
        return None

    def account_pending_reason(self, provider: str) -> str | None:
        """Return the matched marker if this pane's screen currently shows
        `provider`'s CLI stuck on its own account/eligibility gate (#346),
        else ``None``.

        Distinct from ``auth_failure_reason()``: that method answers "is
        this provider not authenticated" (a login/credentials problem,
        fixable by signing in again). This answers a different question —
        "is the provider's OWN backend still deciding whether this account
        is allowed to proceed" — where re-authenticating fixes nothing and
        the only real remedies are wait-and-retry or switching provider.

        Same grace-gating as ``auth_failure_reason()``'s transient tier:
        only counts once the screen has been static for
        ``AUTH_TRANSIENT_GRACE_SEC`` (``seconds_since_output()``, the
        spinner-normalized clock), so an animating spinner next to this text
        still reads as a normal cold boot, not a stuck gate.

        #363 regression fix: scans `_BOOT_MARKER_TAIL_ROWS` (20 rows), NOT
        the tight `_ready_region` (6 rows) the original #346 fix used. Same
        bug shape as #284's boot-phase probe: gemini/agy's real account-
        pending banner is 3 lines tall, and a realistic composer footer below
        it (border + status/context row + the idle hint row) pushes those 3
        lines out of a 6-row window while leaving just enough footer chrome
        ("? for shortcuts") inside it for `is_at_ready_prompt()` to
        misclassify the frozen pane as READY — proven against this exact
        banner text plus one extra footer row (see
        `tests/test_auth_failure_detection.py`). The wider window is safe for
        the same reason #284's is: this banner is provider chrome ("Verifying
        your account...", "...eligibility...") that ordinary conversation
        text has no reason to quote, so it can't poison the verdict the way
        widening the READY window itself would."""
        markers = account_pending_markers_for(provider)
        if not markers or self.seconds_since_output() < AUTH_TRANSIENT_GRACE_SEC:
            return None
        text = _tail_region(self.display_lines(), _BOOT_MARKER_TAIL_ROWS)
        for marker in markers:
            if marker in text:
                return marker
        return None

    def shows_account_pending_marker(self, provider: str) -> bool:
        """True the instant `provider`'s own account/eligibility-gate banner
        is on screen — UNGATED, unlike ``account_pending_reason()`` (#376).

        ``account_pending_reason()`` exists to answer "has this pane been
        stuck long enough to escalate to Lead", which is deliberately gated
        on ``AUTH_TRANSIENT_GRACE_SEC`` (45s) so a normal cold-boot flash of
        the same text doesn't convict a pane about to clear it on its own —
        and delivery's own `_AUTH_FAILURE_CONFIRM_POLLS` streak on top of
        that. Delivery's "may I paste a task into this pane RIGHT NOW"
        question is different: a banner that appeared this very frame is
        exactly as unsafe to paste over as one that has been up for 45s. The
        #376 incident was a task pasted ~7s after spawn, straight onto a
        fresh banner — well before either gate ever got a chance to run,
        because both of them answer the escalate question, not this one.

        Same `_BOOT_MARKER_TAIL_ROWS` (20-row) window as
        ``account_pending_reason()``, for the same #363 reason: the real
        banner plus realistic composer footer chrome does not fit inside the
        tight `_ready_region` (6 rows) that `is_at_ready_prompt()` scans, so
        a pane frozen on the banner still reads READY there.

        Empty marker tuple (a provider with none confirmed) → always False —
        a no-op for that provider, same as every other marker-table lookup
        in this module."""
        markers = account_pending_markers_for(provider)
        if not markers:
            return False
        text = _tail_region(self.display_lines(), _BOOT_MARKER_TAIL_ROWS)
        return any(marker in text for marker in markers)

    def is_hard_blocked_for(self, provider: str) -> bool:
        """True when `provider`'s own `ready_hard_blockers` currently match
        this pane's footer region — i.e. the screen shows an active
        interrupt/generation indicator ("esc to interrupt", ...) — REGARDLESS
        of whether the orchestrator ever dispatched a task to this pane (#263).

        Exists because `is_at_ready_prompt()`/`is_at_ready_prompt_cached()`
        collapse "hard-blocked (genuinely busy)" and "no ready marker matched
        yet (ambiguous)" into the same single `False`, which is exactly what
        let a codex pane read as bare "active" in `takkub list` while its
        screen plainly showed "Working (0s - esc to interrupt)" — the
        orchestrator's own declared-idle label and the screen's own busy
        chrome silently disagreed with nothing surfacing the difference.
        `Orchestrator._derive_display_state` uses this specifically to split
        that "active" bucket into a "busy" label.

        Deliberately duplicates (rather than refactors) the hard-blocker loop
        `_classify_ready`/`_classify_ready_for_provider` already run, instead
        of extracting a shared helper both would then depend on — those two
        are covered by `ready_marker_selftest()`'s shipped-table self-test and
        the task instructions for this change explicitly call out not to risk
        touching that self-tested precedence chain for an unrelated feature.
        Same `_ready_region` scoping as `_classify_ready_for_provider`, so
        the two can never disagree about what counts as hard-blocked for a
        given provider. (The "verifying your account" / "please try again
        shortly" carve-out this docstring used to mention was removed in
        #346 — see the matching note in `_classify_ready`.)"""
        spec = PROVIDER_REGISTRY.get(provider)
        if spec is None:
            return False
        text = _ready_region(self.display_lines())
        for b in spec.ready_hard_blockers:
            if b in text:
                return True
        return False

    def tool_running_marker(self, provider: str) -> str | None:
        """Return the matched marker if this pane's screen currently shows
        `provider`'s CLI actively running a shell/tool call (#308), else
        ``None``.

        Same `_ready_region` scoping as every other prompt-state check in
        this class, but deliberately checked INDEPENDENTLY of
        `is_at_ready_prompt()`/`is_hard_blocked_for()`: the #308 incident
        showed a provider's idle footer ("? for shortcuts") can stay visible
        on screen below a tool-running status line the entire time the tool
        call is hung, so both of those already-classify-ready methods keep
        reading the pane as idle/normal while it is genuinely wedged. This
        is an instantaneous check only — the caller
        (`orchestrator._check_stuck_tool_panes`) is the one that pairs it
        with `seconds_since_output()` to decide "stuck", not "merely running
        a tool right now"."""
        text = _ready_region(self.display_lines())
        for marker in tool_running_markers_for(provider):
            if marker in text:
                return marker
        return None

    def is_blocked_on_tty_prompt(self) -> str | None:
        """Return the first matching line if the pane is stuck on an interactive
        shell prompt (y/N, credential, 'press any key'); else ``None``.

        Scans the ``_TTY_PROMPT_TAIL_ROWS`` rows ending at the current cursor
        row.  Using the cursor position (rather than a fixed bottom-of-screen
        slice) means:
        - In a fresh/short session the cursor is near the top (row 0–2) and the
          scan window follows it there, so short test screens work correctly.
        - Older content that scrolled above the cursor window does NOT trigger
          false-positives even if it happened to contain one of the patterns
          (e.g. command output that printed 'Are you sure' in its own text).

        The screen and cursor are sampled under a single lock acquisition so
        the two reads are consistent.  The returned string is the stripped
        content of the matched line, suitable for watchdog log messages.
        """
        with self._screen_lock:
            lines = self._display_lines_locked()
            cursor_row = self.screen.cursor.y
        if not lines:
            return None
        lo = max(0, cursor_row - _TTY_PROMPT_TAIL_ROWS + 1)
        hi = cursor_row + 1
        for line in reversed(lines[lo:hi]):
            if _TTY_PROMPT_RE.search(line):
                return line.strip() or "interactive prompt detected"
        return None

    def is_blocked_on_permission_prompt(self) -> str | None:
        """Return the matched "1. Yes" option line if the pane is sitting on
        Claude Code's own tool-permission approval dialog; else ``None``.

        Distinct from ``is_blocked_on_tty_prompt()``: this is the CLI's own
        in-app modal chrome, not a subprocess prompt, and its numbered-menu
        rendering has no ``[y/N]`` bracket for that detector's regex to catch
        (#236). Confirmed only for Claude Code's own wording — codex/gemini-
        agy/opencode/kimi/cursor may render tool-approval prompts differently
        (or not gate on one at all); this is a known per-provider gap, not
        assumed coverage (#103).

        Uses a wider window than ``_TTY_PROMPT_TAIL_ROWS`` (``
        _PERMISSION_MENU_TAIL_ROWS``) since the option-1 line and its
        confirming "No"/"Esc to cancel" companion render on separate rows
        within the dialog box, not the same line.
        """
        with self._screen_lock:
            lines = self._display_lines_locked()
            cursor_row = self.screen.cursor.y
        if not lines:
            return None
        lo = max(0, cursor_row - _PERMISSION_MENU_TAIL_ROWS + 1)
        hi = cursor_row + 1
        window = lines[lo:hi]
        joined = "\n".join(window)
        if not (
            _PERMISSION_MENU_OPTION1_RE.search(joined)
            and _PERMISSION_MENU_CONFIRM_RE.search(joined)
        ):
            return None
        for line in window:
            if _PERMISSION_MENU_OPTION1_RE.search(line):
                return line.strip() or "permission prompt detected"
        return "permission prompt detected"

    def has_unparsed_tool_call(self) -> str | None:
        """Return the first line containing a literal tool-call XML tag if one is
        visible near the cursor; else ``None``.

        A well-formed tool call is consumed by the Claude Code harness before it
        ever reaches the terminal renderer, so it never appears as plain text on
        screen.  If one of the recognised tag patterns IS visible, the tool call
        was malformed (missing ``antml:`` prefix or otherwise unparseable) and was
        silently no-op'd by the harness — the model appears to hang even though
        no real hang occurred.

        Scans the ``_MALFORMED_XML_TAIL_ROWS`` rows ending at the current cursor
        row (same cursor-relative window used by ``is_blocked_on_tty_prompt``).
        The screen and cursor are sampled under a single lock acquisition.  The
        returned string is the stripped content of the first matched line,
        suitable for watchdog log messages.
        """
        with self._screen_lock:
            lines = self._display_lines_locked()
            cursor_row = self.screen.cursor.y
        if not lines:
            return None
        lo = max(0, cursor_row - _MALFORMED_XML_TAIL_ROWS + 1)
        hi = cursor_row + 1
        for line in reversed(lines[lo:hi]):
            if _MALFORMED_XML_RE.search(line):
                return line.strip() or "unparsed tool-call XML detected"
        return None
