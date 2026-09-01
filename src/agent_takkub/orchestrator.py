"""Orchestrator: owns all AgentPanes, exposes high-level operations.

Public API (called by main_window UI and cli_server JSON requests):

  spawn(role, cwd=None)          -> bool, message
  assign(role, cwd, task)        -> bool, message
  send(to_role, msg, from_role)  -> bool, message
  close(role)                    -> bool, message
  done(from_role, note)          -> bool, message
  list_status()                  -> dict[role, state]
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import pathlib
import re
import secrets
import sys
import threading
import time
import uuid as _uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtSignal

from .agent_pane import AgentPane
from .claude_auth_config import apply_claude_auth_overrides
from .config import (
    DATA_HOME,
    EVENTS_LOG,
    REPO_ROOT,  # re-exported: tests patch agent_takkub.orchestrator.REPO_ROOT
    RUNTIME_DIR,
    _write_json_atomic,
    active_project,
    agent_role_dir,
    default_cwd_for_role,
    ensure_runtime,
    find_claude_executable,
    lead_cwd,
    validate_name,
)
from .headless_pane import HeadlessPane
from .lead_context import (  # re-exported for test imports
    _LEAD_GUARD_ALLOW_TOOLS,
    _LEAD_GUARD_WRITE_TOOLS,
    BIG_FILE_GUARD,
    STALE_FILE_GUARD,
    _allowed_project_roots,
    _default_plugin_dirs,
    _recent_session_brief,
    _render_lead_context,
    render_lead_settings,
)
from .lead_draft_state import LeadDraftState  # re-exported for test imports
from .lead_inbox import (  # re-exported for test/compat imports; mixin provides methods
    _BLOCKING_NOTICE_MARKERS,
    _SUBMIT_MAX_RESENDS,
    _SUBMIT_VERIFY_GRACE_MS,
    LEAD_NOTIFY_BUSY_CAP,
    LeadInboxMixin,
    _delayed_enter,
    _delayed_enter_verified,
    _is_blocking_lead_notice,
    _notice_role_tag,
    _prompt_block_reason,
    _safe_session_write,
    _system_marker_role,
    _unwrap_notice_item,
)
from .lead_wait import LeadWaitMixin  # mixin providing takkub-wait methods (#242)
from .limit_autoresume import AutoResumeMixin  # mixin providing auto-resume methods
from .orchestrator_text import (  # re-exported for test/app/main_window imports
    _CODEX_TASK_NOTICE,
    _DEFAULT_TEAMMATE_TIER,
    _EVENTS_LOG_MAX_BYTES,
    _HARVEST_EXCLUDE_DIRS,
    _HOT_MD_INTERVAL_MS,
    _PASTE_END,
    _PASTE_ENTER_DELAY_MS,
    _PASTE_MAX_ENTER_DELAY_MS,
    _PASTE_START,
    _ROLE_MODEL_TIERS,
    _TYPING_ENTER_DELAY_MS,
    BRACKETED_PASTE_THRESHOLD,
    TASK_HANDOFF_THRESHOLD,
    _append_verify_fail_hint,
    _append_worktree_hint,
    _build_transcript_path,
    _cwd_within_project,
    _describe_valid_project_cwds,
    _enter_delay_ms,
    _exit_key,
    _lead_model_override,
    _log_event,
    _looks_like_source_reference,
    _notice_fingerprint,
    _paste_payload,
    _project_root_dir,
    _read_tail_bytes,
    _render_daily_digest,
    _render_hot_md,
    _resolve_project_memory,
    _rewrite_task_for_codex,
    _sanitize_pane_text,
    _task_handoff_dir,
    _task_handoff_pointer,
    _teammate_tier,
    _truncate_at_word_boundary,
    classify_stuck_reason,
    cwd_validation_error,
    is_delivery_pointer_failure,
    prune_old_transcripts,
    recovery_snapshot,
    scan_artifacts,
    ui_evidence_gate,
)
from .pane_env import (  # re-exported for test imports — see pane_env.py docstring
    _DEFAULT_MCP_STARTUP_TIMEOUT_MS,
    _DEFAULT_MCP_TOOL_TIMEOUT_MS,
    _LEAD_ENV_EXTRA_ALLOWLIST,
    _PANE_ENV_ALLOWLIST,
    _apply_color_term,
    _apply_mcp_timeout,
    _apply_non_interactive_env,
    _apply_port_file,
    _build_lead_env,
    _build_pane_env,
    inject_user_profile_env,
)
from .pipeline_executor import (  # re-exported for test imports; mixin provides methods
    _SHARD_GROUP_TIMEOUT_MS,
    _SPAWN_STAGGER_MS,
    PipelineMixin,
    PipelineRun,
    ShardGroup,
    _split_shard,
)
from .pty_session import PtySession, WritePriority  # re-exported for test imports
from .resource_governor import (
    GovernorLimits,
    ResourceClass,
    ResourceGovernor,
    ResourceToken,
    classify_resource,
)
from .roles import LEAD
from .roles import by_name as _role_by_name
from .spawn_engine import (  # re-exported for backward compat; mixin provides methods
    _PANE_COLS,
    _PANE_ROWS,
    _STUCK_RESUME_NUDGE,
    _TOCTOU_RESAMPLE_N,
    AUTO_RESPAWN_DELAY_MS,
    AUTO_RESPAWN_MAX,
    CODEX_EARLY_CRASH_WINDOW_SEC,
    RESUME_WINDOW_SEC,
    PaneRegistry,
    PaneState,
    SpawnEngineMixin,
)
from .task_delivery import DeliveryState, NoticeDeduper, make_notice_id
from .vault_mirror import (  # re-exported for test + script imports
    _DEFAULT_VAULT,
    _JUNK_NOTE_EXACT,
    _JUNK_NOTE_MIN_LEN,
    _JUNK_PROJECT_PREFIXES,
    _VAULT_ENV,
    _is_dedup_note,
    _is_junk_note,
    _is_junk_project,
    _render_decision_note,
    _resolve_vault_dir,
    distill_session_facts,
    distill_to_knowledge_base,
    prune_vault_logs,
    write_obsidian_graph_filter,
)

# #105 Phase B: the pane registry accepts either a real (display-backed)
# AgentPane or a display-free HeadlessPane — both expose the same
# session/state/signal surface (see headless_pane.py), so the engine drives
# either one identically. HeadlessWindow registers HeadlessPane instances;
# MainWindow keeps registering AgentPane instances unchanged.
AgentPaneLike = AgentPane | HeadlessPane

# Full ECMA-48 CSI + OSC stripper (issue #145). The old allowlist
# (`[mABCDHJKSThlsu]` finals, `[0-9;]*` params) missed 3 real cases seen in
# `takkub status` tail output: private-mode toggles like `\x1b[?25h`/`\x1b[?25l`
# (the '?' is a valid CSI parameter byte the old class didn't include),
# `\x1b[3G` (CHA — final byte 'G' wasn't in the allowlist), and OSC window-
# title/hyperlink sequences (`\x1b]0;...\x07` / `...\x1b\\`) which are a
# different escape family the old CSI-only pattern never matched at all.
# CSI = `ESC [` + parameter bytes (0x30-0x3F) + intermediate bytes
# (0x20-0x2F) + one final byte (0x40-0x7E), per ECMA-48 §5.4.
# OSC = `ESC ]` + any bytes up to a BEL or ST (`ESC \`) terminator, per
# ECMA-48 §5.6 / ECMA-35.
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Bound on how many bytes of a pane transcript we read to extract its tail for
# `takkub status`. A long session's transcript grows to MBs; reading the whole
# file every status call (just to keep the last few lines) is an unbounded memory
# spike. 64 KiB is ample for the 5-line tail even with very long lines. (M4#22)
_TRANSCRIPT_TAIL_BYTES = 64 * 1024

# Throttle for the #104 Windows Open-With transcript scan (issue #194): even
# a 64 KiB bounded read can block the Qt main thread for 900ms-2.5s when the
# open() call gets caught by Windows Defender's real-time scan — and the
# scan used to run on EVERY 5s idle-watchdog tick, for EVERY working pane,
# until the first hit. 30s still catches a stuck dialog quickly (a human
# needs time to notice + click through it anyway) while cutting the
# blocking-open frequency ~6x.
_SHELL_DIALOG_SCAN_INTERVAL_S = 30.0

# Issue #199: a pane that's actively producing output cannot be blocked on a
# modal Open-With dialog (a real dialog freezes the process until dismissed)
# — so gate the scan on the pane having been silent at least this long.
# Also doubles as a #194 win: an actively-working pane skips the scan (and
# its file open) entirely instead of merely being throttled.
_SHELL_DIALOG_IDLE_GATE_S = 20.0

# Issue #194: minimum spacing between two `write_resume_briefs()` chatlog
# scans — guards against the restart-then-closeEvent double-call (see the
# method's docstring) redoing the same expensive scan twice in one shutdown.
_RESUME_BRIEF_MIN_INTERVAL_S = 10.0

# Harvest hint: inject a '[cockpit] <role> ไม่ active >Nm' message into Lead
# when a teammate pane has been idle this long. 0 = disabled.
HARVEST_HINT_SEC = int(os.environ.get("TAKKUB_HARVEST_HINT_SEC", "600"))

# ── Proactive idle compaction (prompt-cache TTL cost control, issue #161) ───
# Claude's server-side prompt cache is written once, at whatever size the
# transcript is when the cache entry is (re)created, and expires after its
# TTL — normally ~1h, but Anthropic shortens it to ~5min while the account is
# in usage-overage (see limit_status.is_in_overage). A pane that piles up a
# long conversation and then sits fully idle across that TTL boundary pays
# the FULL transcript as one big cache-write the next time anyone talks to
# it — a cost that scales with how much context piled up before the pane
# went idle, not with the TTL itself. Sending `/compact` proactively once a
# pane has been idle long enough to be at real risk of crossing that
# boundary keeps the eventual re-cache small instead of paying for the whole
# pre-idle transcript in one shot. This is IN ADDITION to (never a
# replacement for) the CLI's own automatic near-full-context compaction —
# see _on_session_cap_exceeded's docstring for why the cockpit never second-
# guesses that one. 0 = disabled.
#
# Claude-only for now: `/compact` is a Claude Code CLI slash command with no
# confirmed equivalent on codex/gemini/opencode/kimi/cursor — a known
# multi-provider gap, tracked under #103 rather than silently assumed to work
# everywhere. See _check_proactive_compact's provider gate.
#
# #465 (user directive 2026-09-01): the real TTL is ~1h, not 25min — the old
# 25min default fired long before the cache was ever at risk, paying the
# summarize cost + losing context for zero TTL benefit whenever anyone talked
# to the pane before the hour was up. 55min sits close to that 1h cliff
# without routinely crossing it. See PROACTIVE_COMPACT_OVERAGE_IDLE_AFTER_S
# right below for the shortened threshold used during the ~5min-TTL overage
# window instead of this one.
PROACTIVE_COMPACT_IDLE_AFTER_S = int(
    os.environ.get("TAKKUB_PROACTIVE_COMPACT_IDLE_AFTER_S", str(55 * 60))
)

# #465 (user directive): Anthropic's server-side prompt-cache TTL collapses
# from its normal ~1h down to ~5min while the account's five-hour usage
# window is fully exhausted (see limit_status.is_in_overage) — PROACTIVE_
# COMPACT_IDLE_AFTER_S's 55min default would let an overage pane cross that
# much shorter cliff and eat a full-transcript cache-write anyway.
# `_check_proactive_compact` reads the project's cached usage state each
# tick (best-effort, cross-process shared file — limit_status.
# load_shared_state, no network fetch of its own) and swaps in this much
# shorter threshold whenever that state reports overage. 0 = disable
# proactive compaction specifically while in overage (the base threshold
# above still governs the normal, non-overage case).
PROACTIVE_COMPACT_OVERAGE_IDLE_AFTER_S = int(
    os.environ.get("TAKKUB_PROACTIVE_COMPACT_OVERAGE_IDLE_AFTER_S", str(4 * 60))
)

# Follow-up to #190: `proactive_compact_pending` is trusted as "our own
# /compact is still running" so the not-ready branch below leaves
# proactive_compact_idle_since alone while it's True. But if the pane is
# never observed back at its ready prompt before real work lands on it (a
# task gets assigned, or someone types directly into it, in the same window
# the compact was still finishing) the flag never gets cleared the normal
# way, and idle_since would stay stuck at its stale pre-compact value
# forever — silently suppressing the NEXT idle episode's `/compact` for as
# long as the pane keeps being busy. Bound how long `pending` can be trusted
# before the not-ready branch gives up and treats it like any other new
# not-ready (clears pending, resets idle_since). `_check_idle_teammates`
# ticks every IDLE_WATCHDOG_INTERVAL_MS (5s), so there's ample granularity
# to catch a compact that actually finishes within this window; the #190
# repeat-fire cycles logged in runtime/events.log put real compact runtime
# around ~2 minutes, so 10 minutes leaves a wide margin before a stuck
# `pending` gets mistaken for a still-running compact. Overrideable via env
# like the sibling proactive-compact knobs.
PROACTIVE_COMPACT_PENDING_CEILING_S = int(
    os.environ.get("TAKKUB_PROACTIVE_COMPACT_PENDING_CEILING_S", str(10 * 60))
)

# "Nothing new since the last compact" gate (user report 2026-08-25): the
# one-per-idle-episode rule above still re-fired `/compact` on the SAME
# untouched conversation every ~25-45 min all night, because an idle episode
# ends on ANY not-ready blip (a status-line redraw the ready classifier
# misses for a tick, a hook-driven session report, a `ready_marker_possibly_
# stale` wobble) — none of which add anything to compact. Claude Code then
# either re-summarises an already-compacted context or reports there is
# nothing to compact, and the cockpit counts that as a fresh episode and
# does it again. Now the pane must have emitted at least this many raw PTY
# bytes since the previous `/compact` settled before another one is sent;
# otherwise the episode is marked handled without writing anything. 8 KiB
# comfortably exceeds idle footer/clock redraws and is far below the output
# of even a one-tool work turn. 0 = disable the gate.
#
# #465 follow-up: the skip used to also stamp proactive_compact_sent_ts, the
# same field an ACTUAL `/compact` stamps — which made
# `proactive_compact_sent_ts >= proactive_compact_idle_since` (the "already
# compacted this idle episode" guard above) true for the rest of the
# episode, so real output arriving later in the SAME idle episode (while the
# pane never left its ready prompt, e.g. a live-delivered notice) could
# never re-trigger the check again, contradicting this comment's own "never
# re-fires until real output arrives" claim. The skip below now leaves
# sent_ts untouched — only `proactive_compact_skip_logged_bytes` dedupes
# its logging — so every later tick keeps re-evaluating this gate fresh.
PROACTIVE_COMPACT_MIN_NEW_OUTPUT_BYTES = int(
    os.environ.get("TAKKUB_PROACTIVE_COMPACT_MIN_NEW_OUTPUT_BYTES", str(8 * 1024))
)

# ── Screenshot evidence auto-attach (issue #5) ──────────────────────────────
# Extensions done() treats as "evidence" when scanning the pane's artifacts
# dir. Case-insensitive match against Path.suffix.
_EVIDENCE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
# A file must be at least this old before it's trusted as "fully written" —
# guards against picking up a screenshot mid-write (half a PNG has a valid
# mtime but garbage bytes).
_EVIDENCE_SETTLE_SEC = 1.0
# Windows can hold a brief exclusive lock on a just-written image (AV scan,
# the writer's own fsync); retry stat() a couple of times before giving up
# on that one file rather than letting done() blow up.
_EVIDENCE_STAT_RETRIES = 3
_EVIDENCE_STAT_RETRY_SLEEP_SEC = 0.05
# Cap how many paths get pasted into a done notice — a shot-happy QA run can
# produce dozens; Lead only needs the most recent handful.
_EVIDENCE_MAX_FILES = 10
# Roles expected to always produce fresh evidence; a done with none gets a
# warning line (everyone else silently gets nothing when they have no shots).
_EVIDENCE_WARN_ROLES = ("qa", "critic", "designer", "reviewer")

# Issue #159: a screenshot capture can fail silently (blank/loading page,
# race with render, browser crash mid-shot) and still land as a valid file
# that passes the extension/mtime/settle filters above — the role reports
# "evidence collected" with no idea the shot itself is bad. A real screenshot
# is rarely this small; below this, flag it as suspect rather than trust
# existence alone as proof of a good capture.
_EVIDENCE_SUSPECT_MIN_BYTES = 10 * 1024
# Cheap magic-byte sniff per extension — not a full decode (no image-lib
# dependency), but enough to catch a 0-byte/truncated/HTML-error-page file
# saved under an image extension.
_EVIDENCE_MAGIC_PREFIXES: dict[str, tuple[bytes, ...]] = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
}

# Issue #182: a byte-identical screenshot filed under several different
# names (a retry that overwrote nothing, a copy-paste evidence list, a
# capture script that re-saved the same failed frame under each expected
# filename) passes every #159 check — real magic bytes, plausible size —
# while telling Lead nothing distinct about N different app states. Lead
# caught one live case only by manually diffing md5 sums. Cap how many bytes
# get hashed per file: screenshots are typically well under this, and a
# multi-MB capture is rare enough that skipping its dedup check (never
# skipping the size/header checks) beats blocking done() on slow disk I/O.
_EVIDENCE_DEDUP_MAX_BYTES = 8 * 1024 * 1024

# Issue #evidence-cite: path-like or test-result tokens that count as a note
# "citing" evidence even when the screenshot scan above found nothing (e.g. a
# reviewer citing a log path, or qa citing a pytest summary line). Engine
# check only — never blocks, just widens what counts as "cited" before the
# warn-role note gets tagged.
_EVIDENCE_CITE_RE = re.compile(
    r"(?:docs/|runtime/|tests/|\$TAKKUB_ARTIFACTS_DIR|\$SHOT_DIR"
    r"|\.(?:md|png|jpe?g|log|json|txt)\b"
    r"|\b\d+\s+passed\b|\bexit\s*0\b)",
    re.IGNORECASE,
)

# Windows Open-With dialog tripwire (issue #104): a shell one-liner that
# mangles a bare file path into command position gets ShellExecute'd by
# Windows, popping this exact dialog title text. The dialog is a native GUI
# modal — the pane's PTY shows no error, just silence, so the transcript
# watchdog is the only way to catch it without a human noticing the popup.
_SHELL_OPEN_DIALOG_MARKER = "How do you want to open"

__all__ = [  # backwards-compat re-exports
    "HARVEST_HINT_SEC",
    "LEAD_NOTIFY_BUSY_CAP",
    "_DEFAULT_MCP_STARTUP_TIMEOUT_MS",
    "_DEFAULT_MCP_TOOL_TIMEOUT_MS",
    "_DEFAULT_VAULT",
    "_HARVEST_EXCLUDE_DIRS",
    "_JUNK_NOTE_EXACT",
    "_JUNK_NOTE_MIN_LEN",
    "_JUNK_PROJECT_PREFIXES",
    "_LEAD_ENV_EXTRA_ALLOWLIST",
    "_LEAD_GUARD_ALLOW_TOOLS",
    "_LEAD_GUARD_WRITE_TOOLS",
    "_PANE_ENV_ALLOWLIST",
    "_VAULT_ENV",
    "PaneRegistry",
    "_allowed_project_roots",
    "_apply_color_term",
    "_apply_mcp_timeout",
    "_apply_non_interactive_env",
    "_apply_port_file",
    "_build_lead_env",
    "_build_pane_env",
    "_default_plugin_dirs",
    "_is_dedup_note",
    "_is_junk_note",
    "_is_junk_project",
    "_recent_session_brief",
    "_render_decision_note",
    "_render_lead_context",
    "_resolve_vault_dir",
    "inject_user_profile_env",
    "prune_old_transcripts",
    "prune_vault_logs",
    "render_lead_settings",
    "scan_artifacts",
    "write_obsidian_graph_filter",
]


# RESUME_WINDOW_SEC moved to spawn_engine.py; re-exported above

# Stall detection: if a `working` pane shows no detectable progress (no
# transcript bytes, no new screenshots, no takkub send received) for this long,
# `list_status_detailed()` marks it stalled and `takkub list` shows
# `active (stalled Nm)` instead of plain `active`.
# Overrideable via env so QA-heavy workflows can tune the threshold.
STALL_THRESHOLD_SEC = int(os.environ.get("TAKKUB_STALL_THRESHOLD_SEC", "300"))

# Follow-up to #130: the busy-not-stuck grace period in lead_inbox._check
# (deferring the hard-timeout delivery warning while a pane keeps producing
# output) has no ceiling of its own — a pane whose output is real but that
# never returns to its ready prompt (e.g. a TUI stuck redrawing a spinner/
# progress animation on a loop, never actually accepting input) would poll
# forever with no warning ever reaching the Lead. 30 minutes clears this
# repo's own longest normal gate (full pytest ~6 min) with wide margin, plus
# typical build/docker-pull waits, while still bounding an otherwise-
# unbounded wait. Overrideable via env for slower CI/hardware.
BUSY_WAIT_CEILING_SEC = int(os.environ.get("TAKKUB_BUSY_WAIT_CEILING_SEC", "1800"))

# #248/#247 round 2: how long lead_inbox._send_when_ready's delivery poll
# waits, past spawn, for a pane's provider CLI to render ANY content
# (session.first_content_ts() staying None the whole time) before treating
# it as a wedged spawn rather than a slow-but-healthy cold boot — the exact
# "codex pane spawns, never produces output" symptom from #248. Chosen
# comfortably above every provider's own ready_wait_ms cold-boot allowance
# (claude 45s, codex/gemini/opencode/kimi/cursor 90s) would otherwise need,
# but far below BUSY_WAIT_CEILING_SEC above, since a pane that has rendered
# NOTHING at all (not even a boot banner) is a much stronger "something is
# wedged" signal than one that's merely slow to reach its ready prompt.
# Overrideable via env for slower CI/hardware.
NO_CONTENT_WATCHDOG_SEC = int(os.environ.get("TAKKUB_NO_CONTENT_WATCHDOG_SEC", "75"))

# Issue #254: a pane stuck showing a provider boot-phase marker (codex/agy
# "Booting MCP server: …") redraws its spinner continuously, so
# seconds_since_output() never grows and the #144 busy-wait extension above
# silently renews itself with no further signal until BUSY_WAIT_CEILING_SEC
# (30 min) — a real incident sat there with the task never delivered at all
# (composer still showed the placeholder prompt) and Lead had nothing to act
# on beyond the single "still waiting" notice #144 already sends. This bounds
# how long lead_inbox._send_when_ready's poll loop tolerates a CONTINUOUS
# boot-marker streak before escalating with a distinct, more actionable
# warning naming the stuck phase (issue #254) — well above codex/agy's own
# ready_wait_ms cold-boot allowance (90s, see provider_spec.py) so an
# ordinary boot that finishes on time never trips it, but far below
# BUSY_WAIT_CEILING_SEC so Lead hears about a genuinely wedged boot in
# roughly two minutes instead of thirty. Overrideable via env.
BOOT_STALL_GRACE_SEC = int(os.environ.get("TAKKUB_BOOT_STALL_GRACE_SEC", "110"))

# Issue #276: hard ceiling on the SAME boot-marker streak the grace above only
# warns about. Before this, a pane that never left its boot phase kept polling
# until BUSY_WAIT_CEILING_SEC (30 min) and then blind-pasted the task onto a
# splash screen that has no composer to receive it — so the task was neither
# delivered nor failed, it just stopped existing, and the only thing standing
# between that and a wrong answer was Lead noticing by hand. (The reported
# incident is worse than "slow": Lead closed the pane at ~110-180s, a report
# from a DIFFERENT task then arrived, and the work was marked done having
# never run.)
#
# At this ceiling the delivery is failed explicitly instead: the task's ledger
# row flips to fail, Lead gets a blocking notice naming the role and the
# recovery command, and the pane is torn down — the outcome is wrong, but it
# is never silent. 5 minutes is well past every provider's own cold-boot
# allowance (`ready_wait_ms`, 90s max) plus codex's measured ~61s baseline for
# even a trivial prompt (#278), so a genuinely slow boot still wins the race;
# it is only a boot that is not going to finish that gets cut. Overrideable
# via env for slower hardware.
BOOT_STALL_CEILING_SEC = int(os.environ.get("TAKKUB_BOOT_STALL_CEILING_SEC", "300"))

# #407: per-MCP-server startup allowance the boot ceiling above is widened by
# when a pane was spawned with N cockpit-injected MCP servers. Matches the
# startup timeout every provider branch actually hands its CLI —
# `mcp_bridge._CODEX_DEFAULT_STARTUP_TIMEOUT_SEC` (codex `startup_timeout_sec`)
# and `pane_env._DEFAULT_MCP_STARTUP_TIMEOUT_MS` (claude `MCP_TIMEOUT`), both
# 120 s — so a pane legitimately still inside its OWN MCP startup budget
# (e.g. codex + playwright + chrome-devtools on a cold npx cache: measured
# >300 s end-to-end in #407) is not failed by a ceiling that predates MCP
# injection. See `LeadInboxMixin._boot_stall_ceiling_sec`.
MCP_STARTUP_TIMEOUT_SEC = int(os.environ.get("TAKKUB_MCP_STARTUP_TIMEOUT_SEC", "120"))


# Live probe for Qt main-thread heartbeat staleness (#133 — fan-out delivery
# corruption: concurrent pane spawns backlog the Qt event loop for ~1s at a
# time, so a batch of already-scheduled submit-verify timers fires in a burst
# the instant the backlog clears; the self-heal in lead_inbox mistook that
# burst for real swallowed-paste/CR evidence and repasted on top of a paste
# that was simply still rendering — proven via events.log: duplicate
# `remaining: 3` values on concurrent lead_notify_repaste entries, timestamped
# right after main_thread_stall episodes). Registered once by app.py's
# watchdog setup (`set_main_thread_heartbeat_probe`); read by
# lead_inbox._delayed_enter_verified via `_orch_attr` so the verify loop can
# defer a swallow verdict instead of concluding one — without lead_inbox
# importing app (forbidden by the lead-inbox-layer contract). Defaults to
# "never stale" so headless/test runs (no watchdog registered) keep the
# pre-#133 behaviour exactly.
def _no_heartbeat_probe() -> float:
    return 0.0


_main_thread_heartbeat_age = _no_heartbeat_probe


def set_main_thread_heartbeat_probe(probe) -> None:
    """Register the live Qt-main-thread heartbeat-staleness probe.

    *probe* takes no args and returns seconds since the main thread last
    proved itself alive (0.0 = fine). Called once from app.py's watchdog
    startup; tests may call it directly to simulate a stall window.
    """
    global _main_thread_heartbeat_age
    _main_thread_heartbeat_age = probe


# When `_LAST_SESSION_FILE` is newer than this and teammates are alive,
# the current Lead boot is treated as post-compact so a status snapshot
# is auto-injected into the Lead prompt.
_POST_COMPACT_DETECT_SEC = 5 * 60

# Idle watchdog: when a teammate pane sits at the ready prompt (claude is
# idle, no "esc to interrupt") while pane.state is still "working", the
# orchestrator assumes the agent finished its task but forgot to call
# `takkub done`. After IDLE_REMIND_AFTER_S of continuous idle we surface a
# cockpit-side notice, then back off for IDLE_REMIND_COOLDOWN_S before another.
# This primary path never writes to the PTY, so it works for every provider
# (including providers without Claude-specific prompt markers; #103) without
# waking a full model turn. After N UI-only rounds, one PTY reminder is retained
# as a last-resort escalation for panes that really finished but forgot done.
# Set IDLE_REMIND_AFTER_S to 0 to disable the watchdog entirely; set escalation
# rounds to 0 to keep reminders permanently UI-only.
IDLE_REMIND_AFTER_S = 45
IDLE_REMIND_COOLDOWN_S = 90
IDLE_REMIND_ESCALATE_AFTER_ROUNDS = max(
    0, int(os.environ.get("TAKKUB_IDLE_REMIND_ESCALATE_ROUNDS", "3"))
)

# Fallback that arms the forgot-done reminder even when the watchdog never
# caught the pane mid-turn. The `seen_working` latch (set when a tick observes
# a genuine, non-startup busy state) suppresses reminders for a codex/agy pane
# still booting/queuing its task — but a task that starts AND finishes between
# two 5 s ticks never flips the latch, which would strand a genuinely
# finished-but-forgot pane with no reminder at all. Provider boot/queue windows
# are bounded by the ready-wait allowance (90 s for codex/agy), so once a pane
# has sat continuously idle past this it is not booting any more and the
# reminder is safe to fire regardless of the latch.
IDLE_REMIND_UNLATCHED_AFTER_S = 180

# Stuck-paste reaper. A task pasted at spawn renders "[Pasted text +N lines]"
# in the input box; under parallel-spawn CPU load the submitting Enter (and the
# delivery self-heal's 3 resends, all within ~3 s) can be swallowed while
# claude is still initialising — leaving the pane idle-at-ready with the task
# forever unsent. The idle reminder used to rescue this by accident (its
# trailing Enter submitted the stuck paste), but any reminder-suppression gate
# (e.g. a rate-limit flag) starves that rescue. This reaper is the DIRECT fix:
# a "working" pane at its ready prompt showing pending input for
# STUCK_PASTE_SUBMIT_AFTER_S gets a bare CR (harmless if a submit is somehow
# in flight), retried every STUCK_PASTE_SUBMIT_COOLDOWN_S up to
# STUCK_PASTE_SUBMIT_MAX times. It runs BEFORE every suppression gate so no
# false flag can starve it.
STUCK_PASTE_SUBMIT_AFTER_S = 15.0
STUCK_PASTE_SUBMIT_COOLDOWN_S = 30.0
STUCK_PASTE_SUBMIT_MAX = 4

# Structural stale-marker detector (#20). A pane that is alive, has produced no
# output for STALE_MARKER_QUIET_S (a generating CLI streams continuously, so
# this long a silence means it is NOT mid-generation), and is matched by NO
# state marker (not ready, not a known tty/trust/splash prompt) is almost
# certainly sitting at an idle prompt whose wording an upstream CLI update
# changed out from under our markers — the silent-break failure mode of #20.
# We log it (rate-limited per pane) WITH the bottom screen text so the operator
# can see the real footer and rescue detection via TAKKUB_EXTRA_READY_MARKERS.
STALE_MARKER_QUIET_S = 20.0
STALE_MARKER_COOLDOWN_S = 600.0
STALE_MARKER_TAIL_ROWS = 4

# (#343) A pane stuck in the unrecognised-and-quiet state across multiple
# STALE_MARKER_COOLDOWN_S windows in a row is no longer "one routine 🟡 sweep
# line" — the real episode this was written for ran ~9h/106 occurrences with
# nothing above that routine line the whole time and nobody found out until
# someone happened to run `takkub ma` the next morning. Every Nth consecutive
# occurrence for the same pane (3 ≈ 30 min of continuous, unresolved
# staleness — long enough to rule out a one-off blip, short enough to still
# catch it same-day) fires a louder, separately-classified event with a full
# diagnostic dump instead. Does NOT change is_at_ready_prompt/marker
# detection itself — see _check_stale_markers' docstring for why guessing at
# the marker table here would be actively dangerous.
_STALE_MARKER_ESCALATE_EVERY = 3
# The exact marker checks _check_stale_markers tries before concluding a
# quiet pane is unrecognised — named explicitly here so an escalation dump
# states which checks were attempted (all False) rather than leaving that
# implicit in code the reader may not have open.
_STALE_MARKER_CHECKS_TRIED = (
    "is_at_ready_prompt",
    "is_blocked_on_tty_prompt",
    "is_at_trust_prompt",
    "is_blocked_on_permission_prompt",
    "is_at_update_splash",
)

# (#412) Lead is exempt from `_check_idle_teammates`'s forgot-`done` loop
# entirely, but `_check_stale_markers` above never special-cased Lead — so a
# provider whose ready-prompt marker doesn't cover every genuinely-idle shape
# (real report: opencode, macOS) can drive Lead through the SAME #343
# escalation built for a wedged teammate, paging the operator that Lead
# "ค้าง/ล่ม" while Lead is doing exactly what Lead is supposed to do: sitting
# at its own prompt waiting for the human's next instruction after every
# teammate finished. This is a structural, provider-agnostic guard (no marker
# text involved, unlike the claude-only empty-composer fallback further
# below) — for LEAD_DONE_IDLE_GRACE_S after the last teammate in a project
# stops being "working", Lead's stale-marker streak is held at zero instead
# of accumulating. Once the grace window elapses with the team still idle,
# detection behaves exactly as before (a Lead still unrecognised past that
# point is treated as potentially genuinely stuck, not idle-awaiting-user).
LEAD_DONE_IDLE_GRACE_S = 1800.0


def _stale_marker_footer(sess: PtySession) -> str:
    """Bottom-of-screen diagnostic text for the stale-marker log/escalation —
    factored out so _resolve_stale_marker_nudge can re-capture it (phase 2,
    after a real post-redraw repaint) with exactly the same shape
    _check_stale_markers used to detect it."""
    return " | ".join(
        ln.strip() for ln in sess.display_lines()[-STALE_MARKER_TAIL_ROWS:] if ln.strip()
    )[:300]


# A teammate pane in `working` state with no PTY output for this long
# is treated as hung — claude probably crashed silently, deadlocked on
# a tool call, or got wedged behind a slow MCP server. Orchestrator
# auto-recovers via close + respawn (which picks `--resume <uuid>` because
# the recent-exit timestamp and UUID are still fresh). 10 minutes is generous enough
# that a heavy `npm install` or a slow Lighthouse audit won't trip it.
STUCK_THRESHOLD_S = 10 * 60
# Once a recover fires for a pane, wait this long before another one
# is allowed — otherwise a chronically-stuck workload restarts on a loop.
STUCK_RECOVER_COOLDOWN_S = 5 * 60
# Hard cap on consecutive stuck-recover attempts for a single pane (#41).
# auto-respawn-attempts only caps *crash* respawns; a pane that is alive but
# wedged (deadlocked on a tool call, never reports done) never crashes, so the
# cooldown above would otherwise let the watchdog close→respawn it forever —
# stalling any pipeline hop it belongs to indefinitely. After this many
# recoveries we give up: warn Lead, fail+advance the pipeline hop, and leave the
# pane for the operator instead of looping. ~3 strikes ≈ 30 min of repeated
# wedging (STUCK_THRESHOLD_S + STUCK_RECOVER_COOLDOWN_S per cycle).
STUCK_RECOVER_MAX = 3
# #288: how long the watchdog keeps deferring a recover for a pane that is
# content-static but still has real (non-scaffolding) child processes running.
# Sized for the workload that produced the bug — a QA pane driving a browser
# through a multi-step Playwright script, which routinely runs 10-30 min with a
# frozen screen. Past this the deferral stops: a child process that is itself
# hung must not be able to pin a pane out of recovery indefinitely.
STUCK_LIVE_CHILD_GRACE_S = int(os.environ.get("TAKKUB_STUCK_LIVE_CHILD_GRACE_S", 60 * 60))
# Cooldown between "watchdog held off, work is still running" notices to Lead.
# A silent long-running script is normal, so this reports the situation once per
# episode rather than on every watchdog tick.
STUCK_LIVE_CHILD_NOTICE_COOLDOWN_S = 15 * 60

# TTY prompt block detection (issue #54). When a pane's subprocess is waiting
# for interactive input (y/N, passphrase, "press any key"), close→respawn won't
# help because the prompt comes from the subprocess, not claude. Suppress the
# idle forgot-done reminder (wrong context) and surface a notice to Lead instead.
# Auto-recover is deliberately opt-in / off-by-default.
TTY_BLOCK_SURFACE_AFTER_S = 2 * 60  # first surface after 2 min of continuous block
TTY_BLOCK_SURFACE_COOLDOWN_S = 3 * 60  # minimum gap between repeated surface notices

# Update-splash dismissal (issue #62). When a codex pane is stuck at the
# startup 'update available!' splash, send Enter once to dismiss it instead of
# close→respawn. SPLASH_DISMISS_COOLDOWN_S is the grace period after the Enter
# before we declare the dismiss a failure and fall back to close→respawn.
SPLASH_DISMISS_COOLDOWN_S = 30

# Stuck-tool watchdog (#308). A pane wedged inside a shell/tool call the
# CLI never returns from — real incident: an agy pane sat on "Running
# command..." for ~13 minutes while its OWN idle footer stayed visible below
# it, so `is_at_ready_prompt()` read the pane as normal the whole time and
# neither the content-hash stuck watchdog (STUCK_THRESHOLD_S, 10 min, ALSO
# gated on screen content — same false-idle blind spot) nor the idle-reminder
# loop ever suppressed themselves correctly. This is independent of both:
# `ProviderSpec.tool_running_markers` + `PtySession.seconds_since_output()`
# (spinner-normalized, same clock #130's fix already relies on) is the only
# signal, so it fires even while the ready-marker classifier is fooled.
# Overrideable — the field incident used a ~10 min recursive filesystem scan;
# a slower/loaded machine may need more headroom before this is "stuck".
TOOL_STUCK_TIMEOUT_SEC = float(os.environ.get("TAKKUB_TOOL_STUCK_TIMEOUT_SEC", "180"))
# How long to wait after the one-shot Esc recovery keystroke before deciding
# it didn't work and recommending a manual close+respawn to Lead. Short on
# purpose — Esc either interrupts the wedged tool call within a few seconds
# or it doesn't, and #308's own workaround (close+respawn) doesn't get any
# safer by waiting longer to suggest it.
TOOL_STUCK_ESC_GRACE_S = 15

# Malformed tool-call XML detection (issue #59). When a model outputs tool-call
# XML without the `antml:` namespace prefix the harness silently no-ops it and
# the pane appears to hang. Nudge the pane at most this often.
MALFORMED_XML_NOTICE_COOLDOWN_S = 60  # minimum gap between repeat nudges

# _STUCK_RESUME_NUDGE moved to spawn_engine.py; re-exported above

# Session-goal context header (issue #50). Prepended to every `assign`
# task while a goal is set. Also doubles as the idempotency marker that
# _apply_session_goal greps for to avoid double-prepending on respawn replay.
_SESSION_GOAL_HEADER = "[SESSION GOAL — ทุก role ในงานนี้ยึดเป้าหมายเดียวกัน]"

# A goal is a short objective + scope boundary; bound it so a pathological
# paste (review tok-3: worst case ~64 KiB) can't be re-prepended to every
# assign for the rest of the session. 4000 chars (~1k tokens) is far above any
# real objective. Truncation is at set-time so the stored value is already
# clean for every later prepend.
_SESSION_GOAL_MAX = 4000

# Throughput watchdog (issue #35): flag panes whose PTY output rate exceeds
# RUNAWAY_BYTES_S continuously for RUNAWAY_DURATION_S seconds.
#
# Rationale for thresholds:
#   500 KB/s — a fast build log (e.g. webpack) peaks around 100-200 KB/s;
#   500 KB/s sustained is essentially only seen when a loop prints without
#   any sleep (runaway agent).
#   60 s — a single burst (e.g. `npm install`) can look high for ~10 s;
#   requiring it to sustain 60 s eliminates transient spikes that are not
#   worth bothering Lead about.
RUNAWAY_BYTES_S = 500_000  # 500 KB/s sustained output rate
RUNAWAY_DURATION_S = 60.0  # seconds of sustained overrate before warning Lead
RUNAWAY_WARN_COOLDOWN_S = 300.0  # suppress repeat warnings for 5 min

# Over-capacity advisory (Queue-gap audit, docs/reviews/2026-06-30-queue-gap.md).
# When a fresh teammate spawn pushes the TOTAL live pane count over what the
# machine can comfortably run (exec_mode.machine_total_pane_cap()), warn the Lead
# once per this window so a burst of fan-out assigns doesn't spam. Non-blocking.
OVERCAP_WARN_COOLDOWN_S = 60.0


def _count_active_teammates(panes_by_project: dict) -> int:
    """Total live non-Lead panes across every project (machine-wide).

    Shared by the over-capacity advisory and the fan-out queue so the two can
    never drift on what "machine is full" means. Lead panes (one per tab) are
    excluded — they anchor the cockpit and aren't the resource hogs.
    """
    n = 0
    for _panes in panes_by_project.values():
        for _r, _p in _panes.items():
            if _r == LEAD.name:
                continue
            sess = getattr(_p, "session", None)
            if sess is not None and getattr(sess, "is_alive", False):
                n += 1
    return n


def _open_with_dialog_process_present() -> bool:
    """Best-effort corroboration for the #104 Windows Open-With dialog
    tripwire (issue #199): a transcript-text match alone false-positived
    with zero real dialog behind it (no OpenWith.exe/AppPicker/rundll32
    process on the machine at all), so the notify path now requires this
    to also return True. psutil is an existing dependency (resource_governor.py);
    imported lazily here since only the Windows tripwire path needs it.
    Windows-only by construction — the caller never invokes this off
    `sys.platform == "win32"`, since this dialog type has no equivalent on
    macOS's `_pty_backend`. Degrades to False (no corroboration → no
    notify) on any psutil/permission hiccup."""
    try:
        import psutil
    except Exception:
        return False
    try:
        for proc in psutil.process_iter(("name",)):
            name = (proc.info.get("name") or "").lower()
            if name in ("openwith.exe", "apppicker.exe"):
                return True
            if name == "rundll32.exe":
                try:
                    cmdline = " ".join(proc.cmdline()).lower()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if "openas_rundll" in cmdline:
                    return True
    except Exception:
        pass
    return False


def _fanout_queue_enabled() -> bool:
    """True iff the flag-gated fan-out queue is enabled. Default OFF, so the
    cockpit's spawn behaviour is unchanged unless the operator opts in via
    TAKKUB_QUEUE_FANOUT (the over-capacity advisory still fires regardless).

    When ON, a fresh teammate spawn that would exceed machine_total_pane_cap()
    is deferred to a per-project queue and spawned automatically once a pane
    frees a slot (done/close). See docs/reviews/2026-06-30-queue-gap.md.
    """
    return os.environ.get("TAKKUB_QUEUE_FANOUT", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


# Spinner-line filtering for content-delta stuck detection (Fix 3).
# Lines matching any interrupt phrase or volatile counter pattern are excluded
# from the hash so a pane that only emits spinner bytes is still detected as stuck.
#
# #308: this table used to be a hand-copied 4-item subset ("esc to interrupt",
# "esc to stop", "ctrl-c to", "ctrl+c to") of provider_spec's own
# READY_HARD_BLOCKERS — missing gemini/agy's confirmed "esc to cancel" (agy's
# own footer wording, cited directly by #308's report) among others. That
# drift is exactly why a #308 agy pane wedged on "Running command..." kept
# reading `takkub status` as "last progress: 1s ago": the phrase guard never
# matched agy's own busy chrome, so only the volatile-counter regex below was
# left to filter the line — and it separately failed on a trailing counter
# like "(esc to cancel · 412s)" (see that regex's own note). Unioned with
# READY_HARD_BLOCKERS (rather than replaced by it) so a provider's confirmed
# busy phrase can never drift out of sync with this filter again, while
# keeping "esc to stop"/"ctrl-c to"/"ctrl+c to" — observed CLI phrasing not
# (yet) captured in any ProviderSpec.ready_hard_blockers table.
_SPINNER_INTERRUPT_PHRASES_BASELINE = ("esc to interrupt", "esc to stop", "ctrl-c to", "ctrl+c to")


def _spinner_interrupt_phrases() -> tuple[str, ...]:
    from .provider_spec import READY_HARD_BLOCKERS

    return tuple(dict.fromkeys((*_SPINNER_INTERRUPT_PHRASES_BASELINE, *READY_HARD_BLOCKERS)))


_SPINNER_VOLATILE_RE = re.compile(
    # #308: was `\d+s[\s·]` — required a trailing space/middot after the
    # counter, so a line ending "...412s)" (paren right after the digits,
    # agy's own busy-line shape) never matched at all and kept resetting the
    # stuck-content clock every tick even though nothing real had changed.
    # `\b` matches a closing paren/end-of-line/anything non-word just as well
    # as whitespace, so the counter is stripped regardless of what follows it.
    r"\d+s\b|[↑↓]\s*[\d.,]+k?\s*tokens?",
    re.IGNORECASE,
)


def _human_duration(total_seconds: float) -> str:
    """Coarse "in Xh Ym" / "in Xm" / "in Xs" phrasing for a Lead-facing
    notice (#301) — matches _RATE_LIMIT_FALLBACK-scale windows (minutes to
    hours), so seconds only show up for a duration under a minute."""
    secs = max(0, int(total_seconds))
    hours, rem = divmod(secs, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


IDLE_WATCHDOG_INTERVAL_MS = 5_000
# #406: how long the cockpit-owned mb Chrome may sit with no browser pane
# alive before `_release_native_chrome_if_idle` kills it (see
# `_schedule_native_chrome_idle_release` for why not immediately).
_NATIVE_CHROME_IDLE_GRACE_MS = 60_000
# #435: min seconds between two `takkub done`-typed-as-text nudges per pane.
DONE_TEXT_NOTICE_COOLDOWN_S = 10 * 60

IDLE_REMINDER_TEXT = (
    "🔔 [auto-reminder] pane นี้ idle อยู่ — ถ้า task เสร็จแล้วต้อง run "
    '`takkub done "<summary>"` เป็นคำสั่ง shell **ตอนนี้** (ไม่ใช่พิมพ์เป็น text). '
    "Lead ไม่ได้รับ notice จนกว่าจะ run คำสั่งนี้จริง pane จะค้างจน auto-recover. "
    'ยังทำงานต่ออยู่ → ignore ข้อความนี้ ถ้าติด blocker → `takkub send --to lead "..."`'
)

# AUTO_RESPAWN_DELAY_MS, AUTO_RESPAWN_MAX, _PANE_COLS, _PANE_ROWS,
# CODEX_EARLY_CRASH_WINDOW_SEC, _TOCTOU_RESAMPLE_N moved to spawn_engine.py; re-exported above

# Where teammate-pane state lives between cockpit restarts. Lead panes
# are already restored by the open_tabs mechanism in projects.json
# (one Lead per tab). Teammate panes — frontend/backend/qa/etc. that
# the user spawned manually — disappear when cockpit shuts down. The
# session snapshot file records which teammates were live in each tab
# at the moment of shutdown (or at the last periodic tick) so the next
# cockpit launch can re-spawn them; since session UUIDs are in-memory only,
# each role gets a fresh --session-id (clean slate, no cross-session bleed).
#
# Skip snapshots older than _LAST_SESSION_MAX_AGE_SEC: an hour-old
# snapshot is stale enough that the underlying claude conversations
# have probably been compacted out of usefulness and a fresh spawn is
# the right call.
_LAST_SESSION_FILE = RUNTIME_DIR / "last-session.json"
_LAST_SESSION_MAX_AGE_SEC = 60 * 60


# PaneState moved to spawn_engine.py; re-exported above via SpawnEngineMixin import


# Reasons `resource_governor._overload_state_reason()` can return while the
# machine-wide overload latch is held (#305) — distinct from the per-class
# slot-limit reasons ("heavy_global_limit", "browser_global_limit", etc.):
# those two groups need entirely different fixes (wait for another pane to
# release its slot vs. free up CPU/RAM on the machine itself), so they must
# never be worded the same way.
_OVERLOAD_LATCH_REASONS = frozenset({"cpu_high", "memory_low", "waiting_resume"})


def _describe_resource_wait(
    role_name: str,
    resource_class: ResourceClass,
    reason: str,
    holders: list[tuple[str, str]],
    *,
    metrics: tuple[float, float] | None = None,
    own_project: str = "",
    queue_len: int = 0,
) -> str:
    """Human-readable queued message for a role denied a resource-governor
    slot (#240 point 3) — names the blocking pane(s) instead of just the bare
    limit reason, and is the single formatting source shared by `assign`'s
    return value and `_queued_resource_roles`'s `takkub list`/`status`
    surfacing so the two never drift apart.

    #305: `reason` used to always be worded as "waiting for {resource_class}
    slot (...)" even when the machine-wide overload latch — not the class's
    own slot count — was what actually denied it (e.g. "qa queued — waiting
    for browser slot (waiting_resume)" sent Lead hunting for a browser-lock
    holder that didn't exist). Overload-latch reasons now get their own
    wording naming the machine, not the resource class, plus the CPU/RAM
    numbers that were actually measured.

    *holders* is `ResourceGovernor.holders_for_class`'s ``(project_id,
    pane_id)`` list. #303: heavy/browser/build/... classes are capped
    machine-wide, so a holder is routinely a pane from a DIFFERENT project —
    the old pane-id-only rendering (e.g. "blocked by qa#1, qa") read exactly
    like a stale lock in the caller's OWN project and cost a real debugging
    session before anyone thought to check `runtime/events.log`. A holder
    outside *own_project* is now qualified with its project name; *queue_len*
    (machine-wide waiters for this same resource class) is appended when
    there's more than the one role asking.
    """
    if reason in _OVERLOAD_LATCH_REASONS:
        stats = f": CPU {metrics[0]:.0f}% · RAM free {metrics[1]:.0f}%" if metrics else ""
        return f"{role_name} queued — machine overloaded ({reason}{stats})"
    parts = [
        pane if (not own_project or proj == own_project) else f"{pane} (project '{proj}')"
        for proj, pane in holders
    ]
    blocked_by = f", blocked by {', '.join(parts)}" if parts else ""
    queue_note = (
        f" · {queue_len} queued machine-wide for {resource_class.value}" if queue_len > 1 else ""
    )
    if reason == "global_panes_limit":
        # #364 lever 3: `max_panes_global` denies proactively, before the
        # RAM/CPU governor latch above ever trips — name RAM explicitly here
        # too so this never reads as a bare, unexplained limit code (never a
        # silent rejection, same rule as every other queued-role message).
        stats = f" (RAM free {metrics[1]:.0f}%)" if metrics else ""
        return f"{role_name} queued — machine-wide pane cap reached{stats}{blocked_by}{queue_note}"
    return (
        f"{role_name} queued — waiting for {resource_class.value} slot "
        f"({reason}{blocked_by}){queue_note}"
    )


# Restart-reason marker (#232): `cockpit_restart` events.log lines only ever
# distinguished "cli" (takkub restart) from "user_action" (status-bar
# button) — an auto-triggered restart (npm self-update, git-pull update, the
# pip-sync fallback) rode the exact same `_restart_cockpit()` path with no
# reason of its own, so events.log gave no way to tell "the user restarted
# this" from "the cockpit restarted itself". A plain `_log_event` line
# doesn't survive the process exit a restart performs, so the reason is
# also dropped in this small marker file the OLD process writes right
# before quitting; the NEW process's `restore_teammates()` reads (and
# deletes) it once at boot to attribute the Lead-facing restore notice.
_RESTART_REASON_FILE = RUNTIME_DIR / "restart-reason.json"


def _write_restart_reason_marker(reason: str, **extra: object) -> None:
    try:
        _write_json_atomic(_RESTART_REASON_FILE, {"reason": reason, **extra})
    except Exception:
        pass


def _read_and_clear_restart_reason() -> dict:
    payload: dict = {}
    try:
        parsed = json.loads(_RESTART_REASON_FILE.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            payload = parsed
    except (OSError, ValueError, json.JSONDecodeError):
        payload = {}
    try:
        _RESTART_REASON_FILE.unlink()
    except OSError:
        pass
    return payload


def _restart_reason_suffix(restart_info: dict) -> str:
    reason = restart_info.get("reason")
    if reason == "npm_update" and restart_info.get("version"):
        return f" — restarted to apply update v{restart_info['version']}"
    if reason == "git_pull_update":
        return " — restarted to apply a git-pull update"
    if reason == "pip_sync_fallback":
        return " — restarted after a dependency sync"
    if reason == "user_action":
        return " — user-triggered restart"
    if reason == "cli":
        return " — via `takkub restart`"
    return ""


def _inject_v2_context(
    task: str,
    project_ns: str | None,
    role_name: str,
    base_role_a: str,
    effective_provider: str,
    *,
    retry_count: int = 0,
) -> str:
    """Core V2 Context Builder hook (#309 Phase 7c) —
    `_assign_dispatch`'s call site. Flag OFF (`TAKKUB_V2_CONTEXT=0`)
    short-circuits before any import, so `_assign_dispatch` stays byte-
    identical; `task` is returned unchanged either way on any failure
    (fail-open), including a `core.brain.facade.build_context_for_assign`
    call that doesn't return within 300ms — a stuck/slow recall must never
    delay a spawn, so it runs in a background thread with a hard timeout
    rather than inline on this (the caller's) thread.

    `retry_count` (v2-hardening C, `core.brain.escalation`) is
    `_assign_dispatch`'s own live-pane-reassign counter — plumbed straight
    through to `build_context_for_assign`'s Adaptive Escalation step;
    default 0 (no escalation) keeps every existing caller byte-identical."""
    try:
        from .core.brain.flag import v2_context_enabled

        if not v2_context_enabled():
            return task

        import concurrent.futures

        from .core.brain.facade import build_context_for_assign
        from .provider_spec import PROVIDER_REGISTRY

        supports_file_read = PROVIDER_REGISTRY[effective_provider].supports_agent_file_read
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # #452: set once this call gives up waiting, so the worker — which
        # `wait=False` below leaves running unattended, its return value
        # already discarded — checks in at its own heavy steps and bails
        # early instead of continuing to burn CPU/GIL time a Qt main thread
        # may right then be waiting on (e.g. mid pane-spawn).
        cancel_event = threading.Event()
        try:
            future = executor.submit(
                build_context_for_assign,
                project_ns,
                base_role_a,
                task,
                file_read_supported=supports_file_read,
                retry_count=retry_count,
                cancel_event=cancel_event,
            )
            try:
                context_block = future.result(timeout=0.3)
            except concurrent.futures.TimeoutError:
                context_block = ""
                cancel_event.set()
                _log_event("context_builder_timeout", role=role_name, project=project_ns)
            else:
                _log_context_gate_inefficient(role_name, project_ns)
        finally:
            # wait=False: a still-running worker must never block this
            # (already-timed-out) call — see the docstring above.
            executor.shutdown(wait=False)
        return f"{task}\n\n{context_block}" if context_block else task
    except Exception:
        _log_event("context_builder_hook_error", role=role_name, project=project_ns)
        return task


def _log_context_gate_inefficient(role_name: str, project_ns: str | None) -> None:
    """Closeout #C — surfaces `facade._save_gate_trace`'s "small task over
    15k tokens" flag as an event (`19_DIAGNOSTICS_OBSERVABILITY.md`'s
    trace fields already land in `doctor`'s `[context]` section via
    `trace_store`; this is the event-log half). Best-effort: a trace-read
    hiccup must never affect the assign that just succeeded."""
    try:
        from .core.context_sources.trace_store import load_last_trace

        trace = load_last_trace()
        if trace and trace.get("inefficient"):
            _log_event(
                "context_gate_inefficient",
                role=role_name,
                project=project_ns,
                task_size=trace.get("task_size"),
                total_tokens=trace.get("total_tokens"),
                budget_tokens=trace.get("budget_tokens"),
            )
    except Exception:
        pass


#: Roles that routinely need a different model/provider than the Lead's own
#: (model diversity) or independent-process isolation — a native subagent
#: shares the Lead's own process/provider, so it can never stand in for
#: these. #364 lever 2.
AUTO_SUBAGENT_EXCLUDED_ROLES: frozenset[str] = frozenset({"reviewer", "critic"})

#: Heuristic ceiling on task text length for "short enough to auto-run as a
#: subagent" — a longer spec usually means multi-step work that benefits
#: from a visible, resumable pane rather than a same-process child. #364
#: lever 2; adjust if real usage shows this cutting the wrong way.
AUTO_SUBAGENT_MAX_TASK_CHARS = 400


def resolve_auto_assign_mode(
    role_name: str,
    task: str,
    *,
    requested_mode: str | None,
    isolation: str,
    model: str | None,
    provider: str | None,
    effort: str | None,
    plan: bool,
    shard_total: int,
    project: str | None,
) -> tuple[str, str | None]:
    """Decide the effective `assign` mode when the caller left `--mode`
    unset (#364 lever 2 — auto-route short, no-frills tasks to a native
    subagent instead of a full pane; ~650MB/spawn saved).

    Returns `(mode, note)`. `note` is `None` whenever `requested_mode` was
    given explicitly (an explicit `--mode pane` or `--mode subagent` always
    wins, unconditionally — nothing to explain); otherwise it is a short
    human-readable reason worth echoing back in the assign ack, so an
    auto-picked mode is never silent.

    Picks `"subagent"` only when EVERY one of these holds — anything else
    falls back to `"pane"`, the safe/status-quo default:
      - `requested_mode is None` (caller didn't pin a mode itself)
      - not `plan` and `shard_total <= 1` (plan/shard fan-out are out of
        this lever's scope — both already support `--mode subagent`
        explicitly when the caller asks for it)
      - no `model`/`provider`/`effort` override (all three are already
        rejected outright for an explicit `--mode subagent`; auto-selection
        must not silently drop an override the caller asked for by
        upgrading to a mode that can't honor it)
      - `isolation != "worktree"` (a subagent shares the Lead's own
        process/cwd — it cannot hold a separate git worktree)
      - `role_name` has no `#N` shard suffix (an explicit fan-out target,
        even one dispatched outside `--shards` with `shard_total` unset —
        keep it in a pane alongside its siblings)
      - the base role isn't in `AUTO_SUBAGENT_EXCLUDED_ROLES`
      - `task` is under `AUTO_SUBAGENT_MAX_TASK_CHARS`
      - the role's EFFECTIVE provider is claude — a native subagent only
        ever runs the Lead's own provider (claude); every other provider
        (codex/gemini/opencode/kimi/cursor) has no subagent equivalent yet
        (#103 gap). Auto-selection must never silently downgrade a
        codex/gemini-routed role's engine by running it through a claude
        subagent instead of its own pane — it falls back to pane and says
        so, rather than picking one quietly.
    """
    if requested_mode is not None:
        return requested_mode, None
    if plan or shard_total > 1:
        return "pane", None
    if model or provider or effort:
        return "pane", None
    if isolation == "worktree":
        return "pane", None
    if _split_shard(role_name)[1] is not None:
        # An explicit `role#N` target — fan-out coordination, even when this
        # particular call's `shard_total` wasn't set (e.g. a direct `role#N`
        # assign outside `--shards`). Keep it in a pane like its siblings.
        return "pane", None
    base_role = _split_shard(role_name)[0].lower().strip()
    if base_role in AUTO_SUBAGENT_EXCLUDED_ROLES:
        return "pane", None
    if len(task or "") > AUTO_SUBAGENT_MAX_TASK_CHARS:
        return "pane", None

    from .provider_config import effective_provider_for

    provider_now = effective_provider_for(base_role, project)
    if provider_now != "claude":
        return "pane", (
            f"auto-subagent skipped for {role_name!r}: effective provider is "
            f"{provider_now!r}, not claude — native subagents only support "
            "claude today (#103 gap); using pane instead"
        )
    return "subagent", (
        f"auto-selected --mode subagent for {role_name!r}: task under "
        f"{AUTO_SUBAGENT_MAX_TASK_CHARS} chars, no isolation/model-diversity/plan need"
    )


class Orchestrator(
    PipelineMixin, LeadInboxMixin, LeadWaitMixin, SpawnEngineMixin, AutoResumeMixin, QObject
):
    """Owns the pane registry and routes commands.

    Layout policy: Lead is always pre-registered (created by main_window) and
    fills the window initially. Teammate panes are created on demand the
    first time we spawn that role, via the `paneRequested` signal which
    main_window connects to its own add-pane logic.
    """

    statusChanged = pyqtSignal()
    leadInjected = pyqtSignal(str)
    # Emitted when user toggles a provider on/off via status bar. main_window
    # listens to refresh chip color/label without polling.
    providerStateChanged = pyqtSignal(str, bool)  # (provider, disabled)
    # Emitted when user flips the account plan (Pro/Max) via the status bar.
    # main_window listens to repaint the plan chip without polling.
    planTierChanged = pyqtSignal(str)  # "pro" | "max"
    execModeChanged = pyqtSignal(str)  # "solo" | "parallel"
    # Emitted when user flips the auto-resume (🌙) toggle via the status bar.
    # main_window listens to repaint the chip without polling.
    autoResumeChanged = pyqtSignal(bool)
    # Emitted by AutoResumeMixin's background usage-confirm fetch (signal b)
    # once it has an answer, so the actual park decision runs on the Qt
    # thread instead of the fetch's daemon thread. (project, role, confirmed)
    limitUsageConfirmed = pyqtSignal(str, str, bool)
    paneRequested = pyqtSignal(
        str, str
    )  # role_name, project — main_window adds pane to the matching tab
    paneClosed = pyqtSignal(
        str, str
    )  # role_name, project — main_window removes pane from the matching tab
    agentDone = pyqtSignal(
        str, str, str
    )  # project_ns, role_name, note — includes project to prevent cross-tab contamination
    # Emitted when a teammate's done() fires for a project that is NOT the
    # currently active tab. main_window connects this to show a status-bar
    # flash so the user sees background-tab activity without switching tabs.
    crossTabDone = pyqtSignal(str, str, str)  # project_ns, role, note
    # `takkub restart` (CLI) → main_window._restart_cockpit (persist + relaunch).
    # Emitted deferred so the IPC reply flushes before the app starts quitting.
    restartRequested = pyqtSignal()
    # Emitted whenever a notice is queued for a live Lead pane (done handoffs,
    # peer-CCs, system messages). main_window connects this to put an unread
    # red dot on that project's Lead pane-tab when the user is looking at a
    # different pane — so a Lead notification can't slip by unseen now that the
    # panes-as-tabs layout shows only one pane at a time.
    leadNotified = pyqtSignal(str)  # project_ns
    # #390: `takkub report publish --send` -> `push_report()` emits this so
    # `remote.notify.LeadNotifier` (the only thing holding a reference to the
    # SSE broadcaster) can push it to the connected mobile PWA as a native
    # attachment instead of the recipient tapping an external link. Orch
    # itself never imports `agent_takkub.remote` (remote-bolt-on-isolation
    # contract) — this signal is the one-way bridge, same shape as
    # `agentDone`/`leadNotified` above, which `LeadNotifier` already connects
    # to the same way.
    reportShared = pyqtSignal(str, dict)  # project_ns, {name,url,label,size_bytes,attachment}
    # UI-only session-cap notice. Lead crossings are never injected back into
    # Lead or auto-compacted; MainWindow surfaces the decision to the user.
    # Teammate crossings also emit this after their safe-idle advisory is queued.
    sessionCapNotice = pyqtSignal(str, str, int, int, bool)
    # project_ns, role, prompt, threshold, is_lead
    # UI-only idle notice. The routine reminder uses this cockpit-side channel
    # for every provider (#103) instead of writing+Enter into a provider PTY.
    # `escalated` is true only on the one round that also receives the retained
    # last-resort PTY reminder.
    idleReminderNotice = pyqtSignal(str, str, int, bool)
    # project_ns, role, notice_round, escalated
    # Emitted after every successful Task Ledger (A7) write — assign/done/
    # fail/close. The Task Tree dock (A8) connects this straight to its
    # refresh_project(project_ns) slot instead of polling the state file, so
    # the dock repaints the instant the write it's showing actually lands.
    ledgerChanged = pyqtSignal(str)  # project_ns
    # #365 phase 2 — a pane's "Open in Takkub" click (terminal path, or
    # Explorer). register_pane binds project_name via closure over each
    # pane's openInEditorRequested; main_window routes this to EditorHost.
    openFileInEditorRequested = pyqtSignal(str, str)  # project, absolute path
    # #365 phase 5 — mirrors PreviewController's own opened/updated/closed
    # signals one level up, so main_window/the future preview_widget.py can
    # connect once to the Orchestrator instead of reaching into
    # `self._preview_controller` directly. `state` is a `PreviewState | None`
    # (None only for the closed case's own dedicated signal below).
    previewOpened = pyqtSignal(str, object)  # project, PreviewState
    previewUpdated = pyqtSignal(str, object)  # project, PreviewState
    previewClosed = pyqtSignal(str)  # project

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Browser MCPs (playwright + chrome-devtools) follow Lead into
        # every project. Merge them into runtime/shared-mcp.json before
        # any pane spawns — the orchestrator will then hand the file to
        # claude via `--mcp-config` and panes pick the servers up
        # uniformly across projects. Idempotent: safe to call on every
        # boot. Failure is non-fatal (logged once and panes spawn
        # without browser MCPs) so a readonly runtime never blocks
        # cockpit startup.
        try:
            from .shared_dev_tools import ensure_browser_mcps, warm_browser_mcps

            ok, msg = ensure_browser_mcps()
            _log_event("browser_mcp_init", ok=ok, msg=msg)
            # Kick the browser MCP servers in background daemon threads
            # so the npx cache is hot before claude tries to spawn them
            # lazily on first tool call. Non-blocking; failure here is
            # logged at the helper level and the MCPs still work on
            # the slower first call without warm-up.
            warm_browser_mcps()
        except Exception as e:
            _log_event("browser_mcp_init_error", error=repr(e))
        # graft (code-intelligence MCP) follows Lead into every project the
        # same way the browser MCPs do — separate ensure/warm calls so a
        # failure here can never affect browser MCP init above or below.
        try:
            from .shared_dev_tools import ensure_graft_mcp, warm_graft_mcp

            ok, msg = ensure_graft_mcp()
            _log_event("graft_mcp_init", ok=ok, msg=msg)
            warm_graft_mcp()
        except Exception as e:
            _log_event("graft_mcp_init_error", error=repr(e))
        # Auto-run `graft build` for every project so the MCP above actually
        # has a graph to answer from instead of returning graceful-but-empty
        # results until the user finds out they need to run the CLI by hand.
        # Background threads, capped concurrency, per-dir single-flight — see
        # graft_autobuild.py. Non-fatal: a build failure here never blocks
        # cockpit startup.
        try:
            from .graft_autobuild import build_all_projects_async

            build_all_projects_async()
        except Exception as e:
            _log_event("graft_autobuild_boot_error", error=repr(e))
        # Merge user's ~/.claude.json mcpServers (obsidian-vault, etc.)
        # into shared-mcp.json so every pane inherits them automatically.
        # Browser MCP entries win on name collision. Non-fatal: failure logs
        # once and panes spawn without user MCPs until the issue is resolved.
        try:
            from .shared_dev_tools import ensure_user_mcps

            ok, msg = ensure_user_mcps()
            _log_event("user_mcp_init", ok=ok, msg=msg)
        except Exception as e:
            _log_event("user_mcp_init_error", error=repr(e))
        # Reclaim disk: prune stale per-pane PTY transcripts so runtime/sessions
        # can't grow without bound (a runaway pane once left a 203 MB log).
        # Best-effort and non-fatal — a readonly runtime never blocks startup.
        try:
            prune_old_transcripts()
        except Exception as e:
            _log_event("transcript_prune_error", error=repr(e))
        # Reclaim disk: prune stale per-(project, role, shard, browser) Chromium
        # profile dirs (#39 fan-out) so runtime/browser-profiles/ can't grow
        # without bound (#42). Safe here: no pane is alive yet at startup, so no
        # browser owns a profile, and recently-used login profiles have a fresh
        # mtime and survive the age window. Best-effort / non-fatal.
        try:
            from .shared_dev_tools import prune_old_browser_profiles

            prune_old_browser_profiles()
        except Exception as e:
            _log_event("browser_profile_prune_error", error=repr(e))
        # Reclaim disk: sweep ORPHAN worktree checkouts only — dirs under
        # runtime/../worktrees/ that `git worktree list` no longer knows about
        # (crashed cockpit, deleted .git pointer). Never touches a still-
        # registered worktree (that's `takkub worktree clean`'s job, explicit
        # opt-in). Conservative within "orphan" too (#132): only an empty
        # checkout or one containing nothing but node_modules, clean, with no
        # branch commits still unmerged is eligible — an orphan still holding
        # source/uncommitted/unmerged content is left alone for the Lead to
        # review via `takkub prune --level review --category
        # orphan-worktrees-review --yes`. Safe at boot for the same reason as
        # the two prunes above: no pane is alive yet to be using one.
        # Best-effort / non-fatal.
        try:
            from .disk_usage import prune_orphan_worktrees_boot

            removed = prune_orphan_worktrees_boot()
            if removed:
                _log_event("orphan_worktree_prune", removed=removed)
        except Exception as e:
            _log_event("orphan_worktree_prune_error", error=repr(e))
        # Reconcile task-ledger rows orphaned by a cockpit that exited
        # without ever calling `takkub done` (issue #166 — previously stuck
        # "working" forever since `mark_done` only fires from a live pane's
        # done/close handler). Safe at boot for the same "no pane alive yet"
        # reason as the two prunes above — but the ledger's own date<today
        # gate (`task_ledger._orphan_candidates`) is the one that actually
        # matters here: it's what stops this from closing a same-day row an
        # auto-respawn (2s later, once panes exist) is about to resume.
        # Best-effort / non-fatal — a readonly runtime never blocks startup.
        try:
            from . import task_ledger

            for _proj in task_ledger.list_projects_with_ledger():
                _closed, _warn = task_ledger.reconcile_orphaned(_proj, frozenset())
                if _closed:
                    _log_event("ledger_reconcile_boot", project=_proj, closed=_closed)
                if _warn:
                    _log_event("ledger_reconcile_boot_warning", project=_proj, warning=_warn[:200])
        except Exception as e:
            _log_event("ledger_reconcile_boot_error", error=repr(e))
        # PaneRegistry groups all 7 spawn-engine state dicts under one object.
        # Backward-compat properties on SpawnEngineMixin let every existing
        # access site (self._pane_state[...] etc.) work unchanged.
        # Lifecycle notes are in PaneRegistry's docstring (spawn_engine.py).
        self._registry: PaneRegistry = PaneRegistry()
        # Windows mini-browser uses a fixed CDP endpoint. The process is
        # started lazily for an eligible browser pane and stopped explicitly
        # by the GUI/headless teardown paths.
        from .browser_chrome import NativeChromeManager

        self._native_chrome = NativeChromeManager()
        # #365 phase 5: per-project Live Preview state. One controller for
        # the whole app (RAM rule, master plan §4) — the eventual
        # `preview_widget.py` (frontend, not built yet) will connect its
        # opened/updated/closed signals; today `preview_command` below is
        # the only caller, via `takkub preview` IPC.
        from .preview_controller import PreviewController

        self._preview_controller = PreviewController(self)
        self._preview_controller.opened.connect(self.previewOpened)
        self._preview_controller.updated.connect(self.previewUpdated)
        self._preview_controller.closed.connect(self.previewClosed)
        # #365 phase 10: `takkub doctor --workspace` diagnostic sources.
        # `_editor_host_ref` mirrors `_spawn_gate_pred`'s injection pattern
        # (see that field's comment below) — MainWindow owns the one
        # app-wide EditorHost and registers it via `set_editor_host` after
        # constructing it; None here (and in every headless/test path) is a
        # normal "no live window" state, not an error.
        # `_workspace_diag_sources` is project → {source_key: live object},
        # registered/unregistered by main_window as ProjectTab explorers
        # come and go (`register_workspace_diag_source` /
        # `unregister_workspace_diag_sources` below) — a source with a
        # `.diagnostics()` method (FileWatchService/GitChangesService/
        # ProjectFileIndex all have one) is read directly by
        # `workspace_status`; one that never got instantiated for a given
        # project simply isn't in the dict, which `workspace_status`
        # reports as "not wired", never as a failure.
        self._editor_host_ref = None
        self._workspace_diag_sources: dict[str, dict[str, object]] = {}
        # Peer CC durability: messages queued when Lead is not alive.
        # Keyed by project namespace; flushed to Lead on next Lead spawn.
        self._pending_lead_cc: dict[str, list[dict]] = {}
        self._load_pending_cc()
        # Done-notice durability: `takkub done` notices queued when Lead is
        # not alive at the moment a teammate finishes. Pattern mirrors
        # _pending_lead_cc; flushed to Lead on next Lead spawn AND persisted to
        # disk so a teammate's done report survives a cockpit restart while the
        # Lead is down (issue #13).
        self._pending_done_notices: dict[str, list[dict]] = {}
        self._notice_deduper = NoticeDeduper(RUNTIME_DIR / "notice-dedupe.json")
        # Per-project timestamp of when the reaper first saw pending notices it
        # could not flush because the Lead read as not-ready. Drives the
        # staleness escalation that force-delivers when is_at_ready_prompt() is a
        # perpetual false-negative (e.g. a blocker marker in the Lead's visible
        # conversation reads as busy — #70/#20). Cleared on successful flush.
        self._pending_done_since: dict[str, float] = {}
        self._load_pending_done_notices()
        # Fan-out queue (flag-gated): per-project deque of deferred over-cap
        # assigns. Persisted to disk so a still-queued assign survives a cockpit
        # restart (parity with _pending_done_notices above). Loaded only when
        # TAKKUB_QUEUE_FANOUT is on, so a stale file is ignored when the feature
        # is off. See docs/reviews/2026-06-30-queue-gap.md.
        self._fanout_queue: dict = {}
        self._load_fanout_queue()
        # Global/per-project admission controller. Sampling is non-blocking and
        # dispatch callbacks run on this Qt thread, preserving orchestrator state
        # ownership while preventing multi-project fan-out from saturating the host.
        self._resource_governor = ResourceGovernor(
            event_sink=lambda event, details: _log_event(event, **details)
        )
        # Reliability v2 forbids blind task-body replay. Verification may
        # retry Enter, but a fresh delivery requires a fresh delivery ID.
        self._allow_automated_repaste = False
        self._resource_tokens: dict[tuple[str, str], ResourceToken] = {}
        self._main_thread_stall_count = 0
        self._latest_main_thread_stall: dict = {}
        self._resource_timer = QTimer(self)
        self._resource_timer.setInterval(1000)
        self._resource_timer.timeout.connect(self._tick_resource_governor)
        self._resource_timer.start()
        # In-memory serialisation queue for live Lead writes (ready-prompt aware).
        # Keyed by project namespace.  Items are string bodies; a single pump
        # fires per project so concurrent done notices never overwrite each other
        # mid-generation.  Lead-absent items fall through to _pending_done_notices.
        self._lead_notify_queue: dict[str, collections.deque] = {}
        # Clean done/peer-CC notices wait here for the configurable debounced
        # Lead Inbox Digest window. _digest_timer stores the latest generation
        # token per project so stale singleShot callbacks are harmless.
        self._lead_digest_queue: dict[str, collections.deque] = {}
        self._digest_timer: dict[str, int] = {}
        self._lead_notify_pumping: set[str] = set()
        # Busy-retry counter per project_ns; reset on delivery or Lead-dies path.
        self._lead_notify_retry: dict[str, int] = {}
        # #279: wall-clock the CURRENT busy streak started for this project's
        # Lead. `_pump_lead_notify` prefers an idle Lead for
        # `_LEAD_BUSY_DELIVER_AFTER_S` and then delivers into the busy one
        # anyway — an autonomous Lead is almost never idle, so waiting for the
        # ready prompt used to cost ~90 s per report via the spill → reaper →
        # force-flush chain. Cleared together with _lead_notify_retry by
        # LeadInboxMixin._reset_lead_notify_backoff.
        self._lead_notify_busy_since: dict[str, float] = {}
        # #133: project_ns currently has an in-flight _delayed_enter_verified
        # submit-verify chain writing to the Lead session. Both
        # _pump_lead_notify (next queued item) and _force_deliver_done_notices
        # (staleness escalation) check this before writing so two chains can
        # never race into the same Lead composer at once — the proven cause of
        # the fan-out corruption (events.log showed duplicate
        # `remaining: 3` lead_notify_repaste entries, meaning two independent
        # chains, not one bursty one). Cleared by the chain's on_settled.
        self._lead_notify_verify_active: set[str] = set()
        # Shard fan-out groups: keyed f"{project_ns}::{base_role}".
        # Created on first shard assign, closed when all N shards report.
        self._shard_groups: dict[str, ShardGroup] = {}
        # Pipeline runs: keyed f"{project_ns}::{run_id}".
        # Created by run_pipeline(); closed when last hop completes.
        self._pipeline_runs: dict[str, PipelineRun] = {}
        # Session objective per project (issue #50). Set by Lead via
        # `takkub goal "<objective>"`; prepended to every subsequent
        # `assign` task so parallel teammates share the big picture and
        # don't drift on scope. Volatile (never persisted) and per-project
        # so a goal set in one tab never leaks into another.
        self._session_goals: dict[str, str] = {}
        # Lead draft-typing guard (#3, 2026-07-09 core-upgrade plan): tracks
        # whether each project's Lead pane input line currently holds
        # unsubmitted user text, so _pump_lead_notify / _flush_pending_lead_cc
        # never paste an engine message on top of a draft the user hasn't
        # submitted yet. Fed by _on_pane_input via
        # LeadInboxMixin._track_lead_draft_input; see lead_inbox.py.
        self._lead_draft_state: dict[str, LeadDraftState] = {}
        # #265: wall-clock of the most recent byte `_on_pane_input` saw the
        # Lead pane's owner actually type (submitted or still drafting —
        # either way proves they're about to act). Stamped from the exact
        # same choke point as `_lead_draft_state` above, so it inherits the
        # same guarantee: engine-originated writes (notices/tasks) go
        # straight to `session.write()` and never pass through here, so
        # they can never be mistaken for the owner interrupting. Unlike
        # `_lead_draft_state` (which resets to "empty"/pending_since=0.0 the
        # instant Enter submits a line), this timestamp is never cleared —
        # `LeadWaitMixin.poll_wait` needs "did the owner type ANYTHING
        # after this wait started", not "is a draft currently held".
        self._lead_last_user_input_ts: dict[str, float] = {}
        # #449: whether the MOST RECENT stamp above had any content left
        # after stripping every recognizable escape sequence (see
        # `_chunk_has_non_escape_content`) — i.e. whether it looks like real
        # typed/pasted text rather than a terminal auto-reply structure the
        # #357/#420/#428/#431 denylist doesn't recognize yet. A bare special
        # key (arrow/F-key) also reads False here, same as an unrecognized
        # auto-reply — both are genuinely ambiguous, which is exactly why
        # `LeadWaitMixin._pending_user_input_interrupt` hedges its wording on
        # this instead of asserting "you typed something" outright.
        self._lead_last_user_input_printable: dict[str, bool] = {}
        # #428/#431: when the owner's own keystroke last reached the Lead
        # PTY, per project. Compared against `session.last_write_ts` (stamped
        # by EVERY write, engine-originated included) so `_on_pane_input`
        # can tell "an ESC-led chunk right after the cockpit pasted a digest/
        # notice" (a terminal reply the auto-reply filter doesn't know yet)
        # from a genuine special key — see `_LEAD_INJECT_GRACE_S`.
        self._lead_last_user_write_ts: dict[str, float] = {}
        # #393: the last `_lead_last_user_input_ts` value that has already
        # fired a `poll_wait` "user_input" interrupt for this project — see
        # `LeadWaitMixin._pending_user_input_interrupt`'s docstring for why a
        # registration's `started_ts` alone isn't enough to stop the SAME
        # stamp from re-triggering across repeated `begin_wait` attach
        # cycles. Monotonic, never reset: a later genuine keystroke always
        # produces a strictly newer timestamp than whatever this holds.
        self._wait_user_input_ack_ts: dict[str, float] = {}

        # Per-cockpit-run capability token. Injected only into the Lead pane
        # env (TAKKUB_LEAD_TOKEN) so the Lead takkub CLI can authenticate
        # Lead-only server commands. Teammates don't get it — their CLI calls
        # will be rejected server-side even if they connect to the socket.
        # Generated fresh each boot; never written to disk, logs, or argv.
        self._lead_token: str = secrets.token_urlsafe(32)

        # Idle watchdog bookkeeping. Per-role:
        #   first_idle_ts   — when the pane was first seen idle in this streak
        #                     (None = currently processing or not "working")
        #   last_reminder_ts — last time we surfaced a reminder (0 = never)
        #   notice_rounds    — UI-only rounds in this continuous idle streak
        #   escalated        — whether the one-shot PTY fallback already fired
        # Kept as a separate dict (not in PaneState) because its key-presence
        # semantics ("absent = not tracking") are relied on by the watchdog and
        # tests — pop() must remove the entry, not merely reset fields.
        self._idle_state: dict[str, dict[str, float | int | bool | None]] = {}
        # Per-pane last-logged watchdog exception (err_str, ts) — dedups the
        # blind 5s-tick `idle_watchdog_pane_error` spam (was 3279 entries in one
        # events.log) so a persistent fault is logged once with detail, not
        # flooded. See _check_idle_teammates' except block.
        self._idle_err_last: dict[str, tuple[str, float]] = {}
        # Per-pane last-logged "ready marker possibly stale" timestamp — rate-
        # limits the #20 structural staleness detector (a pane alive + output-
        # quiet + matched by NO state marker = likely an upstream prompt reword
        # that silently broke detection). See _check_stale_markers.
        self._stale_marker_last: dict[str, float] = {}
        # Per-pane consecutive-occurrence counter for the same signal (#343) —
        # incremented once per routine log above, reset only on genuine
        # recovery (marker matches again, or the pane dies). Drives the
        # louder escalation in _check_stale_markers/_escalate_stale_marker.
        self._stale_marker_streak: dict[str, int] = {}
        # Per-pane auto-recovery-probe state (#343 follow-up), present only
        # between the sweep tick that fired the resize nudge and the NEXT
        # sweep tick that reads the post-redraw screen — see
        # _nudge_stale_marker (phase 1) / _resolve_stale_marker_nudge (phase
        # 2). A key's presence here means "awaiting phase 2", checked first
        # thing in _check_stale_markers so phase 2 runs unconditionally on
        # the very next tick, not gated behind the quiet/cooldown checks.
        self._stale_marker_nudged: dict[str, dict[str, object]] = {}
        # Per-project timestamp of "every non-Lead teammate stopped being
        # 'working'" (#412) — drives Lead's LEAD_DONE_IDLE_GRACE_S exemption
        # from the #343 stale-marker escalation above. Absent/0.0 means at
        # least one teammate is currently working, or the project has never
        # been observed all-idle yet. See `_team_idle_since_for`.
        self._team_idle_since: dict[str, float] = {}
        self._idle_watchdog = QTimer(self)
        self._idle_watchdog.setInterval(IDLE_WATCHDOG_INTERVAL_MS)
        self._idle_watchdog.timeout.connect(self._check_idle_teammates)
        if IDLE_REMIND_AFTER_S > 0:
            self._idle_watchdog.start()
        # Auto-resume (🌙): the signal-(b) confirm fetch runs in a background
        # thread and reports back via this signal so the park decision itself
        # always executes on the Qt thread.
        self.limitUsageConfirmed.connect(self._on_limit_usage_confirmed)

        # Periodic snapshot of cockpit state to `<vault>/hot.md`. Skipped
        # silently when no vault is configured (see `_resolve_vault_dir`).
        # In-process list of the last few `takkub done` events drives the
        # "Recent" section without hitting disk on every tick.
        self._recent_done: list[tuple[str, str, str]] = []
        # #241: fingerprints of notice bodies Lead has already pulled via
        # `takkub inbox`/`takkub wait` (see `_notice_fingerprint`). A later
        # Lead Inbox Digest flush collapses a matching item to a one-line
        # reference instead of re-pasting content Lead has already read.
        self._inbox_seen: dict[str, set[str]] = {}
        # #242: at most one active `takkub wait` registration per project —
        # a second concurrent `wait` call attaches to it (unions its role
        # set) instead of starting an independent poll loop, so stray
        # duplicate waiters can never multiply cockpit socket load.
        self._active_waits: dict[str, dict] = {}
        # #242: last-seen resolution (ts + failed flag) per (project_ns,
        # role) `done()` call, keyed independently of `_recent_done` so a
        # `wait` registration can tell "resolved before I started watching"
        # from "resolved just now" without racing the digest/live queues.
        self._wait_done_events: dict[tuple[str, str], dict] = {}
        # #249 follow-up: when a registration naturally concludes (every role
        # resolved, or the registration's own timeout elapsed) `poll_wait`
        # pops it — but two+ attached `takkub wait` clients each poll on
        # their own schedule, so only the poller whose tick happened to
        # observe the conclusion gets the real result; every OTHER attacher's
        # next poll used to find `active is None` and get a manufactured
        # "wait session no longer active" error for what was actually a
        # success. This caches that final result per project so a straggling
        # attacher's poll echoes the real terminal outcome instead — see
        # `poll_wait`'s docstring for the cancel/timeout-supersede exception.
        self._wait_resolved_echo: dict[str, dict] = {}
        self._hot_md_timer = QTimer(self)
        self._hot_md_timer.setInterval(_HOT_MD_INTERVAL_MS)
        self._hot_md_timer.timeout.connect(self._write_hot_md)
        self._hot_md_timer.start()

        # ── Spawn arbiter (3-layer gate + FIFO serialiser) ──────────
        # Predicate injected by main_window; returns True when Qt has a modal
        # or popup widget active (QDialog/QWizard/QMenu).  None = no guard
        # (tests, headless paths).  Win32 InSendMessageEx is always checked
        # directly inside _is_spawn_blocked() regardless of this predicate.
        self._spawn_gate_pred: Callable[[], bool] | None = None
        # _spawn_deferred, _spawn_queue, _spawn_in_progress → self._registry

    def close_native_chrome(self) -> None:
        """Stop Chrome only when this cockpit launched it (idempotent)."""
        manager = getattr(self, "_native_chrome", None)
        if manager is not None:
            manager.close()

    def _planted_context_cwd_in_use(self, cwd: str | None, exclude: tuple[str, str] | None) -> bool:
        """True while some OTHER live pane (session attached, not exited) runs
        in *cwd* — its planted AGENTS.md must stay until it goes too."""
        if not cwd:
            return False
        for project_ns, panes in getattr(self, "_panes_by_project", {}).items():
            for role, pane in panes.items():
                if exclude is not None and (project_ns, role) == exclude:
                    continue
                if getattr(pane, "session", None) is None:
                    continue
                if getattr(pane, "state", None) == "exited":
                    continue
                if getattr(pane, "_session_cwd", None) == cwd:
                    return True
        return False

    def _release_planted_context_if_unused(
        self, cwd: str | None, *, exclude: tuple[str, str] | None = None
    ) -> list[str]:
        """Remove the takkub-managed context files from *cwd* once no live
        pane needs them (see `codex_agents_md.remove_managed_context_files`
        for why). Best-effort: never raises into close()/exit paths."""
        if not cwd or self._planted_context_cwd_in_use(cwd, exclude):
            return []
        try:
            from .codex_agents_md import remove_managed_context_files

            removed = remove_managed_context_files(cwd)
        except Exception:
            return []
        if removed:
            _log_event("planted_context_removed", cwd=str(cwd), files=removed)
        return removed

    def release_all_planted_context(self) -> dict[str, list[str]]:
        """Cockpit-shutdown sweep (app.py `_kill_all`): every cwd any pane —
        Lead included — was spawned into gets its planted context files
        removed, since no pane will outlive the process to need them."""
        cwds: list[str] = []
        for panes in getattr(self, "_panes_by_project", {}).values():
            for pane in panes.values():
                cwd = getattr(pane, "_session_cwd", None)
                if cwd and cwd not in cwds:
                    cwds.append(cwd)
        out: dict[str, list[str]] = {}
        for cwd in cwds:
            try:
                from .codex_agents_md import remove_managed_context_files

                removed = remove_managed_context_files(cwd)
            except Exception:
                removed = []
            if removed:
                out[cwd] = removed
        if out:
            _log_event(
                "planted_context_removed",
                cwd="*",
                files=[name for names in out.values() for name in names],
            )
        return out

    def _native_chrome_in_use(self) -> bool:
        """True while any live pane is one that `spawn()` starts/reuses the
        cockpit-owned mb Chrome for (#406): a non-sharded browser role
        (`browser_chrome.should_manage_native_chrome`) whose session is still
        attached and not already in the `exited` state. Shards never share
        that Chrome (#92), so they don't keep it alive either."""
        from .browser_chrome import should_manage_native_chrome

        for panes in getattr(self, "_panes_by_project", {}).values():
            for role, pane in panes.items():
                base_role, shard_idx = _split_shard(str(role).lower().strip())
                if not should_manage_native_chrome(base_role, shard_idx):
                    continue
                if getattr(pane, "session", None) is None:
                    continue
                if getattr(pane, "state", None) == "exited":
                    continue
                return True
        return False

    def _schedule_native_chrome_idle_release(self) -> None:
        """#406: release the cockpit-owned mb Chrome once the LAST pane that
        could use it is gone, instead of holding it (measured ~500 MB working
        set across 8 Chrome processes, sitting on about:blank) until
        whole-app shutdown — the only place `close_native_chrome` was ever
        called before this.

        Debounced by `_NATIVE_CHROME_IDLE_GRACE_MS` rather than immediate:
        the stuck-pane watchdog closes and respawns the same qa/critic pane
        2 s later, a `done` auto-close is routinely followed by Lead
        assigning the next browser task within a minute, and a fresh Chrome
        launch is a multi-second boot per `NativeChromeManager.ensure_started`
        — so a short grace keeps the reuse path warm through those churns
        while an actually idle Chrome still goes away. Re-checks
        `_native_chrome_in_use()` when the timer fires so a pane spawned
        during the grace cancels the release without any bookkeeping.

        The kill itself (`taskkill /T /F` with a 5 s ceiling, then
        terminate/kill) runs on a daemon thread: a blocking subprocess on the
        Qt thread is exactly the Lead-pane input-lag class the
        `main_thread_stall` dumps in boot.log keep catching (same
        investigation that fixed ram_report's GIL hold). Windows-only in
        effect — `NativeChromeManager.close` is a no-op elsewhere."""
        manager = getattr(self, "_native_chrome", None)
        # Only a Chrome THIS cockpit launched is ours to kill (the reuse path
        # never owns one) — and only then is a grace timer worth arming: an
        # Orchestrator built in a test never launches Chrome, so it never
        # arms this timer and can't leak it past its owner (#344 tracker).
        if manager is None or not getattr(manager, "_owns_process", False):
            return
        timer = getattr(self, "_native_chrome_idle_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setObjectName("native-chrome-idle-release")
            timer.setSingleShot(True)
            timer.timeout.connect(self._release_native_chrome_if_idle)
            self._native_chrome_idle_timer = timer
        timer.start(_NATIVE_CHROME_IDLE_GRACE_MS)

    def _release_native_chrome_if_idle(self) -> None:
        manager = getattr(self, "_native_chrome", None)
        if manager is None or self._native_chrome_in_use():
            return
        _log_event("native_chrome_idle_release")
        threading.Thread(
            target=manager.close, name="native-chrome-idle-release", daemon=True
        ).start()

    def shutdown_timers(self) -> None:
        """Stop every QTimer this Orchestrator (recursively) owns (#344).

        __init__ arms _resource_timer / _idle_watchdog / _hot_md_timer
        unconditionally, so any Orchestrator() left reachable past its
        owner's lifetime keeps ticking under the process-wide QApplication —
        a leaked timer can fire against long-torn-down state and abort the
        process with no traceback.

        This is test/short-lived-owner tooling, not a hook production is
        missing. `self.orch = Orchestrator(self)` happens exactly once, in
        MainWindow.__init__ / HeadlessWindow.__init__ (each window's own
        lifetime == the process's), and neither ever replaces it with a
        fresh instance while running — so there is no live "old Orchestrator
        discarded, still reachable" moment for this to guard against.
        app.py's real teardown (`_kill_all`, wired to aboutToQuit/atexit/
        signals) always ends in process exit, which takes every timer with
        it regardless. Call this only where an Orchestrator is built and
        then dropped without the process ending — tests, mainly.

        Deterministic `findChildren(QTimer)` sweep (#345 — mirrors
        CliServer.shutdown_timers), not just the 3 named attrs above: also
        catches transient per-call watchdogs parented to `self` but never
        stored as an attribute — e.g. `_check_uncommitted_async`'s git-status
        timeout and `_run_boot_diagnostic_async`'s boot-diagnostic timeout
        (lead_inbox.py) — which a test exercising just that one call path
        leaves armed with no attribute name to stop by hand. `self` has no
        other QObject parented as a child (`_native_chrome` is constructed
        parentless), so this never reaches into an unrelated owner's timers.
        """
        for timer in self.findChildren(QTimer):
            timer.stop()

    # ──────────────────────────────────────────────────────────────
    # project-aware view onto the pane registry  (SpawnEngineMixin provides _ps + spawn methods)
    # ──────────────────────────────────────────────────────────────
    @staticmethod
    def _resolve_project(project: str | None) -> str:
        """Pick a namespace key. Resolves None to the currently active
        project from projects.json, falling back to a sentinel "default"
        when no project is configured (typical in unit tests)."""
        if project:
            validate_name(project, "project")  # raises ValueError on traversal attempts
            return project
        name, _ = active_project()
        return name or "default"

    def _project_panes(self, project: str | None = None) -> dict[str, AgentPaneLike]:
        """Return (and lazily create) the inner pane dict for `project`.

        Always returns the same dict instance for a given project, so
        callers can hold a reference and mutate it directly — that's how
        `self.panes` works for the active project."""
        return self._panes_by_project.setdefault(self._resolve_project(project), {})

    def _project_ns_for_pane(self, pane: AgentPaneLike) -> str | None:
        """Reverse-lookup: which project namespace owns *pane* (identity
        match). Needed because every project's Lead pane shares
        ``role.name == LEAD.name``, so a role-name lookup alone can't tell
        which project's draft tracker a Lead keystroke belongs to (issue #3).
        """
        for project_ns, panes in self._panes_by_project.items():
            if panes.get(pane.role.name) is pane:
                return project_ns
        return None

    def iter_all_panes(self) -> Iterator[tuple[str, AgentPaneLike]]:
        """Yield (role, pane) for every pane across every project namespace.

        Public replacement for UI code reaching into
        ``orch._panes_by_project`` directly — callers that need every
        teammate/Lead regardless of which project tab is active (cockpit
        restart confirmation, claude.exe liveness count, app-quit teardown)
        should use this instead of touching the private registry.
        """
        for project_panes in self._panes_by_project.values():
            yield from project_panes.items()

    @property
    def panes(self) -> dict[str, AgentPaneLike]:
        """Active project's pane dict. Backwards-compatible with the
        pre-Phase-1 single-namespace API — existing callers that read or
        write `orch.panes["backend"]` continue to operate on the active
        project's panes without knowing about the project dimension."""
        return self._project_panes()

    # ── spawn / registration methods provided by SpawnEngineMixin ──
    # ── Session goal (issue #50) ────────────────────────────────────
    def set_session_goal(self, text: str, project: str | None = None) -> tuple[bool, str]:
        """Set the session objective for `project`. Prepended to every
        subsequent `assign` task so teammates share the big picture."""
        project_ns = self._resolve_project(project)
        text = (text or "").strip()
        if not text:
            return False, "empty goal — pass an objective string, or use --clear to unset"
        if len(text) > _SESSION_GOAL_MAX:
            text = text[:_SESSION_GOAL_MAX].rstrip() + "\n…(goal truncated)"
        self._session_goals[project_ns] = text
        _log_event("goal-set", goal_preview=text[:120], project=project_ns)
        preview = text if len(text) <= 80 else text[:77] + "…"
        return True, f"goal set: {preview}"

    def clear_session_goal(self, project: str | None = None) -> tuple[bool, str]:
        """Unset the session objective for `project`."""
        project_ns = self._resolve_project(project)
        had = self._session_goals.pop(project_ns, None)
        if had is None:
            return True, "no goal was set"
        _log_event("goal-clear", project=project_ns)
        return True, "goal cleared"

    def _on_session_cap_exceeded(self, pane: AgentPaneLike, prompt: int, threshold: int) -> None:
        """Log + surface one edge-triggered cap crossing (audit + passive notice).

        The CLI's own auto-compact already handles context pressure, so this
        never writes into the pane's PTY — it only emits a UI notice (for the
        status bar) and an audit log line. Lead and teammates are treated the
        same; `is_lead` only affects notice wording downstream.
        """
        project_ns = self._project_ns_for_pane(pane)
        if project_ns is None:
            return
        role_name = pane.role.name
        is_lead = role_name == LEAD.name
        self.sessionCapNotice.emit(project_ns, role_name, prompt, threshold, is_lead)
        _log_event(
            "session_cap_crossed",
            project=project_ns,
            role=role_name,
            prompt=prompt,
            threshold=threshold,
        )

    def get_session_goal(self, project: str | None = None) -> str | None:
        """Return the current session objective for `project`, or None."""
        project_ns = self._resolve_project(project)
        return self._session_goals.get(project_ns)

    def _apply_session_goal(self, task: str, project_ns: str) -> str:
        """Prepend the session-goal context block to `task` when one is set.

        No-op when no goal exists, or when the task already carries the
        block (idempotent — guards against double-prepend on auto-respawn
        replay, which re-sends the stored last_assigned_task)."""
        goal = getattr(self, "_session_goals", {}).get(project_ns)
        if not goal:
            return task
        if _SESSION_GOAL_HEADER in task:
            return task
        return f"{_SESSION_GOAL_HEADER}\n{goal}\n\n{task}"

    def _register_subagent(
        self,
        role_name: str,
        cwd: str | None,
        task: str,
        *,
        requires_commit: bool = False,
        auto_chain: bool = False,
        shard_total: int = 0,
        isolation: str = "shared",
        project: str | None = None,
        feature: str = "",
    ) -> tuple[bool, str]:
        """Register a native child without opening a cockpit pane (#268).

        The Lead process owns native-subagent execution; the cockpit owns the
        durable task capsule and completion bookkeeping.  This boundary keeps
        provider-native execution in-process while preserving the ledger,
        inbox and wait semantics of a normal ``done`` report.
        """
        try:
            role_name = validate_name(role_name, "role")
        except ValueError as exc:
            return False, str(exc)
        if role_name == LEAD.name:
            return False, "lead cannot assign itself as a subagent"
        project_ns = self._resolve_project(project)
        pending = getattr(self, "_subagent_assignments", None)
        if pending is None:
            pending = self._subagent_assignments = {}
        key = (project_ns, role_name)
        if key in pending:
            return (
                False,
                f"subagent assignment already pending for {role_name}; use {role_name}#N for fan-out",
            )

        base_role = _split_shard(role_name)[0]
        run_cwd = cwd or default_cwd_for_role(base_role, project=project_ns)
        worktree = None
        if isolation == "worktree":
            from .worktree_manager import WorktreeManager, load_worktree_config

            if run_cwd:
                info, warning = WorktreeManager().create(
                    run_cwd, project_ns, role_name, int(time.time()), exclude_ports=set()
                )
            else:
                info, warning = None, "ไม่มี cwd ให้สร้าง worktree (ระบุ --cwd)"
            if info is not None:
                cfg, _ = load_worktree_config(info.git_root)
                run_cwd = info.path
                worktree = info.as_dict()
                task = _append_worktree_hint(task, info.branch, cfg.post_create, info.port)
            else:
                self._notify_lead(
                    project_ns,
                    f"⚠️ [{role_name}] worktree isolation ใช้ไม่ได้ → subagent ใช้ shared cwd · {warning}",
                    from_role=role_name,
                    note="",
                    kind="worktree-fallback-subagent",
                )

        task = self._apply_session_goal(task, project_ns)
        task_id = _uuid.uuid4().hex
        capsule = (
            f"[ROLE: {role_name} — Lead assigned this task with `takkub assign --mode subagent`]\n"
            "You are a native subagent of the Lead's current provider. Work directly in the cwd below; "
            "do not claim independent-provider or cross-model verification.\n"
            f"cwd: {run_cwd or '(current project cwd)'}\n\n{task}\n\n"
            f'When finished, run: takkub subagent-done --role {role_name} "<one-line summary>"\n'
            "For analysis/review, save detailed findings under docs/ before that completion command."
        )
        capsule_dir = _task_handoff_dir(project_ns)
        capsule_path = capsule_dir / (
            f"{datetime.now().strftime('%H%M%S')}-{role_name}-subagent-{task_id[:8]}.md"
        )
        try:
            capsule_path.write_text(capsule, encoding="utf-8")
        except OSError as exc:
            return False, f"could not write subagent task capsule: {exc}"

        from .provider_config import effective_provider_for

        parent_provider = effective_provider_for(LEAD.name, project=project_ns)
        pending[key] = {
            "task": task,
            "task_id": task_id,
            "cwd": run_cwd,
            "assign_ts": time.time(),
            "requires_commit": requires_commit,
            "auto_chain": auto_chain,
            "shard_total": shard_total,
            "worktree": worktree,
            "provider": parent_provider,
            "capsule": str(capsule_path),
        }
        try:
            from .task_ledger import create_assignment

            warning = create_assignment(
                project_ns,
                role_name,
                run_cwd,
                task,
                self.get_session_goal(project=project_ns),
                feature,
                parent_provider,
            )
            if warning:
                self._notify_lead(
                    project_ns, warning, from_role=role_name, note="", kind="ledger-warning"
                )
            self.ledgerChanged.emit(project_ns)
        except Exception:
            _log_event(
                "ledger_hook_error", role=role_name, project=project_ns, stage="subagent-assign"
            )

        if shard_total > 0:
            group_key = f"{project_ns}::{base_role}"
            if group_key not in self._shard_groups:
                self._shard_groups[group_key] = ShardGroup(base_role=base_role, total=shard_total)
            else:
                self._shard_groups[group_key].total = shard_total
        _log_event(
            "assign_subagent",
            role=role_name,
            project=project_ns,
            effective_provider=parent_provider,
            capsule=str(capsule_path),
        )
        forward = str(capsule_path).replace(os.sep, "/")
        return True, (
            f"subagent registered for {role_name} (provider={parent_provider}, no pane). "
            f"Dispatch one native subagent with task capsule: {forward}; completion command is inside. "
            "This mode is same-provider only and is not a cross-model check."
        )

    def subagent_done(
        self, role_name: str, note: str = "", project: str | None = None, failed: bool = False
    ) -> tuple[bool, str]:
        """Complete a pending native child through done-equivalent sinks (#268)."""
        try:
            role_name = validate_name(role_name, "role")
        except ValueError as exc:
            return False, str(exc)
        project_ns = self._resolve_project(project)
        pending = getattr(self, "_subagent_assignments", {})
        state = pending.pop((project_ns, role_name), None)
        if state is None:
            return False, f"no pending subagent assignment for {role_name}"
        note = (note or "").strip()
        now = datetime.now()
        try:
            from .task_ledger import mark_done

            warning = mark_done(project_ns, role_name, "fail" if failed else "ok", ts=now)
            if warning:
                self._notify_lead(
                    project_ns, warning, from_role=role_name, note="", kind="ledger-warning"
                )
            self.ledgerChanged.emit(project_ns)
        except Exception:
            _log_event(
                "ledger_hook_error", role=role_name, project=project_ns, stage="subagent-done"
            )

        session_path = self._save_decision_note(project_ns, role_name, note, now=now, failed=failed)

        # Core V2 Conversation hook (#309 Phase 6) — see done()'s identical
        # comment. Subagents have no pane/PTY, so there is no cwd/session_id
        # to pass through for transcript ingest; the note itself still gets
        # recorded as a conversation message + rolling-summary update.
        try:
            from .core.conversation.flag import v2_conversation_enabled

            if v2_conversation_enabled():
                import threading

                from .core.conversation.facade import on_pane_done as _cv_on_pane_done

                threading.Thread(
                    target=_cv_on_pane_done,
                    args=(project_ns, role_name),
                    kwargs={"note": note, "failed": failed},
                    daemon=True,
                ).start()
        except Exception:
            _log_event(
                "conversation_v2_hook_error",
                role=role_name,
                project=project_ns,
                stage="subagent-done",
            )

        # Core V2 Second Brain Reflection hook (#309 Phase 7c) — see done()'s
        # identical comment. Subagents never compute DigestFacts (no pane/PTY,
        # no worktree git-state digest), so only the note-based heuristic
        # candidate applies here.
        try:
            from .core.brain.flag import v2_brain_enabled

            if v2_brain_enabled():
                import threading

                from .core.brain.facade import on_pane_done as _brain_on_pane_done

                threading.Thread(
                    target=_brain_on_pane_done,
                    args=(project_ns, role_name),
                    kwargs={"note": note, "failed": failed},
                    daemon=True,
                ).start()
        except Exception:
            _log_event(
                "brain_v2_hook_error", role=role_name, project=project_ns, stage="subagent-done"
            )

        body = note if failed else self._condense_done_note(note, note, "", session_path)
        notice = (
            self._build_verify_fail_handoff(role_name, note)
            if failed
            else f"[{role_name} done · subagent] {body}".rstrip()
        )
        shard_total = int(state.get("shard_total", 0) or 0)
        # Match pane done semantics: clean shard reports are consolidated;
        # failures surface immediately as blocking notices.
        if failed or shard_total == 0:
            self._notify_lead(
                project_ns, notice, from_role=role_name, note=note, kind="subagent-done"
            )
        if state.get("requires_commit") and not state.get("worktree") and state.get("cwd"):
            self._check_uncommitted_async(project_ns, role_name, state["cwd"])
        if state.get("worktree"):
            self._finalize_worktree(project_ns, role_name, state["worktree"])
        if state.get("auto_chain") and not any(
            p == project_ns and other.get("auto_chain") for (p, _role), other in pending.items()
        ):
            self._maybe_fire_auto_chain_handoff(project_ns, True)

        if shard_total > 0:
            base_role = _split_shard(role_name)[0]
            group_key = f"{project_ns}::{base_role}"
            group = self._shard_groups.get(group_key)
            if group and not group.closed:
                if failed:
                    group.failed.add(role_name)
                    group.failed_notes[role_name] = body
                else:
                    group.done[role_name] = body
                if len(group.done) + len(group.failed) >= group.total:
                    group.closed = True
                    self._inject_shard_fanout_handoff(project_ns, group)
                    self._shard_groups.pop(group_key, None)

        stamp = now.strftime("%Y-%m-%dT%H%M%S")
        self._recent_done.insert(0, (project_ns, role_name, f"{stamp}-{role_name}.md"))
        del self._recent_done[20:]
        getattr(self, "_wait_done_events", {})[(project_ns, role_name)] = {
            "ts": now.timestamp(),
            "failed": bool(failed),
        }
        self._write_hot_md()
        self.agentDone.emit(project_ns, role_name, note)
        _log_event("done_subagent", role=role_name, project=project_ns, note=note[:200])
        return True, f"{role_name} subagent reported done"

    def assign(
        self,
        role_name: str,
        cwd: str | None,
        task: str,
        requires_commit: bool = False,
        auto_chain: bool = False,
        shard_total: int = 0,
        plan: bool = False,
        isolation: str = "shared",
        project: str | None = None,
        feature: str = "",
        model: str | None = None,
        provider: str | None = None,
        effort: str | None = None,
        mode: str = "pane",
        _resource_token: ResourceToken | None = None,
        worktree_prepared: tuple | None = None,
    ) -> tuple[bool, str]:
        """*worktree_prepared* (#408): ``(WorktreeInfo | None, reason)`` from a
        `WorktreeManager.create` the caller already ran OFF the Qt thread
        (`cli_server` does this for `--isolation worktree`, fed by
        `worktree_assign_inputs`). When given, `_assign_with_worktree` uses
        it instead of running `git worktree add` inline on the main thread."""
        if mode not in {"pane", "subagent"}:
            return False, "mode must be pane or subagent"
        if mode == "subagent":
            if model:
                return False, "model override is not supported in subagent mode"
            if provider:
                return False, "provider override is not supported in subagent mode"
            if effort:
                return False, "effort override is not supported in subagent mode"
            if plan:
                return False, "plan mode is not supported in subagent mode"
            return self._register_subagent(
                role_name,
                cwd,
                task,
                requires_commit=requires_commit,
                auto_chain=auto_chain,
                shard_total=shard_total,
                isolation=isolation,
                project=project,
                feature=feature,
            )
        provider = (provider or "").strip().lower() or None
        if provider:
            from .provider_config import assign_provider_override_error

            provider_error = assign_provider_override_error(provider)
            if provider_error:
                return False, provider_error
        model = (model or "").strip() or None
        if model:
            from .provider_config import assign_model_override_error

            model_error = assign_model_override_error(
                role_name, model, project, provider_override=provider
            )
            if model_error:
                return False, model_error
        effort = (effort or "").strip().lower() or None
        if effort:
            from .provider_config import assign_effort_override_error

            effort_error = assign_effort_override_error(
                role_name, effort, project, provider_override=provider
            )
            if effort_error:
                return False, effort_error
        # #162: a repeated `--isolation worktree` assign at the same BARE role
        # name (no `#N`) doesn't get its own isolated pane — it silently
        # collides with the pane the earlier call already owns (see
        # `_worktree_bare_role_collision` docstring). Reject before touching
        # anything so the caller sees it instead of two tasks quietly racing.
        if isolation == "worktree":
            collision_err = self._worktree_bare_role_collision(role_name, project)
            if collision_err:
                return False, collision_err
        project_ns = self._resolve_project(project)
        resource_key = (project_ns, role_name)
        governor = getattr(self, "_resource_governor", None)
        if governor is None:
            governor = self._resource_governor = ResourceGovernor(
                event_sink=lambda event, details: _log_event(event, **details)
            )
        if not hasattr(self, "_resource_tokens"):
            self._resource_tokens = {}
        if _resource_token is not None:
            self._resource_tokens[resource_key] = _resource_token
        elif resource_key not in self._resource_tokens:
            resource_class = classify_resource(role_name, task)
            task_id = _uuid.uuid4().hex
            decision = governor.request_slot(
                project_id=project_ns,
                pane_id=role_name,
                task_id=task_id,
                resource_class=resource_class,
            )
            if not decision.allowed:
                # #303 item 1: write the task ledger's detail file the moment
                # the task is QUEUED, not only once a pane finally spawns for
                # it — before this, a gate-blocked assign left absolutely
                # nothing on disk under runtime/tasks/ until admission, so
                # there was no file Lead could read or edit while it waited
                # (only `runtime/events.log`, and even that gave no way to
                # act). Best-effort: a write failure here degrades to the
                # in-memory `task` text below, same as the working-status
                # ledger write already does.
                base_role_q = _split_shard(role_name)[0]
                detail_path = None
                try:
                    from .task_ledger import create_assignment as _create_ledger_row

                    ledger_cwd_q = cwd or default_cwd_for_role(base_role_q, project=project_ns)
                    ledger_warning_q, detail_path = _create_ledger_row(
                        project_ns,
                        role_name,
                        ledger_cwd_q,
                        task,
                        self.get_session_goal(project=project_ns),
                        feature,
                        provider or "",
                        status="queued",
                    )
                    if ledger_warning_q:
                        self._notify_lead(
                            project_ns,
                            ledger_warning_q,
                            from_role=role_name,
                            note="",
                            kind="ledger-warning",
                        )
                    self.ledgerChanged.emit(project_ns)
                except Exception:
                    _log_event(
                        "ledger_hook_error", role=role_name, project=project_ns, stage="enqueue"
                    )
                governor.enqueue(
                    project_id=project_ns,
                    pane_id=role_name,
                    task_id=task_id,
                    resource_class=resource_class,
                    reason=decision.reason,
                    on_admitted=lambda token, r=role_name, c=cwd, t=task, rc=requires_commit, ac=auto_chain, st=shard_total, pl=plan, iso=isolation, p=project, f=feature, m=model, pr=provider, ef=effort, dp=detail_path, wp=worktree_prepared: (
                        self.assign(
                            r,
                            c,
                            self._latest_queued_task_text(dp, t),
                            requires_commit=rc,
                            auto_chain=ac,
                            shard_total=st,
                            plan=pl,
                            isolation=iso,
                            project=p,
                            feature=f,
                            model=m,
                            provider=pr,
                            effort=ef,
                            _resource_token=token,
                            # #408: a worktree prepared off-thread for this
                            # assign must not be orphaned by a governor
                            # deferral — carry it into the admitted call.
                            worktree_prepared=wp,
                        )
                    ),
                )
                # #240 point 3 (extended #303 item 4): name the blocking
                # pane(s) AND which project holds them — `assign` used to
                # answer only a bare limit-name reason with no way for Lead
                # to tell which pane to look at, and the queued role vanished
                # from `takkub list`/`status` entirely (see
                # `_queued_resource_roles` below) until the slot freed. A
                # bare pane-id-only holder list also used to read exactly
                # like a stale lock in the caller's own project even when the
                # real holder was a totally different project sharing this
                # machine's global limit — see `_describe_resource_wait`.
                holders = governor.holders_for_class(resource_class)
                queue_len = governor.queue_length_for_class(resource_class)
                _log_event(
                    "assign_resource_wait",
                    project=project_ns,
                    role=role_name,
                    resource_class=resource_class.value,
                    reason=decision.reason,
                    blocked_by=[f"{pane}@{proj}" for proj, pane in holders],
                    queue_len=queue_len,
                )
                return True, _describe_resource_wait(
                    role_name,
                    resource_class,
                    decision.reason,
                    holders,
                    metrics=governor.current_metrics(),
                    own_project=project_ns,
                    queue_len=queue_len,
                )
            if decision.token is not None:
                self._resource_tokens[resource_key] = decision.token
        # Fan-out queue (flag-gated, default off): defer a fresh teammate spawn
        # that would exceed the machine's total-pane budget. It spawns
        # automatically when a pane frees a slot (see _drain_fanout_queue). No-op
        # unless TAKKUB_QUEUE_FANOUT is set, so default behaviour is unchanged.
        if self._should_queue_assign(role_name, project):
            # The machine-pane queue owns admission from here; do not reserve a
            # governor slot while the work is parked behind a separate limit.
            token = self._resource_tokens.pop(resource_key, None)
            governor.release_slot(token)
            return self._enqueue_assign(
                role_name,
                cwd,
                task,
                requires_commit,
                auto_chain,
                shard_total,
                plan,
                isolation,
                project,
                feature,
                model,
                provider,
                effort,
            )

        # Per-pane git worktree isolation (issue #81): create the worktree +
        # branch, then dispatch the pane into it. On any failure this falls back
        # to a shared-cwd dispatch and warns the Lead — never blocks the assign.
        if isolation == "worktree":
            result = self._assign_with_worktree(
                role_name,
                cwd,
                task,
                requires_commit,
                auto_chain,
                shard_total,
                plan,
                project,
                feature,
                model,
                provider,
                effort,
                prepared=worktree_prepared,
            )
        else:
            result = self._assign_dispatch(
                role_name,
                cwd,
                task,
                requires_commit=requires_commit,
                auto_chain=auto_chain,
                shard_total=shard_total,
                plan=plan,
                project=project,
                worktree=None,
                feature=feature,
                model=model,
                provider=provider,
                effort=effort,
            )
        if not result[0]:
            token = self._resource_tokens.pop(resource_key, None)
            governor.release_slot(token)
        return result

    def _latest_queued_task_text(self, detail_path: pathlib.Path | None, fallback: str) -> str:
        """Resolve the text to actually deliver once a gate-blocked assign is
        finally admitted (#303 item 1).

        Reads *detail_path* — the ledger detail file written at enqueue
        time — back off disk so a task edit Lead made by hand while the
        assign was still queued (e.g. tightening a safety condition before
        it could run unattended) is what ships, not the stale in-memory copy
        captured in the `on_admitted` closure at the moment it was queued.
        Falls back to that in-memory copy if there's no detail file (write
        failed, or the task was short enough it was never split into one) or
        it's no longer readable.
        """
        if detail_path is None:
            return fallback
        from . import task_ledger

        text = task_ledger.read_detail_task(detail_path)
        return text if text else fallback

    def _tick_resource_governor(self) -> None:
        governor = getattr(self, "_resource_governor", None)
        if governor is None:
            return
        governor.sample()
        governor.dispatch_waiting()

    def _worktree_bare_role_collision(self, role_name: str, project: str | None) -> str | None:
        """Guard for issue #162: firing `assign --isolation worktree` twice at
        the same BARE role name (no `#N` shard suffix) before the pane it
        already owns is closed.

        Pane identity — `_project_panes()` / `_pane_state` / the spawn-initial
        one-shot payload — is keyed purely by `role_name`
        (`_exit_key(project, role_name)`). `--isolation worktree` creates a
        fresh git worktree on disk every call, but it dispatches into that
        SAME keyed pane slot. A second bare-name call therefore doesn't spawn
        an independent pane: `_assign_dispatch` overwrites the shared
        `PaneState`'s one-shot task payload, and `spawn()` either re-launches
        over the still-starting first process or (once it's alive) treats the
        pane as "already running" and pastes the second call's pointer text
        into it — the first call's own worktree gets silently dropped while a
        second, unused worktree sits on disk. Confirmed live: three
        back-to-back bare `assign --role backend --isolation worktree` calls
        left two of the three worktrees never entered by any process.

        A `#N` suffix (`_split_shard` giving a non-None index) sidesteps this
        entirely — each shard is its own registry key — so only the bare-name
        case is rejected. Any existing pane entry (alive, still spawning, or
        merely registered) counts as a collision: worktree isolation promises
        an independent pane per call, and reusing an in-flight identity
        breaks that promise regardless of the pane's exact lifecycle state.
        """
        if _split_shard(role_name)[1] is not None:
            return None
        project_ns = self._resolve_project(project)
        if self._project_panes(project_ns).get(role_name) is None:
            return None
        return (
            f"[{role_name}] มี pane อยู่แล้วใน project นี้ (bare role name ไม่มี #N) — "
            f"assign --isolation worktree ซ้ำที่ role name เดิมจะไม่ได้ pane อิสระใหม่ "
            f"แต่จะไปชน/paste ทับ pane เดิม (worktree ใหม่ถูกสร้างบนดิสก์แต่ไม่มี pane ไหนใช้งานจริง — #162). "
            f"ใช้ '{role_name}#N' (เช่น {role_name}#2) เพื่อได้ pane อิสระจริง "
            f"หรือปิด/รอ {role_name} ปัจจุบันให้เสร็จก่อน (takkub close --role {role_name})"
        )

    def _assign_dispatch(
        self,
        role_name: str,
        cwd: str | None,
        task: str,
        requires_commit: bool = False,
        auto_chain: bool = False,
        shard_total: int = 0,
        plan: bool = False,
        project: str | None = None,
        worktree: dict | None = None,
        feature: str = "",
        model: str | None = None,
        provider: str | None = None,
        effort: str | None = None,
    ) -> tuple[bool, str]:
        # Spawn the pane and run all post-spawn wiring (goal, provider rewrite,
        # verify hint, shard/plan bookkeeping, send). Shared by the normal assign
        # path and the worktree path (which passes the worktree checkout as cwd +
        # the WorktreeInfo dict so done()/close() can finalize it).
        # Plan mode spawns a single PLANNER pane (not a shard) — it carries
        # shard_total=0 so done() treats it as a normal pane; the fan-out it
        # triggers later assigns the real shards with shard_total=N.
        from .provider_config import CODEX, effective_provider_for
        from .provider_spec import PROVIDER_REGISTRY

        project_ns = self._resolve_project(project)
        # Task Ledger (A7) records what the caller asked for, not delivery
        # mechanics added below.
        raw_task_for_ledger = task
        task = self._apply_session_goal(task, project_ns)
        base_role_a = _split_shard(role_name)[0]
        # Fetched early (normally computed further down, right before spawn)
        # so an explicit --provider (#270) can be validated against
        # pane_is_running and folded into `effective_provider` BEFORE it's
        # used below for the codex task-rewrite decision and the
        # system_prompt_flag lookup — both must reflect the CLI that will
        # actually run, not the role's static config, exactly like the
        # model_override handling a few lines down.
        key = _exit_key(project_ns, role_name)
        ps_assign = self._ps(key)
        ps_assign.task_id = _uuid.uuid4().hex
        existing_pane = self._project_panes(project_ns).get(role_name)
        pane_is_running = bool(
            existing_pane is not None
            and getattr(existing_pane, "session", None) is not None
            and getattr(existing_pane.session, "is_alive", False)
        )
        if provider and pane_is_running:
            self._notify_lead(
                project_ns,
                f"⚠️ [{role_name}] --provider {provider!r} ไม่มีผล: pane เปิดอยู่แล้วและยังใช้ "
                "provider เดิม · close pane นี้ก่อนแล้ว assign ใหม่เพื่อใช้ override",
                from_role=role_name,
                note="",
                kind="assign-override-ignored",
            )
            _log_event(
                "assign_provider_override_ignored",
                role=role_name,
                project=project_ns,
                provider=provider,
                reason="pane-already-running",
            )
        elif not pane_is_running:
            # Same "clear on a plain re-assign, survive gate/FIFO/respawn
            # otherwise" contract as model_override below. A watchdog-set
            # degrade (see PaneState.provider_override docstring) that this
            # explicit call doesn't renew is intentionally dropped here —
            # spawn()'s own fresh-spawn-clear block would reset it to None on
            # the very next spawn anyway; this just does it one call sooner.
            ps_assign.provider_override = provider
        effective_provider = ps_assign.provider_override or effective_provider_for(
            base_role_a, project=project_ns
        )
        if effective_provider == CODEX:
            task = _rewrite_task_for_codex(task)
        task = _append_verify_fail_hint(task, base_role_a)
        # v2-hardening C (Adaptive Escalation) — a NEW task dispatched to a
        # role whose pane is still alive (pane_is_running, computed above)
        # is being reassigned before its previous task ever closed out: the
        # fix-loop/re-assign signal `03_ADAPTIVE_ESCALATION.md` describes.
        # A fresh spawn resets the count to 0 (see `next_retry_count`).
        if not hasattr(self, "_role_retry_counts"):
            self._role_retry_counts: dict[str, int] = {}
        from .core.brain.escalation import next_retry_count

        retry_count = next_retry_count(
            self._role_retry_counts.get(key, 0), pane_is_running=pane_is_running
        )
        self._role_retry_counts[key] = retry_count
        task = _inject_v2_context(
            task, project_ns, role_name, base_role_a, effective_provider, retry_count=retry_count
        )

        plan_file = None
        delivery_task = task
        if plan and shard_total > 0:
            plan_file = self._qa_plan_file(project_ns, base_role_a)
            delivery_task = self._wrap_planner_task(task, plan_file, shard_total)
        elif shard_total > 0:
            # Real shard pane (not the planner) — every shard in the group
            # gets identical task text, so a fixed output path in it is a
            # collision unless we force per-shard uniqueness (#160).
            shard_idx = _split_shard(role_name)[1] or 0
            delivery_task = self._wrap_shard_task(task, shard_idx, shard_total)

        # Materialise the reliable file handoff before spawn. Claude can attach
        # the full task to its per-spawn system-prompt file; the pointer remains
        # the fallback and is still the delivery path for a running pane and
        # providers without a confirmed file-backed equivalent.
        # #273: gated on the EFFECTIVE provider's own file-read capability —
        # a provider whose agent tool set has no structured file-read (only
        # codex, confirmed) never gets the pointer at all, regardless of
        # task length; see `_task_handoff_pointer`'s docstring.
        paste_text, task_file = _task_handoff_pointer(
            delivery_task,
            project_ns,
            role_name,
            supports_file_read=PROVIDER_REGISTRY[effective_provider].supports_agent_file_read,
        )
        if model and pane_is_running:
            self._notify_lead(
                project_ns,
                f"⚠️ [{role_name}] --model {model!r} ไม่มีผล: pane เปิดอยู่แล้วและยังใช้ "
                "model เดิม · close pane นี้ก่อนแล้ว assign ใหม่เพื่อใช้ override",
                from_role=role_name,
                note="",
                kind="assign-override-ignored",
            )
            _log_event(
                "assign_model_override_ignored",
                role=role_name,
                project=project_ns,
                model=model,
                reason="pane-already-running",
            )
        elif not pane_is_running:
            # PaneState can outlive a crashed session. A new assign without
            # --model must clear the prior one-shot choice; a new assign with
            # --model survives spawn gate/FIFO retries and crash auto-respawn.
            ps_assign.model_override = model
        if effort and pane_is_running:
            self._notify_lead(
                project_ns,
                f"⚠️ [{role_name}] --effort {effort!r} ไม่มีผล: pane เปิดอยู่แล้วและยังใช้ "
                "effort เดิม · close pane นี้ก่อนแล้ว assign ใหม่เพื่อใช้ override",
                from_role=role_name,
                note="",
                kind="assign-override-ignored",
            )
            _log_event(
                "assign_effort_override_ignored",
                role=role_name,
                project=project_ns,
                effort=effort,
                reason="pane-already-running",
            )
        elif not pane_is_running:
            # Same one-shot-choice contract as model_override immediately
            # above — narrowest override, cleared on every fresh assign that
            # doesn't repeat it.
            ps_assign.effort_override = effort
        ps_assign.spawn_initial_task = None
        ps_assign.spawn_initial_task_fallback = None
        ps_assign.spawn_initial_prompt_file = None
        ps_assign.spawn_initial_task_state = ""
        if PROVIDER_REGISTRY[effective_provider].system_prompt_flag is not None:
            ps_assign.spawn_initial_task = delivery_task
            ps_assign.spawn_initial_task_fallback = paste_text
            ps_assign.spawn_initial_task_state = "requested"

        spawn_shard_total = 0 if plan else shard_total
        ok, msg = self.spawn(role_name, cwd=cwd, project=project, _shard_total=spawn_shard_total)
        if not ok:
            ps_assign.spawn_initial_task = None
            ps_assign.spawn_initial_task_fallback = None
            ps_assign.spawn_initial_prompt_file = None
            ps_assign.spawn_initial_task_state = ""
            # The CLI already acked "task queued" to the Lead's shell before
            # this async spawn ran, so a failure here is invisible unless we
            # say so. Tell the Lead the task never landed (#26).
            self._warn_lead_spawn_failed(role_name, project, msg)
            # #5: record spawn-failed shard into its group so the aggregate
            # doesn't orphan forever (mirrors _warn_lead_respawn_capped path).
            # Plan mode has no shard group yet (the planner failed before
            # fan-out), so skip — the dead planner pane is visible to the user.
            if shard_total > 0 and not plan:
                pns_fail = self._resolve_project(project)
                base_fail = _split_shard(role_name)[0]
                gk_fail = f"{pns_fail}::{base_fail}"
                if gk_fail not in self._shard_groups:
                    self._shard_groups[gk_fail] = ShardGroup(base_role=base_fail, total=shard_total)
                    gen_fail = self._shard_groups[gk_fail].generation
                    QTimer.singleShot(
                        _SHARD_GROUP_TIMEOUT_MS,
                        lambda gk=gk_fail, pns=pns_fail, g=gen_fail: (
                            self._check_shard_group_timeout(pns, gk, g)
                        ),
                    )
                grp_fail = self._shard_groups[gk_fail]
                if not grp_fail.closed:
                    grp_fail.failed.add(role_name)
                    if len(grp_fail.done) + len(grp_fail.failed) >= grp_fail.total:
                        grp_fail.closed = True
                        self._inject_shard_fanout_handoff(pns_fail, grp_fail)
                        self._shard_groups.pop(gk_fail, None)
            return ok, msg

        ps_assign.last_assigned_task = delivery_task
        ps_assign.last_assigned_task_file = task_file
        # New task → fresh assign timestamp so done()'s evidence scan only
        # picks up screenshots captured for THIS task, not a stale one left
        # over from a previous assignment to the same pane (issue #5).
        ps_assign.assign_ts = time.time()
        # Task Ledger (A7): write-on-assign — every task, not just long ones,
        # so a role that never calls `takkub done` leaves a visible `[~]` row
        # behind. Degrades on failure (never blocks the assign itself).
        try:
            from .task_ledger import create_assignment

            ledger_cwd = cwd or default_cwd_for_role(base_role_a, project=project_ns)
            ledger_warning, _detail_path = create_assignment(
                project_ns,
                role_name,
                ledger_cwd,
                raw_task_for_ledger,
                self.get_session_goal(project=project_ns),
                feature,
                effective_provider,
            )
            if ledger_warning:
                self._notify_lead(
                    project_ns, ledger_warning, from_role=role_name, note="", kind="ledger-warning"
                )
            self.ledgerChanged.emit(project_ns)
        except Exception:
            _log_event("ledger_hook_error", role=role_name, project=project_ns, stage="assign")
        # New task → fresh one-shot budget for the Stop-hook done-gate.
        ps_assign.stop_gate_notified = False
        # New task → fresh auto-resume park/wake budget (issue: limit-aware
        # auto-resume). A pane that gave up on its previous task must get
        # another chance on this one.
        ps_assign.limit_parked = False
        ps_assign.limit_confirm_pending = False
        ps_assign.limit_park_rounds = 0
        ps_assign.limit_park_wake_ts = 0.0
        ps_assign.limit_park_stopped = False
        if requires_commit:
            ps_assign.requires_commit_on_done = True
        if auto_chain:
            ps_assign.auto_chain = True
        if worktree:
            # Remember the isolated worktree so done()/close() can finalize it
            # (merge proposal if the branch has commits, else safe-remove).
            ps_assign.worktree = worktree
            ps_assign.assign_base_sha = None
            ps_assign.assign_git_root = None
            ps_assign.assign_dirty_snapshot = None
        else:
            # #245/#251: a SHARED-tree pane has no WorktreeInfo baseline.
            # Capture HEAD plus the current porcelain paths' status/mtime/size
            # once per assign.  Comparing that cheap O(dirty paths) snapshot
            # at done excludes unchanged work that pre-dated this assignment;
            # no tracked-tree walk or content hash is performed.
            from .worktree_manager import WorktreeManager as _WorktreeManagerSnap

            _snap_pane = self._project_panes(project_ns).get(role_name)
            _snap_cwd = getattr(_snap_pane, "_session_cwd", None)
            if _snap_cwd:
                (
                    ps_assign.assign_base_sha,
                    ps_assign.assign_git_root,
                    ps_assign.assign_dirty_snapshot,
                ) = _WorktreeManagerSnap().shared_tree_baseline(_snap_cwd)
            else:
                ps_assign.assign_base_sha = None
                ps_assign.assign_git_root = None
                ps_assign.assign_dirty_snapshot = None
        initial_task_consumed = ps_assign.spawn_initial_task_state in {
            "pending",
            "delivered",
            "fallback",
        }
        if not initial_task_consumed:
            # Running pane, unsupported provider, or a mocked spawn in unit
            # tests: preserve the established pointer/ready-polling delivery.
            ps_assign.spawn_initial_task = None
            ps_assign.spawn_initial_task_fallback = None
            ps_assign.spawn_initial_prompt_file = None
            ps_assign.spawn_initial_task_state = ""
            self._send_when_ready(role_name, paste_text, project=project)
        initial_delivery = ps_assign.spawn_initial_task_state or "pointer"
        if initial_delivery == "delivered":
            initial_delivery_reason = "preloaded"
        elif initial_delivery == "pending":
            initial_delivery_reason = "queued-pending"
        elif initial_delivery == "fallback":
            initial_delivery_reason = "fallback-after-fail"
        elif PROVIDER_REGISTRY[effective_provider].system_prompt_flag is None:
            initial_delivery_reason = "provider-unsupported (by design)"
        else:
            initial_delivery_reason = "pane-already-running"
        if plan and shard_total > 0:
            # Planner wrapping happened before spawn so a fresh Claude pane can
            # receive the complete planner task in its one-shot system prompt.
            ps_assign.plan_fanout = {
                "shards": shard_total,
                "cwd": cwd,
                "task": task,
                "plan_file": str(plan_file),
                "model": model,
            }
            _log_event(
                "assign_plan",
                role=role_name,
                cwd=cwd,
                shards=shard_total,
                plan_file=str(plan_file),
                task_file=task_file,
                initial_delivery=initial_delivery,
                initial_delivery_reason=initial_delivery_reason,
                effective_provider=effective_provider,
            )
            return True, f"planner queued for {role_name} (fan-out {shard_total} on done)"
        if shard_total > 0:
            ps_assign.shard_total = shard_total
            # Create/update shard group for aggregate tracking.
            group_key = f"{project_ns}::{base_role_a}"
            if group_key not in self._shard_groups:
                group = ShardGroup(base_role=base_role_a, total=shard_total)
                self._shard_groups[group_key] = group
                # #2: capture generation so stale timers from a previous
                # fan-out with the same key don't close this new group.
                gen_a = group.generation
                QTimer.singleShot(
                    _SHARD_GROUP_TIMEOUT_MS,
                    lambda gk=group_key, pns=project_ns, g=gen_a: self._check_shard_group_timeout(
                        pns, gk, g
                    ),
                )
            else:
                self._shard_groups[group_key].total = shard_total
        _log_event(
            "assign",
            role=role_name,
            cwd=cwd,
            task_preview=task[:120],
            requires_commit=requires_commit,
            auto_chain=auto_chain,
            shard_total=shard_total,
            task_file=task_file,
            initial_delivery=initial_delivery,
            initial_delivery_reason=initial_delivery_reason,
            effective_provider=effective_provider,
            model_override=model,
            provider_override=provider,
            effort_override=effort,
        )
        return True, f"task queued for {role_name} (sending when ready)"

    def worktree_assign_inputs(
        self, role_name: str, cwd: str | None, project: str | None
    ) -> dict | None:
        """(#408) The cheap, main-thread half of `--isolation worktree`: the
        arguments `WorktreeManager.create` needs, so `cli_server` can run the
        slow `git worktree add` on a worker thread and hand the result back
        through `assign(worktree_prepared=...)`. Returns None whenever the
        synchronous path would not create a worktree anyway (bare-role
        collision → `assign` rejects; no resolvable cwd → `_assign_with_worktree`
        falls back) so nothing is ever created that `assign` then discards."""
        try:
            role_name = validate_name(role_name, "role")
        except Exception:
            return None
        if self._worktree_bare_role_collision(role_name, project):
            return None
        project_ns = self._resolve_project(project)
        base_role = _split_shard(role_name)[0]
        base_cwd = cwd or default_cwd_for_role(base_role, project=project_ns)
        if not base_cwd:
            return None
        sibling_ports = {
            ps.worktree.get("port", 0)
            for key, ps in getattr(self, "_pane_state", {}).items()
            if key.startswith(f"{project_ns}::") and ps.worktree
        } - {0}
        return {
            "base_cwd": base_cwd,
            "project_ns": project_ns,
            "role": role_name,
            "ts": int(time.time()),
            "exclude_ports": sibling_ports,
        }

    def done_git_inputs(self, from_role: str, project: str | None = None) -> dict | None:
        """(#408) The cheap, main-thread half of `done()`'s git fact gather:
        the inputs `WorktreeManager.collect_done_git_facts` needs, read from
        pane state without touching git. `cli_server` runs the collect on a
        worker thread and passes the dict to `done(git_facts=...)`. None when
        there is no pane/state to report on (done() then fails the same way
        it always did, synchronously)."""
        try:
            from_role = validate_name(from_role, "role")
        except Exception:
            return None
        project_ns = self._resolve_project(project)
        pane = self._project_panes(project_ns).get(from_role)
        ps = getattr(self, "_pane_state", {}).get(f"{project_ns}::{from_role}")
        if pane is None or ps is None:
            return None
        return {
            "worktree": dict(ps.worktree) if ps.worktree else None,
            "pane_cwd": getattr(pane, "_session_cwd", None),
            "assign_base_sha": ps.assign_base_sha,
            "assign_git_root": ps.assign_git_root,
        }

    def _assign_with_worktree(
        self,
        role_name: str,
        cwd: str | None,
        task: str,
        requires_commit: bool,
        auto_chain: bool,
        shard_total: int,
        plan: bool,
        project: str | None,
        feature: str = "",
        model: str | None = None,
        provider: str | None = None,
        effort: str | None = None,
        prepared: tuple | None = None,
    ) -> tuple[bool, str]:
        """Create an isolated git worktree for the pane, then dispatch into it.

        Any failure (not a git repo, no resolvable cwd, git error) degrades to a
        normal shared-cwd dispatch + a one-line Lead warning — a `--isolation
        worktree` assign must never be worse than a plain assign. The worktree
        add is a bounded synchronous git op (matches the existing on-thread spawn
        cost envelope; a Phase-2 optimisation can move it to QProcess).
        """
        from .worktree_manager import WorktreeManager

        project_ns = self._resolve_project(project)
        base_role = _split_shard(role_name)[0]
        base_cwd = cwd or default_cwd_for_role(base_role, project=project_ns)

        def _fallback(reason: str) -> tuple[bool, str]:
            _log_event("worktree_fallback", role=role_name, project=project_ns, reason=reason[:200])
            self._notify_lead(
                project_ns,
                f"⚠️ [{role_name}] worktree isolation ใช้ไม่ได้ → รันแบบ shared cwd แทน · {reason}",
                from_role=role_name,
                note="",
                kind="worktree-fallback",
            )
            return self._assign_dispatch(
                role_name,
                cwd,
                task,
                requires_commit=requires_commit,
                auto_chain=auto_chain,
                shard_total=shard_total,
                plan=plan,
                project=project,
                worktree=None,
                feature=feature,
                model=model,
                provider=provider,
                effort=effort,
            )

        if not base_cwd:
            return _fallback("ไม่มี cwd ให้สร้าง worktree (ระบุ --cwd)")

        mgr = WorktreeManager()
        # Exclude ports already reserved by live sibling worktrees in THIS
        # project — a bind probe can't see them (their dev servers may not have
        # started yet), so two same-second assigns would both get base_port.
        sibling_ports = {
            ps.worktree.get("port", 0)
            for key, ps in getattr(self, "_pane_state", {}).items()
            if key.startswith(f"{project_ns}::") and ps.worktree
        } - {0}
        if prepared is not None:
            # #408: `git worktree add` already ran off the Qt thread
            # (`worktree_assign_inputs` → cli_server worker → here).
            info, reason = prepared
        else:
            info, reason = mgr.create(
                base_cwd, project_ns, role_name, int(time.time()), exclude_ports=sibling_ports
            )
        if info is None:
            return _fallback(reason)

        _log_event(
            "worktree_created",
            role=role_name,
            project=project_ns,
            branch=info.branch,
            path=info.path,
            links=list(info.links),
            port=info.port,
        )
        # Non-fatal env-propagation warnings (P2.2): the worktree exists and is
        # usable, but some configured links/config entries were skipped.
        env_note = f" · ⚠️ {reason}" if reason else ""
        linked_note = f" · linked: {', '.join(info.links)}" if info.links else ""
        if info.port:
            linked_note += f" · dev port {info.port}"
        self._notify_lead(
            project_ns,
            f"🌿 [{role_name}] isolated worktree — branch `{info.branch}` "
            f"(build แยก ไม่ชนกับ pane อื่น · merge เป็น proposal ตอน done)"
            f"{linked_note}{env_note}",
            from_role=role_name,
            note="",
            kind="worktree-spawn",
        )
        # #444: a fresh `--isolation worktree` cwd is a path Claude Code has
        # never seen before, and its folder-trust dialog's auto-answer gives
        # up after 90s — leaving the pane parked with no task ever
        # delivered. Pre-trust the shared managed root (never each
        # individual worktree) here, before dispatch spawns the pane, so
        # this cwd — and every sibling worktree under it — is already
        # trusted. Claude-only: other providers have no such file. Uses the
        # same override-or-role-default resolution as `_assign_dispatch`
        # (a worktree assign's `pane_is_running` is effectively always
        # False — the worktree just got created above). Best-effort; never
        # blocks the spawn on failure.
        from .provider_config import CLAUDE, effective_provider_for

        resolved_provider = (provider or "").strip() or effective_provider_for(
            role_name, project=project_ns
        )
        if resolved_provider == CLAUDE:
            from .worktree_manager import pre_trust_worktrees_root

            pre_trust_worktrees_root(project_ns)

        # postCreate runs in the pane's own shell via the task hint (visible,
        # off the Qt thread — pnpm install can take minutes).
        from .worktree_manager import load_worktree_config

        wt_cfg, _ = load_worktree_config(info.git_root)
        result = self._assign_dispatch(
            role_name,
            info.path,
            # Scoped policy override: the pane must commit on ITS OWN branch or
            # finalize can never produce a merge proposal (e2e finding, #81).
            _append_worktree_hint(task, info.branch, wt_cfg.post_create, info.port),
            requires_commit=requires_commit,
            auto_chain=auto_chain,
            shard_total=shard_total,
            plan=plan,
            project=project,
            worktree=info.as_dict(),
            feature=feature,
            model=model,
            provider=provider,
            effort=effort,
        )
        # Tag the pane title with the branch so the isolation is unmistakable in
        # the cockpit (best-effort; the pane exists once dispatch's spawn emitted
        # paneRequested). Never let a UI tag failure affect the assign result.
        try:
            self._tag_pane_worktree(project_ns, role_name, info.branch)
        except Exception:
            pass
        return result

    def request_restart(self) -> tuple[bool, str]:
        """`takkub restart` — full cockpit restart without touching the GUI.

        Rides the SAME path as the status-bar 🔄 button (_restart_cockpit):
        state/tabs/session-snapshot/resume-briefs are persisted before the
        successor spawns, so teammates restore. The signal is emitted on a
        short timer so the CLI gets its reply before the app starts quitting.
        Typing the command IS the confirmation — no dialog on this path.
        """
        _log_event("cockpit_restart", reason="cli")
        # #232: same marker `_restart_cockpit` writes for its own
        # auto-triggered reasons, so `restore_teammates()` can attribute the
        # Lead-facing restore notice to whichever path actually fired.
        _write_restart_reason_marker("cli")
        QTimer.singleShot(200, self.restartRequested.emit)
        return True, "restarting cockpit — state persisted, app relaunching (panes respawn)"

    def _tag_pane_worktree(self, project_ns: str, role_name: str, branch: str) -> None:
        """Set the worktree-branch chip on a pane's header (🌿 <branch>)."""
        pane = self._project_panes(project_ns).get(role_name)
        if pane is not None:
            setter = getattr(pane, "set_worktree_branch", None)
            if callable(setter):
                setter(branch)

    def _finalize_worktree(
        self, project_ns: str, from_role: str, worktree: dict, precomputed: dict | None = None
    ) -> None:
        """Wrap up an isolated pane's worktree when it reports done/close.

        * branch has commits → send Lead a MERGE PROPOSAL (propose-then-fire,
          never auto) and KEEP the worktree until the Lead merges.
        * no commits at all  → NEVER auto-delete (#161). A pane that reports
          done without ever committing — even though the task required it —
          used to be silently `rmtree`'d right here the instant done() fired,
          with zero recovery path (proven data loss on a real task). Keep the
          worktree either way and warn the Lead loudly instead; explicit
          cleanup is still one command away via `takkub worktree clean`
          (`WorktreeManager.clean_isolated`), which applies the exact same
          clean+no-commits SAFE test this method used to apply automatically —
          but only on the Lead's say-so, after they've had a chance to look.

        Phase-1 worktrees are build-only (no dev-server / node_modules copied in),
        so they contain only tracked source and every git op here is sub-second;
        the WorktreeManager runner's 30s timeout is the hard backstop. Best-effort
        throughout — a git hiccup here must never break the done() flow.

        *precomputed* (#245): `done()` already ran these exact same git reads
        a few lines earlier to build the digest fact table — when supplied,
        this method reuses them instead of shelling out a second time for
        the same answer. Shape: ``{"commits", "dirty", "uncommitted",
        "merge_conflicts", "diffstat"}``. None (the `close()` call site,
        which never computes digest facts) falls back to the original
        compute-here-every-time behaviour, unchanged.
        """
        try:
            from .worktree_manager import WorktreeInfo, WorktreeManager, build_merge_proposal

            info = WorktreeInfo.from_dict(worktree)
            mgr = WorktreeManager()
            commits = precomputed["commits"] if precomputed is not None else mgr.commit_count(info)
            if commits > 0:
                # #244: commits > 0 does NOT mean "ready to merge" — the
                # branch can carry accepted commits AND still hold fresh
                # uncommitted work on top (real near-miss, twice in one
                # night: this used to announce "พร้อม merge" unconditionally
                # the instant commits > 0, and Lead almost merged stale
                # work). Check dirty + merge-cleanliness against the
                # CURRENT base before the proposal is allowed to claim
                # readiness. Same git-subprocess-on-main-thread shape the
                # 3 calls just below it already used (commit_count/diffstat/
                # is_dirty) — a one-shot event fired from done(), not a
                # per-tick poll (unlike #229's per-tick FS walk), so two
                # more fast local git calls stay in the same performance
                # class as the pre-existing calls, not a new risk.
                if precomputed is not None:
                    dirty = precomputed["dirty"]
                    uncommitted = precomputed["uncommitted"]
                    merge_conflicts = precomputed["merge_conflicts"]
                    conflict_files = precomputed.get("conflict_files")
                    diffstat_text = precomputed["diffstat"]
                else:
                    dirty = mgr.is_dirty(info)
                    uncommitted = mgr.uncommitted_count(info) if dirty else 0
                    conflict_files = mgr.merge_conflict_files(info.git_root, info.branch)
                    merge_conflicts = (
                        bool(conflict_files)
                        if conflict_files is not None
                        else mgr.merge_conflicts_with_base(info.git_root, info.branch)
                    )
                    diffstat_text = mgr.diffstat(info)
                proposal = build_merge_proposal(
                    from_role,
                    info,
                    commits,
                    diffstat_text,
                    dirty=dirty,
                    uncommitted=uncommitted,
                    merge_conflicts=merge_conflicts,
                    conflict_files=conflict_files,
                )
                _log_event(
                    "worktree_merge_proposed",
                    role=from_role,
                    project=project_ns,
                    branch=info.branch,
                    commits=commits,
                    dirty=dirty,
                    merge_conflicts=merge_conflicts,
                    conflict_files=(conflict_files or [])[:20],
                )
                self._notify_lead(
                    project_ns, proposal, from_role=from_role, note="", kind="worktree-proposal"
                )
                return
            dirty = precomputed["dirty"] if precomputed is not None else mgr.is_dirty(info)
            _log_event(
                "worktree_no_commit_kept",
                role=from_role,
                project=project_ns,
                branch=info.branch,
                dirty=dirty,
            )
            state_note = (
                "มี uncommitted changes ในนั้น — ยังกู้ได้"
                if dirty
                else "working tree clean ด้วย — เช็คให้ชัวร์ว่างานหายไปจริงหรือแค่ลืม commit"
            )
            self._notify_lead(
                project_ns,
                f"⚠️ [{from_role}] done แต่ไม่มี commit ใน worktree `{info.branch}` — "
                f"เก็บไว้ไม่ลบอัตโนมัติ ({state_note}) · path: {info.path} · "
                f"ตรวจสอบแล้วค่อยลบเองด้วย `takkub worktree clean`",
                from_role=from_role,
                note="",
                kind="worktree-no-commit-kept",
            )
        except Exception as exc:  # never let worktree cleanup break done()
            _log_event(
                "worktree_finalize_error",
                role=from_role,
                project=project_ns,
                error=str(exc)[:200],
            )

    def _unknown_pane_message(self, to_role: str, project_ns: str) -> str:
        """Explain why `send()`'s pane lookup came back empty (issue #164).

        `_project_panes(...).get(role)` returns `None` in two different
        situations that used to collapse into the same misleading "unknown
        role" message: the name genuinely isn't in the role registry at all,
        vs. it's a real role (`roles.by_name` finds it) whose pane just isn't
        open right now — e.g. it was closed, or the process restarted and
        this role hasn't respawned yet (`unregister_pane` pops the pane
        widget out of `_project_panes` but never touches `roles.py`'s
        registry). Only the first case is actually "unknown role".

        `to_role` may carry a shard suffix (`"qa#1"`) — the registry only
        knows base role names, so that's stripped via `_split_shard` before
        the lookup; the raw (possibly sharded) name is still used for the
        PaneState/worktree lookup below since shard panes get their own
        independent `PaneState` entry."""
        base_role, _shard_idx = _split_shard(to_role)
        if _role_by_name(base_role) is None:
            return f"unknown role: {to_role}"
        hint = (
            f"'{to_role}' is a known role but has no pane open right now — "
            f'use `takkub assign --role {to_role} "<task>"` to open one'
        )
        ps = self._pane_state.get(_exit_key(project_ns, to_role))
        wt = getattr(ps, "worktree", None) if ps is not None else None
        wt_path = wt.get("path") if wt else None
        if wt_path:
            hint += (
                f" (this role last worked on an isolated worktree — a plain "
                f"`--isolation worktree` re-assign creates a *new* branch off "
                f"base, it does not continue the old one; to pick that work "
                f"back up, assign with `--cwd {wt_path}` instead)"
            )
        return hint

    def _queue_message_for_unspawned_role(
        self, to_role: str, msg: str, from_role: str | None, project_ns: str
    ) -> tuple[bool, str]:
        """`takkub send --to <r>` when `<r>` is a known role with no pane
        open right now (#303 item 3) — used to fail outright
        (`_unknown_pane_message`'s "known role but has no pane open"
        wording), which left Lead unable to hand a not-yet-spawned
        teammate anything — e.g. a safety condition worth adding to the
        task BEFORE it starts — until it happened to spawn on its own,
        reported as blocking for up to an hour in the field.

        Durably records the message via `role_messages` (same JSONL store
        `takkub messages --role` already reads, so it survives a cockpit
        restart for free) instead of adding a second, parallel persistence
        mechanism. `spawn()` flushes it once `to_role`'s pane actually comes
        up — see `_flush_queued_no_pane_messages`.
        """
        base_role, _shard_idx = _split_shard(to_role)
        if _role_by_name(base_role) is None:
            return False, f"unknown role: {to_role}"
        from . import role_messages

        role_messages.append_queued_no_pane(
            RUNTIME_DIR, project_ns, to_role=to_role, from_role=from_role, body=msg
        )
        _log_event("send_queued_no_pane", project=project_ns, to=to_role, from_role=from_role)
        return True, (
            f"'{to_role}' has no pane open right now — message queued, will be delivered "
            f'as soon as it spawns (`takkub assign --role {to_role} "<task>"` to open it now)'
        )

    def _flush_queued_no_pane_messages(self, project_ns: str, role_name: str) -> None:
        """Deliver `takkub send` messages queued while `role_name` had no
        pane open (#303 item 3), now that it has one. Mirrors
        `_flush_pending_lead_cc`'s retry shape: a no-op if the pane went
        busy/died again before this fired (5s after spawn — see
        spawn_engine.py) — the records stay `"queued_no_pane"` on disk and
        the next spawn retries them."""
        from . import role_messages

        pending = role_messages.queued_no_pane_for_role(RUNTIME_DIR, project_ns, role_name)
        if not pending:
            return
        pane = self._project_panes(project_ns).get(role_name)
        if not (pane and pane.session and pane.session.is_alive):
            return
        delivered = 0
        for rec in pending:
            body = rec.get("body")
            msg_id = rec.get("id", "")
            if not isinstance(body, str):
                role_messages.mark_abandoned(RUNTIME_DIR, project_ns, msg_id, "empty body")
                continue
            self.send(role_name, body, from_role=rec.get("from"), project=project_ns)
            # #303: `send()` above already appended a fresh "sent" record for
            # the real delivery — mark the queued placeholder terminal so it
            # is never picked up again, without double-counting it as a
            # second live delivery attempt.
            role_messages.mark_abandoned(RUNTIME_DIR, project_ns, msg_id, "flushed_after_spawn")
            delivered += 1
        if delivered:
            _log_event(
                "send_queued_no_pane_flushed", project=project_ns, role=role_name, count=delivered
            )

    def _redact_forwarded_text(
        self, text: str, project_ns: str, *, hop: str, from_role: str | None = None
    ) -> str:
        """(#441) Scrub credential-shaped values from text the cockpit is
        about to copy somewhere new (Lead transcript, inbox file, phone).
        Logs one event per hit so the audit trail shows a value WAS
        forwarded and stripped, without ever logging the value itself."""
        from .secret_redact import redact_secrets

        out, names = redact_secrets(text)
        if not names:
            return text
        _log_event(
            "forwarded_secret_redacted",
            project=project_ns,
            hop=hop,
            role=from_role or "",
            names=names[:10],
        )
        shown = ", ".join(names[:6]) + (" …" if len(names) > 6 else "")
        return f"{out}\n⚠ cockpit redacted {len(names)} secret value(s): {shown} (#441)"

    def send(
        self,
        to_role: str,
        msg: str,
        from_role: str | None = None,
        project: str | None = None,
    ) -> tuple[bool, str]:
        try:
            to_role = validate_name(to_role, "role")
        except ValueError as exc:
            return False, str(exc)
        project_ns = self._resolve_project(project)
        # #441: a pane that just `cat`-ed an env file forwards the values
        # verbatim in its message — scrub at the cockpit hop, every provider.
        msg = self._redact_forwarded_text(msg, project_ns, hop="send", from_role=from_role)
        project_panes = self._project_panes(project_ns)
        pane = project_panes.get(to_role)
        if pane is None:
            return self._queue_message_for_unspawned_role(to_role, msg, from_role, project_ns)
        if pane.session is None or not pane.session.is_alive:
            return False, f"{to_role} is not running (spawn it first)"

        # #255: Lead sending straight into a pane that still has a task
        # delivery retrying toward it is a strong signal Lead has taken
        # over recovery by hand (e.g. after the delivery got stuck on a
        # boot stall, #254) — leaving the old delivery armed means its
        # self-heal resend re-pastes the ORIGINAL task on top of whatever
        # Lead just sent the moment the pane reaches ready, silently
        # duplicating the work. Scoped to Lead-originated sends only
        # (from_role is None for the CLI's own default, or explicitly
        # "lead") — a peer teammate messaging another teammate is not
        # "Lead recovering delivery" and must not cancel anything.
        if to_role != LEAD.name and from_role in (None, LEAD.name):
            delivery_manager = getattr(self, "_delivery_manager", None)
            if delivery_manager is not None:
                generation = int(getattr(pane, "_session_generation", 0))
                # #295: the two outcomes below mean opposite things to Lead, so
                # they are reported as two different notices instead of one
                # count. Cancelling a delivery that already pasted once only
                # suppresses a duplicate re-paste; a delivery that never
                # reached the pane is the pane's ONLY copy of its task, and the
                # old single-count message made "harmless" and "your teammate
                # now has no work" look identical.
                cancelled_list, kept_undelivered = delivery_manager.supersede_for_session(
                    project_ns, to_role, generation
                )
                superseded = len(cancelled_list)
                if superseded:
                    last_ids = getattr(self, "_last_delivery_ids", None)
                    if last_ids is not None:
                        last_ids.pop((project_ns, to_role), None)
                    # #392: a bare count ("ยกเลิก 1 pending delivery") gave
                    # Lead no way to tell WHICH task got cancelled or what
                    # replaced it without cross-checking the transcript by
                    # hand — name both here instead.
                    cancelled_desc = "; ".join(
                        f'{d.pane_id}#{d.task_id[:8]} "{_truncate_at_word_boundary(d.payload, 30)}"'
                        for d in cancelled_list
                    )
                    # #464: this used to also `_notify_lead` a paragraph
                    # ending in its own "ปลอดภัย ไม่ต้องทำอะไร" (safe, nothing
                    # to do) — a message that names itself actionless has no
                    # business interrupting Lead. The audit trail below is
                    # enough; `takkub inbox`/events.log still has the detail
                    # (cancelled_desc/replacement_desc) if anyone needs it.
                    _log_event(
                        "delivery_superseded_by_send",
                        role=to_role,
                        project=project_ns,
                        cancelled=superseded,
                        cancelled_desc=cancelled_desc,
                        replacement_desc=_truncate_at_word_boundary(msg, 30),
                    )
                if kept_undelivered:
                    # #336: "not yet delivered" and "we cannot tell" need
                    # different advice. The first still lands on its own; an
                    # UNCERTAIN one will not be re-pasted (auto-repaste stays
                    # off, #134/#328), so Lead has to look and decide.
                    unconfirmed = [
                        d
                        for d in kept_undelivered
                        if DeliveryState(d.state) is DeliveryState.UNCERTAIN
                    ]
                    pending = [
                        d
                        for d in kept_undelivered
                        if DeliveryState(d.state) is not DeliveryState.UNCERTAIN
                    ]
                    if pending:
                        task_ids = ", ".join(sorted({d.task_id[:8] for d in pending}))
                        self._notify_lead(
                            project_ns,
                            f"⚠️ [delivery-pending] {to_role} มีใบงาน {len(pending)} ใบที่ "
                            f"**ยังไม่ถึงมือ** (task {task_ids}) — ไม่ถูกยกเลิก จะส่งให้เมื่อ pane ready "
                            f"แต่ข้อความที่เพิ่ง `takkub send` อาจถึงก่อนใบงาน ถ้าข้อความนั้นอ้างถึงใบงาน "
                            f"ให้ส่งซ้ำหลัง pane รับงานแล้ว",
                            from_role="system",
                            note="delivery_pending_not_cancelled",
                            kind="delivery-pending-not-cancelled",
                        )
                    if unconfirmed:
                        task_ids = ", ".join(sorted({d.task_id[:8] for d in unconfirmed}))
                        self._notify_lead(
                            project_ns,
                            f"🚨 [delivery-unconfirmed] {to_role} มีใบงาน {len(unconfirmed)} ใบที่ "
                            f"**ไม่ยืนยันว่าถึงมือหรือไม่** (task {task_ids}) — ไม่ถูกยกเลิก และ "
                            f"**จะไม่ paste ซ้ำให้เอง** · เช็ค transcript ของ {to_role} ว่าเห็นใบงานจริงไหม "
                            f"ถ้าไม่เห็น ให้ `takkub assign` ใหม่ทั้งใบ อย่าส่งข้อความแก้ทีละจุด",
                            from_role="system",
                            note="delivery_unconfirmed_on_send",
                            kind="delivery-unconfirmed-on-send",
                        )
                    _log_event(
                        "delivery_kept_undelivered_on_send",
                        role=to_role,
                        project=project_ns,
                        kept=len(kept_undelivered),
                        states=sorted({str(d.state) for d in kept_undelivered}),
                    )

        header = f"[{from_role} → {to_role}] " if from_role and from_role != to_role else ""
        body = header + _sanitize_pane_text(msg)
        _send_sess = pane.session
        body_payload = _paste_payload(body)
        message_expires_at = time.time() + float(
            os.environ.get("TAKKUB_TASK_DELIVERY_TTL_SEC", "30")
        )
        # #277: record the message BEFORE writing it. `send` used to leave no
        # trace at all, so a respawn (crash / stuck-recover / rate-limit
        # resume) took the message with it and nothing — not the sender, not
        # Lead, not a later audit — could tell it had ever existed. The
        # generation stamp is what lets `_reap_role_messages` recognise, later,
        # that this message was written into a session that no longer exists.
        send_generation = int(getattr(pane, "_session_generation", 0))
        message_id = self._record_role_message(
            project_ns, to_role=to_role, from_role=from_role, body=body, generation=send_generation
        )
        _safe_session_write(
            _send_sess,
            body_payload,
            priority=WritePriority.TASK,
            kind="task",
            expires_at=message_expires_at,
            session_generation=send_generation,
        )
        # Self-healing submit (issue #22): resend Enter if the peer message's
        # submit was swallowed mid-paste-render. Safe — a busy target isn't at
        # its ready prompt, so no resend fires into an in-flight turn.
        _delayed_enter_verified(
            pane,
            _send_sess,
            _enter_delay_ms(body_payload),
            # Peer messages are never automatically pasted twice. If submit
            # evidence is ambiguous, retry Enter only and surface the existing
            # delivery warning path instead of duplicating the body.
            payload=None,
            content_fragment=body,
            on_resend=lambda rem, r=to_role: _log_event(
                "send_enter_resend", project=project_ns, role=r, remaining=rem
            ),
            on_repaste=lambda rem, r=to_role: _log_event(
                "send_repaste", project=project_ns, role=r, remaining=rem
            ),
            # #277: the same accept signal task delivery already trusts — the
            # pane left its ready prompt, so it took the paste. Until this
            # fires the record stays "sent", which is the honest state: bytes
            # written, receipt unproven.
            on_settled=lambda p=project_ns, m=message_id, s=_send_sess: self._confirm_role_message(
                p, m, s
            ),
            expires_at=message_expires_at,
        )

        # Record delivery time for stall detection: receiving a message counts
        # as evidence the pane is still being monitored by the orchestrator.
        self._ps(f"{project_ns}::{to_role}").last_send_ts = time.time()

        # CC Lead unless source was Lead and target was a teammate, or vice versa.
        # If Lead is not alive, queue the CC so it isn't silently lost — the
        # queue is flushed when Lead next spawns (see _flush_pending_lead_cc).
        if from_role and from_role not in (None, LEAD.name) and to_role != LEAD.name:
            lead = project_panes.get(LEAD.name)
            if lead and lead.session and lead.session.is_alive:
                self._notify_lead(project_ns, f"[CC] {body}", kind="peer-cc")
            else:
                ts = datetime.now().isoformat(timespec="seconds")
                self._pending_lead_cc.setdefault(project_ns, []).append(
                    {"from_role": from_role, "to_role": to_role, "body": f"[CC] {body}", "ts": ts}
                )
                self._save_pending_cc(project_ns)
                _log_event(
                    "send_cc_queued",
                    project=project_ns,
                    from_=from_role,
                    to=to_role,
                    msg_preview=body[:120],
                )

        # Track teammate ↔ Lead conversation so the idle watchdog doesn't
        # fire its `[auto-reminder]` while a teammate is legitimately
        # waiting for Lead to reply. Two cases:
        #   - teammate → Lead: mark sender as blocked-on-lead
        #   - Lead → teammate: clear teammate's blocked-on-lead flag
        from_norm = (from_role or "").lower().strip()
        if from_norm and from_norm != LEAD.name and to_role == LEAD.name:
            self._ps(f"{project_ns}::{from_norm}").blocked_on_lead_ts = time.time()
        elif from_norm == LEAD.name and to_role != LEAD.name:
            _ps_to = self._pane_state.get(f"{project_ns}::{to_role}")
            if _ps_to is not None:
                _ps_to.blocked_on_lead_ts = None
                _ps_to.last_turn_end_ts = None
                # Lead sending new instructions counts as new real work —
                # give the Stop-hook done-gate a fresh one-shot budget.
                _ps_to.stop_gate_notified = False

        _MAX_LOG_BODY = 4_096
        _log_event(
            "send",
            to=to_role,
            from_=from_role,
            body=msg[:_MAX_LOG_BODY] + ("…" if len(msg) > _MAX_LOG_BODY else ""),
            message_id=message_id,
        )
        # #277: no longer claims receipt. Writing to the PTY is all that has
        # happened at this point — the pane may still be booting, mid-turn, or
        # about to be respawned out from under the paste. The record above is
        # what turns that from an unanswerable question into a lookup.
        return True, (
            f"queued to {to_role} (id {message_id}) — "
            f"ยืนยันว่าถึงมือจริงไหมด้วย `takkub messages --role {to_role}`"
        )

    def push_report(
        self,
        name: str,
        url: str,
        label: str,
        size_bytes: int,
        attachment: bool,
        project: str | None = None,
    ) -> tuple[bool, str]:
        """#390: `takkub report publish --send`'s IPC target (`cli_server`'s
        `report-send` cmd). Emits `reportShared` for `remote.notify.
        LeadNotifier` to turn into an SSE `report` event — see that signal's
        comment for why this is emit-and-return rather than an import into
        `remote`.

        Always returns `(True, ...)` when it gets this far: reaching this
        method at all already proves the cockpit is up and reachable (the
        caller's IPC round-trip succeeded), which is the one thing this
        layer can actually confirm. Whether remote control is enabled, a
        tunnel is up, or a phone is actually connected right now are things
        only `remote/` code knows — `cmd_report`'s CLI side already checked
        `remote_status_text()` before attempting this call and reports that
        reason itself when it skips the attempt entirely, so this never
        needs to re-derive it via a forbidden import."""
        project_ns = self._resolve_project(project)
        payload = {
            "name": name,
            "url": url,
            "label": label,
            "size_bytes": int(size_bytes),
            "attachment": bool(attachment),
        }
        self.reportShared.emit(project_ns, payload)
        return True, f"pushed {name!r} to project {project_ns}"

    def answer_picker(self, key_sequence: str, project: str | None = None) -> tuple[bool, str]:
        """Remote mobile AskUserQuestion fix: write a raw key
        sequence straight into the Lead pane's PTY, bypassing `send()`'s
        chat-message pipeline entirely — `_sanitize_pane_text`/
        `_paste_payload` are built for prose a human typed, not control
        keystrokes, and `send()`'s delayed-Enter self-heal would double-fire
        Enter into a picker that already advanced on its own.

        `key_sequence` is caller-built (`remote/api.py::_build_picker_key_
        sequence`) from a *fresh* re-read of the pane's actual current
        AskUserQuestion state — every character in it is a plain ASCII
        digit ('1'-'9', selecting an option) or '\\r' (confirming the
        multi-question "Review your answers" screen), never an escape
        sequence, so no sanitization is needed or wanted here. Lead-pane
        only, same as every other write this orchestrator makes into a
        picker: there is no such thing as a teammate-pane picker (#103,
        `spawn_engine.py` denies teammates the `AskUserQuestion` tool
        outright)."""
        if not key_sequence:
            return False, "empty key sequence"
        project_ns = self._resolve_project(project)
        pane = self._project_panes(project_ns).get(LEAD.name)
        if pane is None or pane.session is None or not pane.session.is_alive:
            return False, "lead is not running"
        pane.session.write(key_sequence)
        return True, "ok"

    # ------------------------------------------------------------------
    # Pane health (#280) — watchdog observations are accumulated per pane
    # lifecycle and reported at its terminal event instead of interrupting
    # Lead with a status update every time something is noticed. See
    # pane_health.py for what stays immediate and why.
    # ------------------------------------------------------------------

    def _record_pane_health(
        self, project_ns: str, role: str, kind: str, detail: str, live_body: str | None = None
    ) -> bool:
        """Record one watchdog observation for (project, role).

        Returns True when the caller should ALSO send *live_body* to Lead right
        now — i.e. only under the `live` policy. Under the default `terminal`
        policy the observation is kept for the pane's report; under `off` it is
        dropped entirely.
        """
        from .pane_health import POLICY_LIVE, POLICY_OFF, PaneHealth, watch_policy

        policy = watch_policy()
        if policy == POLICY_OFF:
            return False
        store = self.__dict__.setdefault("_pane_health", {})
        health = store.setdefault(f"{project_ns}::{role}", PaneHealth())
        health.record(kind, detail, ts=time.time())
        return policy == POLICY_LIVE and live_body is not None

    def _drain_pane_health(self, project_ns: str, role: str) -> str:
        """Pop this pane's accumulated observations and render them as one
        line for its report. Popping (not peeking) is deliberate: a pane
        lifecycle reports its own health exactly once, and a respawn under the
        same role starts clean rather than inheriting the previous pane's."""
        from .pane_health import summarize

        store = getattr(self, "_pane_health", None)
        if not store:
            return ""
        return summarize(store.pop(f"{project_ns}::{role}", None))

    # ------------------------------------------------------------------
    # Role-message durability (#277) — see role_messages.py for the store.
    # ------------------------------------------------------------------

    def _record_role_message(
        self, project_ns: str, *, to_role: str, from_role: str | None, body: str, generation: int
    ) -> str:
        """Append one `takkub send` to the durable log; returns its id (empty
        string if the store is unwritable — a broken audit log must never stop
        the message itself from going out)."""
        try:
            from . import role_messages

            return role_messages.append(
                RUNTIME_DIR,
                project_ns,
                to_role=to_role,
                from_role=from_role,
                body=body,
                generation=generation,
            )
        except Exception as exc:
            _log_event("role_message_record_failed", project=project_ns, error=str(exc)[:200])
            return ""

    def role_message_log(
        self, role: str, project: str | None = None, limit: int = 20
    ) -> tuple[bool, str, list[str]]:
        """`takkub messages --role <r>` (#277): the send-audit log for one
        role, rendered for the CLI. Read-only — never mutates delivery state."""
        try:
            role = validate_name(role, "role")
        except ValueError as exc:
            return False, str(exc), []
        project_ns = self._resolve_project(project)
        try:
            from . import role_messages

            records = role_messages.read(RUNTIME_DIR, project_ns, role=role)
            lines = role_messages.format_for_cli(records, limit=max(1, int(limit)))
        except Exception as exc:
            return False, f"อ่าน message log ไม่ได้: {exc}", []
        pending = sum(1 for r in records if r.get("state") == "sent")
        queued_no_pane = sum(1 for r in records if r.get("state") == "queued_no_pane")
        note = f"ยังไม่ยืนยันว่าถึงมือ {pending}"
        if queued_no_pane:
            note += f" · รอ pane เปิด {queued_no_pane}"
        return (
            True,
            f"{len(records)} ข้อความถึง {role} ({note})",
            lines,
        )

    def _confirm_role_message(self, project_ns: str, message_id: str, session) -> None:
        """Mark a recorded message delivered once its submit chain settles and
        the pane is demonstrably no longer at its ready prompt."""
        if not message_id:
            return
        try:
            accepted = not session.is_at_ready_prompt()
        except Exception:
            accepted = False
        if not accepted:
            return
        try:
            from . import role_messages

            role_messages.mark_delivered(RUNTIME_DIR, project_ns, message_id)
        except Exception:
            pass

    def _reap_role_messages(self) -> None:
        """Re-deliver `takkub send` messages that a respawn swallowed (#277).

        Rides the existing idle-watchdog tick rather than hooking each respawn
        path separately: there are several (crash auto-respawn, stuck-recover,
        rate-limit resume, a manual close+assign) and they have already drifted
        apart once. The condition is the same for all of them and is checkable
        from state alone — a message written into generation N while the pane
        now runs generation N+1 was, by definition, never read by anyone.
        """
        try:
            from . import role_messages
        except Exception:
            return
        for project_ns, panes in list(getattr(self, "_panes_by_project", {}).items()):
            # One read per project per tick, not one per pane — this runs on
            # the Qt main thread every 5s.
            try:
                records = role_messages.read(RUNTIME_DIR, project_ns)
            except Exception:
                continue
            if not records:
                continue
            for role, pane in list(panes.items()):
                if role == LEAD.name:
                    continue
                session = getattr(pane, "session", None)
                if session is None or not getattr(session, "is_alive", False):
                    continue
                generation = int(getattr(pane, "_session_generation", 0))
                try:
                    pending = role_messages.undelivered_in(records, role, generation)
                except Exception:
                    continue
                if not pending:
                    continue
                for rec in pending:
                    msg_id = str(rec.get("id", ""))
                    body = str(rec.get("body", ""))
                    if not body:
                        role_messages.mark_abandoned(RUNTIME_DIR, project_ns, msg_id, "empty body")
                        continue
                    if int(rec.get("replays", 0)) + 1 > role_messages.MAX_REPLAYS:
                        role_messages.mark_abandoned(
                            RUNTIME_DIR, project_ns, msg_id, "replay cap reached"
                        )
                        continue
                    # Reuse the ordinary ready-prompt-aware delivery path so a
                    # replay lands under the same rules as any other write
                    # (never into a boot splash or an open modal).
                    self._send_when_ready(role, body, project=project_ns)
                    role_messages.mark_replayed(RUNTIME_DIR, project_ns, msg_id, generation)
                    _log_event(
                        "role_message_replayed",
                        project=project_ns,
                        role=role,
                        message_id=msg_id,
                        generation=generation,
                    )
                self._notify_lead(
                    project_ns,
                    (
                        f"♻️ [message-replayed] {role} pane ถูก respawn ระหว่างที่ยังมีข้อความจาก "
                        f"`takkub send` ค้างอยู่ — cockpit ส่งซ้ำให้แล้ว {len(pending)} ข้อความ "
                        f"(เดิมข้อความจะหายไปเงียบๆ พร้อม process เก่า) · "
                        f"ตรวจได้ด้วย `takkub messages --role {role}` (issue #277)"
                    ),
                    from_role="system",
                    note="message_replayed",
                    kind="message-replayed",
                )

    def kill_pane_children(
        self,
        role_name: str,
        project: str | None = None,
        pid: object = None,
        by: str = "lead",
    ) -> tuple[bool, str]:
        """#430 `takkub kill --role X [--pid N]` — kill the processes running
        under *role_name*'s pane (children of its PTY shell, recursive)
        without touching the pane itself. A `--pid` must be inside that
        pane's tree — the point is that a Lead can end a runaway build in
        one pane without ever reaching for `taskkill /IM` (#169). Audited
        to events.log as `pane_children_killed`."""
        role_name = (role_name or "").lower().strip()
        project_ns = self._resolve_project(project)
        pane = self._project_panes(project_ns).get(role_name)
        if pane is None or pane.session is None:
            return False, f"'{role_name}' has no live pane in project '{project_ns}'"
        root_pid = getattr(pane.session, "_pid", None)
        if not root_pid:
            return False, f"'{role_name}' pane has no process id yet"
        try:
            import psutil

            children = psutil.Process(root_pid).children(recursive=True)
        except Exception as exc:
            return False, f"could not enumerate processes under '{role_name}': {exc}"
        if pid not in (None, ""):
            try:
                want = int(pid)
            except (TypeError, ValueError):
                return False, f"--pid must be an integer, got {pid!r}"
            children = [c for c in children if c.pid == want]
            if not children:
                return False, (
                    f"pid {want} is not running under '{role_name}'s pane — refusing "
                    "(only processes inside that pane's tree can be killed this way)"
                )
        killed: list[str] = []
        failed: list[str] = []
        # Deepest first so a parent never respawns a child we already killed.
        for child in sorted(children, key=lambda c: -len(c.parents()) if c.is_running() else 0):
            try:
                label = f"{child.name()}({child.pid})"
            except Exception:
                label = f"pid {child.pid}"
            try:
                child.kill()
                killed.append(label)
            except Exception:
                failed.append(label)
        _log_event(
            "pane_children_killed",
            role=role_name,
            project=project_ns,
            by=by,
            killed=killed[:20],
            failed=failed[:20],
        )
        if not killed and not failed:
            return True, f"nothing running under '{role_name}' (pane idle)"
        msg = f"killed {len(killed)} process(es) under '{role_name}': {', '.join(killed[:8])}"
        if len(killed) > 8:
            msg += f" … (+{len(killed) - 8})"
        if failed:
            msg += f" · could not kill: {', '.join(failed[:5])}"
        return not failed, msg

    def _live_non_scaffolding_children(self, project_ns: str, role_name: str, session) -> list[str]:
        """Names of the processes running under *session* that represent real
        work, with the provider's own launcher scaffolding filtered out AND
        any child that has already exited excluded (#412).

        #288 split this out of :meth:`_warn_if_live_children`: the same list
        that explains what a close is about to kill is also the proof that a
        screen-silent pane is not actually wedged, and the stuck watchdog has
        to consult it *before* deciding to kill (see `_check_stuck_panes`).
        Returns ``[]`` on any probe failure — every caller must treat "no
        evidence of live work" as inconclusive, never as proof of idleness.

        #412 (real report: macOS, opencode): `psutil.Process(pid).children()`
        can still enumerate a child that has ALREADY finished — e.g. a `vite
        build` whose process exited but whose own parent (an intermediate
        shell/launcher under the pane) hasn't `wait()`-ed on it yet, so POSIX
        keeps it around as a zombie/defunct entry. That produced a "10
        subprocess(es) still running... about to be killed" warning on
        `takkub done` for work that had, in fact, already completed. Exit
        detection differs by OS (Windows has no zombie state at all), hence
        the explicit branch below rather than one exception-swallowing check
        assumed to cover both.
        """
        pid = getattr(session, "_pid", None)
        if not pid:
            return []
        try:
            import psutil

            children = psutil.Process(pid).children(recursive=True)
        except Exception:
            return []
        if not children:
            return []
        from .provider_config import effective_provider_for
        from .provider_spec import normalize_process_name, scaffolding_process_names_for

        provider = effective_provider_for(role_name, project=project_ns)
        scaffolding = scaffolding_process_names_for(provider)
        names: list[str] = []
        for child in children:
            try:
                status = child.status()
            except Exception:
                # Vanished between the children() snapshot and this check —
                # gone is the opposite of "still running", never a reason to
                # warn.
                continue
            if sys.platform == "win32":
                # No zombie state on Windows — a PID enumerated a moment ago
                # can still have exited by now (the job-object teardown this
                # warning precedes races against the child's own exit), so
                # recheck liveness directly rather than trusting the status
                # string alone.
                try:
                    if not child.is_running():
                        continue
                except Exception:
                    continue
            elif status == psutil.STATUS_ZOMBIE:
                # POSIX: exited but not yet reaped by its own parent — the
                # exact #412 shape (a finished `vite build`). Still enumerable
                # by name, but not "still running" by any meaningful sense.
                continue
            try:
                child_name = child.name()
            except Exception:
                continue
            if normalize_process_name(child_name) in scaffolding:
                continue
            names.append(child_name)
        return names

    def _warn_if_live_children(self, project_ns: str, role_name: str, session) -> None:
        """#234: best-effort check for a live subprocess tree under this
        pane's shell right before `terminate()` kills it (`taskkill /T` /
        job-object teardown, `pty_session.PtySession.terminate`) — e.g. a
        `docker compose build` still running when `done()`'s 2.5s auto-close
        (or a direct `close()`) tears the pane down. Cross-platform via
        `psutil.Process(pid).children(recursive=True)`, the same idiom
        `app.py`'s stale-process sweep already uses. Never blocks or delays
        the close — only surfaces what is about to be killed so Lead isn't
        left guessing why a build silently vanished (#234's own repro: a
        `docker images`/`docker ps` hunt after the fact was the only way to
        find out nothing had actually run).

        #272: `children` always includes the provider's own CLI-launcher
        scaffolding (npm .cmd shim → cmd.exe/conhost.exe/node.exe on Windows,
        codex's code-mode sandbox host, kimi's python interpreter, ...) —
        that was every single close, 100% false-positive rate, so this now
        subtracts each provider's confirmed `scaffolding_process_names` (via
        `provider_spec.scaffolding_process_names_for`) before deciding
        whether anything worth warning about is left. Only fires once real
        work (docker/pytest/build tooling/etc.) survives the filter.

        #286: the same 100%-false-positive shape came back for codex panes
        via `pwsh.exe` (its Windows shell-tool host) and, on every provider,
        via the pane's own in-flight `takkub done` call. Both are now in the
        filter — see `provider_spec.GENERIC_SCAFFOLDING_PROCESS_NAMES` and
        codex_spec's note. Worth remembering when the next name shows up:
        the discriminator that settled both cases was CONSTANCY, not
        plausibility — a child present on every close with an identical
        count, across tasks with nothing in common, is scaffolding; genuine
        unfinished work varies with the work. Check events.log's
        `close_kills_live_children` history before adding a name here.
        """
        names = self._live_non_scaffolding_children(project_ns, role_name, session)
        if not names:
            return
        detail = f" ({', '.join(names[:5])}{'…' if len(names) > 5 else ''})" if names else ""
        self._notify_lead(
            project_ns,
            f"⚠️ [{role_name} closing] {len(names)} subprocess(es) still running under this "
            f"pane are about to be killed{detail} — if the work wasn't actually finished, use "
            f"`takkub progress` next time instead of `done` until it is.",
            from_role=role_name,
            note="subprocess_kill_warning",
            kind="subprocess-kill-warning",
        )
        _log_event(
            "close_kills_live_children",
            role=role_name,
            project=project_ns,
            count=len(names),
            names=names[:10],
        )

    def close(
        self,
        role_name: str,
        project: str | None = None,
        force: bool = False,
        reason: str = "",
        suppress_pipeline: bool = False,
        suppress_auto_chain: bool = False,
    ) -> tuple[bool, str]:
        """Terminate a pane's session and remove it from the layout.

        force=True is for legitimate cockpit lifecycle (tab close, project switch).
        Never expose to CLI — teammates can only call `takkub done`.

        suppress_pipeline=True skips the "pane closed without done → mark the
        pipeline role failed + advance" path. Used by the stuck-pane watchdog,
        which closes then *respawns* the same role 2 s later: without this guard a
        recovery-close on a single-role hop would empty hop_pending and spuriously
        advance/complete the whole pipeline before the recovered pane comes back
        (whose later done() would then be a no-op). The respawn path re-honors the
        failure only if the respawn itself fails.

        suppress_auto_chain=True skips the auto-chain handoff check. Used by the
        stuck-pane watchdog (close→respawn cycle) so a recovery-close never fires
        the verify-hop pre-authorisation prematurely. External / user-initiated
        closes (force=True, tab close) do NOT suppress so the #8 behaviour holds:
        if a user forcibly removes the last auto-chain pane the handoff still fires.
        """
        role_name = role_name.lower().strip()
        project_ns = self._resolve_project(project)
        pane = self._project_panes(project_ns).get(role_name)
        if pane is None:
            # #409: a `role#N` target still parked behind the resource
            # governor's admission queue — `assign()` acked async but
            # spawn() hasn't run yet, so there's no pane entry although
            # `takkub status` already shows it queued (`_queued_resource_roles`)
            # — used to fail "unknown role" here even though `cancel_task_delivery`
            # already grew this exact fallback for `task cancel` (#303 item 2).
            # Reuse that same helper instead of leaving `close` unable to
            # abort a mid-flight assign until it finally spawns on its own.
            cancelled, cancel_msg = self._cancel_queued_resource_task(role_name, project_ns)
            if cancelled:
                return True, cancel_msg
            # #432: a KNOWN role with no pane (already closed by done()'s
            # own teardown, a worktree merge, or an earlier `close`) is a
            # no-op, not an error — the Lead's post-merge sequence
            # (`worktree merge` → `close --role X`) must read the same on
            # every pane regardless of whether the pane outlived the merge.
            # A genuinely unknown role name still errors.
            base_role, _shard_idx = _split_shard(role_name)
            if _role_by_name(base_role) is not None:
                _log_event("close_noop_no_pane", role=role_name, project=project_ns)
                return True, f"'{role_name}' has no pane open right now — already closed (no-op)"
            return False, cancel_msg
        was_alive = pane.session is not None
        if was_alive:
            # Lead is permanent; only force=True (tab close, project switch) may terminate
            if role_name == LEAD.name and not force:
                _log_event("close_ignored", role=role_name, reason="lead_protected")
                return True, "lead close ignored (protected)"
            # mark exit as expected so the pane doesn't surface "exited"/crash
            pane.mark_expected_exit()
            self._warn_if_live_children(project_ns, role_name, pane.session)
            _closing_cwd = getattr(pane, "_session_cwd", None)
            pane.session.terminate()
            pane.set_state("empty", note=None)
            # Planted AGENTS.md leaves with the last pane that used this cwd —
            # otherwise an IDE-launched CLI in that project reads it and
            # thinks it is a pane (2026-08-26 report).
            self._release_planted_context_if_unused(_closing_cwd, exclude=(project_ns, role_name))
        key = f"{project_ns}::{role_name}"
        resource_token = getattr(self, "_resource_tokens", {}).pop((project_ns, role_name), None)
        resource_governor = getattr(self, "_resource_governor", None)
        if resource_governor is not None:
            resource_governor.release_slot(resource_token)
            resource_governor.cancel_waiting(project_id=project_ns, pane_id=role_name)
        delivery_id = getattr(self, "_last_delivery_ids", {}).pop((project_ns, role_name), None)
        delivery_manager = getattr(self, "_delivery_manager", None)
        if delivery_id and delivery_manager is not None:
            # Routine: closing a pane that still has a delivery on the books
            # is not a delivery malfunction (#331).
            delivery_manager.mark_failed(delivery_id, "pane_closed")
        if delivery_manager is not None:
            delivery_manager.cancel_for_session(
                project_ns,
                role_name,
                int(getattr(pane, "_session_generation", 0)),
            )
        # #8: read auto_chain flag BEFORE popping state so a pane that is
        # closed externally (e.g. forced close) still triggers the auto-chain
        # handoff if it was the last pending auto-chain pane in the project.
        _ps_close = getattr(self, "_pane_state", {}).get(key)
        had_auto_chain_close = _ps_close.auto_chain if _ps_close is not None else False
        had_pipeline_run_id_close = _ps_close.pipeline_run_id if _ps_close is not None else None
        # #81: a pane closed WITHOUT a done() (manual close / tab close) still
        # holds worktree state here (done() pops it first, so this is None on the
        # done→auto-close path — no double finalize).
        had_worktree_close = _ps_close.worktree if _ps_close is not None else None

        # Task Ledger (A7): same "still holds state" signal as worktree above —
        # `_ps_close is not None` means this pane never called done(), so flip
        # its open row to "closed" (abandoned). The done()→auto-close 2.5s later
        # is a no-op here since done() already popped the state (and already
        # flipped the row to ok/fail).
        if _ps_close is not None:
            try:
                from .task_ledger import mark_done

                ledger_warning = mark_done(project_ns, role_name, "closed")
                if ledger_warning:
                    self._notify_lead(
                        project_ns,
                        ledger_warning,
                        from_role=role_name,
                        note="",
                        kind="ledger-warning",
                    )
                self.ledgerChanged.emit(project_ns)
            except Exception:
                _log_event("ledger_hook_error", role=role_name, project=project_ns, stage="close")

        # #280: report-at-close. `_ps_close is not None` means this pane never
        # called done(), so nothing else will ever carry what the watchdogs saw
        # about it — this is the last moment it can be said. A pane that DID
        # report has already drained its own health in done(), so this is
        # silent for it rather than a second copy.
        if _ps_close is not None and role_name != LEAD.name:
            close_health = self._drain_pane_health(project_ns, role_name)
            if close_health:
                self._notify_lead(
                    project_ns,
                    f"🔚 [{role_name} closed] ปิดโดยไม่มีรายงาน done\n{close_health}",
                    from_role=role_name,
                    note="close_health",
                    kind="close-health",
                )

        self._idle_state.pop(key, None)
        # #422 item 3: keep the session id for the `close` event below — the
        # PaneState that carries it is popped right here.
        _closed_session_uuid = self._session_uuid_for(key)
        getattr(self, "_pane_state", {}).pop(key, None)
        getattr(self, "_last_done_task_ids", {}).pop(key, None)

        if had_worktree_close:
            self._finalize_worktree(project_ns, role_name, had_worktree_close)
        # Revoke the pane's capability token so stale done/send requests from
        # the closing pane are rejected after it terminates.
        _pane_tokens = getattr(self, "_pane_tokens", {})
        _revoke_keys = [t for t, v in _pane_tokens.items() if v == (project_ns, role_name)]
        for _tok in _revoke_keys:
            _pane_tokens.pop(_tok, None)

        if not suppress_auto_chain:
            self._maybe_fire_auto_chain_handoff(project_ns, had_auto_chain_close)

        # Pipeline: pane closed without done (crash / forced close) — mark failed.
        # Advance if all roles in the hop are now done or failed.
        # suppress_pipeline (stuck-watchdog recovery-close) skips this: the same
        # role respawns 2 s later, so a single-role hop must NOT advance here.
        if had_pipeline_run_id_close and not suppress_pipeline:
            pipeline_key_close = f"{project_ns}::{had_pipeline_run_id_close}"
            pl_run_close = self._pipeline_runs.get(pipeline_key_close)
            if pl_run_close and not pl_run_close.closed:
                pl_run_close.hop_pending.discard(role_name)
                pl_run_close.hop_failed.add(role_name)
                if not pl_run_close.hop_pending:
                    self._advance_pipeline(project_ns, pipeline_key_close, pl_run_close)

        # For teammates, fully remove from the layout so the right column
        # collapses back. Lead stays as it always anchors the cockpit.
        # The project namespace travels with the signal so main_window
        # can route the removal to the correct tab even when the user
        # is viewing a different project at the moment of close (the
        # `done`-triggered close fires 2.5 s after the agent reports
        # done, plenty of time for a tab switch).
        # paneClosed never fires for Lead — tab close handles UI teardown separately via deleteLater
        if role_name != LEAD.name:
            self.paneClosed.emit(role_name, project_ns)
        self.statusChanged.emit()
        _log_event(
            "close",
            role=role_name,
            force=force,
            reason=reason,
            project=project_ns,
            session_uuid=_closed_session_uuid,
        )
        # #406: the pane just removed may have been the last mb-Chrome user.
        self._schedule_native_chrome_idle_release()
        # Fan-out queue (flag-gated): a genuine teammate close frees a slot —
        # drain one queued assign on the next event-loop tick (deferred so we
        # never re-enter the close/paneClosed emit stack, per the 0xc0000409
        # teardown-reentrancy lesson). Recovery-closes (suppress_pipeline) keep
        # the pane, so they don't free a slot and don't drain.
        if role_name != LEAD.name and not suppress_pipeline and _fanout_queue_enabled():
            QTimer.singleShot(0, lambda p=project_ns: self._drain_fanout_queue(p))
        return True, f"{role_name} closed"

    def toggle_provider(self, provider: str, disabled: bool) -> tuple[bool, str]:
        """Flip codex or gemini between enabled/disabled globally across all tabs.

        Persists to ~/.takkub/disabled-providers.json then broadcasts a
        `[system] <provider> ENABLED/DISABLED ...` message into every Lead
        pane in every project so live sessions notice the change without
        having to poll the file.

        Returns (ok, message). Fails on unknown provider or if the state
        file can't be written (disk full/permissions) — callers must not
        assume this always succeeds and should surface `message` to the user
        rather than proceeding as if the toggle took effect.
        """
        from .provider_state import TOGGLABLE, set_disabled

        provider = provider.lower().strip()
        if provider not in TOGGLABLE:
            return False, f"unknown provider: {provider!r}"

        try:
            set_disabled(provider, disabled)
        except OSError as e:
            return False, f"could not persist provider state: {e}"

        word = "DISABLED" if disabled else "ENABLED"
        suffix = (
            f"Claude will substitute for the {provider} role (same slot, claude-backed); "
            "you may still propose/fire it — just note the substitution to the user."
            if disabled
            else f"{provider} CLI available again — it will back its role natively."
        )
        notice = f"[system] {provider} provider {word}. {suffix}"

        # Broadcast to every Lead pane across all project tabs. Iterate
        # _panes_by_project directly because we want every Lead, not just
        # the active project's Lead.
        for _project_ns, panes in self._panes_by_project.items():
            lead = panes.get(LEAD.name)
            if lead and lead.session and lead.session.is_alive:
                _tog_sess = lead.session
                _tog_sess.write(notice)
                # Same trailing-CR delay as done() so the inject lands
                # after the inline text not before it.
                _delayed_enter(lead, _tog_sess, 150)
                self.leadInjected.emit(notice)
            # If Lead isn't alive in this project, the next spawn's
            # _render_lead_context() will read the fresh state — no need
            # to queue per-message for this case (unlike done notices,
            # which carry per-event info that mustn't be lost).

        self.providerStateChanged.emit(provider, disabled)
        _log_event("provider_toggled", provider=provider, disabled=disabled)
        return True, f"{provider} {word.lower()}"

    def set_plan_tier(self, tier: str) -> tuple[bool, str]:
        """Set the account plan (pro/max) globally and persist it.

        Pins (or unpins) the Lead's model at the NEXT spawn: Pro forces a
        standard-context model so the 1M-context credit error can't bite,
        Max lets the Lead inherit the user default again. Already-running
        Lead panes keep their current model until respawn — we broadcast a
        `[system]` notice so the live session knows, and (under Pro) stops
        proposing 1M-context work.

        Returns (ok, message). Fails only on an unknown tier.
        """
        from . import plan_tier

        tier = tier.lower().strip()
        if tier not in plan_tier.TIERS:
            return False, f"unknown plan tier: {tier!r}"

        plan_tier.set_current(tier)

        if tier == plan_tier.PRO:
            notice = (
                "[system] account plan set to PRO. 1M-context model is "
                "unavailable (usage-credits gated) — do not propose or rely on "
                "it. New Lead panes pin to a standard-context model."
            )
        else:
            notice = (
                "[system] account plan set to MAX. Full model access restored "
                "(incl. 1M context). Applies to newly spawned panes."
            )

        # Broadcast to every Lead pane across all project tabs (same pattern
        # as toggle_provider). The model pin itself only lands at the next
        # spawn, but the notice keeps live sessions in sync.
        for _project_ns, panes in self._panes_by_project.items():
            lead = panes.get(LEAD.name)
            if lead and lead.session and lead.session.is_alive:
                _tier_sess = lead.session
                _tier_sess.write(notice)
                _delayed_enter(lead, _tier_sess, 150)
                self.leadInjected.emit(notice)

        self.planTierChanged.emit(tier)
        _log_event("plan_tier_set", tier=tier)
        return True, f"plan set to {tier}"

    def set_exec_mode(self, mode: str) -> tuple[bool, str]:
        """Set the execution mode (solo/parallel) globally and persist it.

        SOLO is the cockpit's original 1-agent-per-role behaviour. PARALLEL tells
        the Lead, on the NEXT task, to decompose an independent-multi-feature
        request and fan out several instances per role (frontend#1..#K, …) so the
        features finish concurrently. The instruction reaches the Lead via the
        system-prompt block in lead_context (read at spawn); we also broadcast a
        `[system]` notice so a live Lead switches planning style immediately.

        Returns (ok, message). Fails only on an unknown mode.
        """
        from . import exec_mode

        mode = mode.lower().strip()
        if mode not in exec_mode.MODES:
            return False, f"unknown execution mode: {mode!r}"

        exec_mode.set_current(mode)

        if mode == exec_mode.PARALLEL:
            notice = (
                "[system] execution mode → PARALLEL (multi). When a request has "
                "K independent features, plan a decomposition and fan out one "
                "instance per role per feature (frontend#1..#K, backend#1..#K). "
                "No hard numeric cap — sequence independent tasks in waves by "
                "per-role cost instead of firing everything at once. Independent "
                "features only; keep dependent work serial."
            )
        else:
            notice = (
                "[system] execution mode → SOLO (1:1). One agent per role; work "
                "features sequentially. (No multi-instance fan-out.)"
            )

        for _project_ns, panes in self._panes_by_project.items():
            lead = panes.get(LEAD.name)
            if lead and lead.session and lead.session.is_alive:
                _em_sess = lead.session
                _em_sess.write(notice)
                _delayed_enter(lead, _em_sess, 150)
                self.leadInjected.emit(notice)

        self.execModeChanged.emit(mode)
        _log_event("exec_mode_set", mode=mode)
        return True, f"execution mode set to {mode}"

    @staticmethod
    def _uncommitted_warning(from_role: str, porcelain_out: str) -> str | None:
        """Build the Lead `[requires-commit]` warning from `git status --porcelain`
        stdout, or None when the tree is clean. Pure → unit-tested. (M2)"""
        dirty = (porcelain_out or "").strip()
        if not dirty:
            return None
        files_preview = dirty[:200]
        return (
            f"⚠ [requires-commit] {from_role} มี uncommitted changes รอ Lead review + commit:\n"
            f"{files_preview}"
        )

    def _check_uncommitted_async(self, project_ns: str, from_role: str, cwd: str) -> None:
        """Run `git status --porcelain` WITHOUT blocking the Qt main thread, then
        deliver a follow-up warning to Lead if the tree is dirty. (M2)

        Uses QProcess (driven by the Qt event loop) rather than a worker thread,
        so the completion handler runs on the main thread exactly like any slot —
        there is NO cross-thread access to orchestrator / pane state, hence no
        race. A watchdog timer bounds a hung git the way the old timeout=10 did.
        """
        proc = QProcess(self)
        proc.setWorkingDirectory(cwd)
        timeout = QTimer(self)
        timeout.setSingleShot(True)
        timeout.setInterval(10_000)
        state = {"done": False}

        def _settle(reason: str | None) -> None:
            # reason is None on a clean finish; a string when we bailed (skip warn).
            if state["done"]:
                return
            state["done"] = True
            timeout.stop()
            # Don't leak the watchdog QTimer (parented to self → would live for the
            # whole cockpit run, accumulating one per requires-commit done).
            timeout.deleteLater()
            if reason is not None:
                _log_event(
                    "done_commit_gate_skipped", role=from_role, project=project_ns, reason=reason
                )
                try:
                    proc.kill()
                except Exception:
                    pass
                proc.deleteLater()
                return
            try:
                out = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
            except Exception:
                out = ""
            proc.deleteLater()
            warning = self._uncommitted_warning(from_role, out)
            if warning is None:
                return
            _log_event(
                "done_with_uncommitted",
                role=from_role,
                project=project_ns,
                reason="dirty_tree",
                files=warning[-200:],
            )
            self._notify_lead(
                project_ns, warning, from_role=from_role, note="", kind="done-with-uncommitted"
            )

        proc.finished.connect(lambda _code, _status: _settle(None))
        proc.errorOccurred.connect(lambda _e: _settle("git_proc_error"))
        timeout.timeout.connect(lambda: _settle("timeout"))
        timeout.start()
        proc.start("git", ["status", "--porcelain"])

    @staticmethod
    def _evidence_stat_mtime(path: pathlib.Path) -> float | None:
        """`path.stat().st_mtime`, retrying past a transient Windows lock.

        Returns None (skip this file) if every attempt fails — done() must
        never blow up because a screenshot was mid-write or briefly locked.
        """
        for attempt in range(_EVIDENCE_STAT_RETRIES):
            try:
                return path.stat().st_mtime
            except PermissionError:
                if attempt == _EVIDENCE_STAT_RETRIES - 1:
                    return None
                time.sleep(_EVIDENCE_STAT_RETRY_SLEEP_SEC)
            except OSError:
                return None
        return None

    @staticmethod
    def _evidence_file_size(path: pathlib.Path) -> int:
        """Best-effort `st_size`; 0 (never raises) on any read hiccup — a
        locked/vanished file still sorts and formats fine, matching
        `_evidence_stat_mtime`'s degrade-silently contract (issue #159)."""
        try:
            return path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _evidence_looks_valid_image(path: pathlib.Path) -> bool:
        """Cheap magic-byte sniff — catches a 0-byte/truncated/wrong-content
        file saved under an image extension, without a full decode or an
        image-lib dependency (issue #159). Unreadable file → treat as valid;
        that failure is already surfaced via the size check / mtime skip."""
        suffix = path.suffix.lower()
        try:
            with open(path, "rb") as f:
                header = f.read(16)
        except OSError:
            return True
        if suffix == ".webp":
            return header.startswith(b"RIFF") and header[8:12] == b"WEBP"
        prefixes = _EVIDENCE_MAGIC_PREFIXES.get(suffix)
        if prefixes is None:
            return True
        return header.startswith(prefixes)

    @staticmethod
    def _evidence_content_hash(path: pathlib.Path, size: int) -> str | None:
        """md5 of `path`'s bytes, for cross-file duplicate detection (issue
        #182). `None` on any read failure or when `size` exceeds
        `_EVIDENCE_DEDUP_MAX_BYTES` — the caller must treat that as "unknown,
        can't compare" rather than "empty file", so it never collides with a
        real hash by coincidence."""
        if size > _EVIDENCE_DEDUP_MAX_BYTES:
            return None
        try:
            return hashlib.md5(path.read_bytes()).hexdigest()
        except OSError:
            return None

    @classmethod
    def _evidence_format_entry(
        cls, path: pathlib.Path, size: int, dup_of: pathlib.Path | None = None
    ) -> str:
        """`path (12.3KB)`, tagged `⚠small`/`⚠bad-header` when the file looks
        like a failed capture rather than a real screenshot (issue #159), or
        `⚠dup-of:<name>` when it's byte-identical to an earlier file in the
        same evidence batch (issue #182)."""
        reasons = []
        if size < _EVIDENCE_SUSPECT_MIN_BYTES:
            reasons.append("small")
        if not cls._evidence_looks_valid_image(path):
            reasons.append("bad-header")
        if dup_of is not None:
            reasons.append(f"dup-of:{dup_of.name}")
        tag = f" ⚠{'+'.join(reasons)}" if reasons else ""
        posix_path = str(path).replace("\\", "/")
        return f"{posix_path} ({size / 1024:.1f}KB{tag})"

    @classmethod
    def _find_evidence_files(
        cls, directory: pathlib.Path, assign_ts: float, now: float
    ) -> list[tuple[float, pathlib.Path, int]]:
        """Recursively collect `(mtime, path, size)` for settled evidence
        images under `directory` that landed after `assign_ts`. Empty list on
        a missing/unreadable dir — never raises (issue #5)."""
        try:
            candidates = list(directory.rglob("*")) if directory.is_dir() else []
        except OSError:
            candidates = []

        found: list[tuple[float, pathlib.Path, int]] = []
        for path in candidates:
            try:
                if not path.is_file() or path.suffix.lower() not in _EVIDENCE_EXTENSIONS:
                    continue
            except OSError:
                continue
            mt = cls._evidence_stat_mtime(path)
            if mt is None or mt < assign_ts or mt > now - _EVIDENCE_SETTLE_SEC:
                continue
            found.append((mt, path, cls._evidence_file_size(path)))
        return found

    @classmethod
    def _scan_done_evidence(
        cls, project_ns: str, from_role: str, assign_ts: float, note: str = ""
    ) -> str:
        """Scan the pane's artifacts dir for screenshots newer than `assign_ts`.

        Returns a `'📸 evidence: <paths>'` suffix to append to the done notice
        when fresh screenshot files were found. Each path is annotated with
        its size, e.g. `login.png (43.2KB)`, and tagged `⚠small`/`⚠bad-header`
        when the file looks like a failed capture rather than a real
        screenshot — a file existing is not proof it's a *good* screenshot
        (issue #159). The file still counts as evidence either way (a bad
        shot is still a shot the role took — Lead judges whether to send it
        back), it's just flagged for Lead's attention rather than silently
        trusted.

        Otherwise, for a warn-role (qa/critic/designer/reviewer): if `note`
        itself cites evidence (a path-like or test-result token, see
        `_EVIDENCE_CITE_RE`) returns `''` — the note is trusted at face value
        — else returns a bare `'⚠ no evidence cited'` warning. Everyone else
        silently gets `''`. Degrades silently on any filesystem hiccup — a
        missing/unreadable artifacts dir just yields no evidence, never an
        exception (issue #5).

        Issue #109: a flat scan over the whole project artifacts dir attaches
        *any* pane's screenshot to *any* other pane's done() if the mtimes
        overlap. To attribute evidence correctly, prefer the pane's own
        `<artifacts_dir>/<role>/` subdir (including its own `screenshots/`)
        first; only fall back to the old flat project-wide scan — tagged
        `(shared dir)` since it can't be pinned to just this pane — when the
        role subdir has nothing. This keeps the qa→critic shared
        `screenshots/` pickup convention working unchanged for panes that
        haven't adopted a per-role subdir yet.

        Issue #165: the flat fallback above is itself scoped to
        `_EVIDENCE_WARN_ROLES` (qa/critic/designer/reviewer) — the only roles
        the shared-screenshots convention is actually for. A role outside
        that set (backend, devops, …) that never wrote to its own subdir
        gets `''`, never another pane's screenshots by coincidence of
        overlapping assign windows — confirmed live: a pure-Python backend
        pane's done() picked up critic's unrelated screenshots this way.
        """
        if assign_ts <= 0:
            # No tracked assignment (pane never went through _assign_dispatch,
            # e.g. a bare `takkub done` in a test or a legacy PaneState) — we
            # have no window to scan, so say nothing rather than guess.
            return ""

        base_role, _ = _split_shard(from_role)
        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        artifacts_dir = RUNTIME_DIR / "exports" / today / project_ns

        found = cls._find_evidence_files(artifacts_dir / base_role, assign_ts, now)
        shared = False
        # Issue #165: the flat/shared fallback used to run for ANY role, so a
        # pane that never touches a browser (e.g. backend doing pure-Python
        # work) could get another pane's screenshots (e.g. critic's) folded
        # into its own done() evidence line — same-project, overlapping
        # assign windows is all it took. The fallback is only a legitimate
        # signal for the roles the qa→critic/designer shared screenshots/
        # convention actually applies to; scope it to _EVIDENCE_WARN_ROLES so
        # a role outside that set never inherits evidence it didn't produce.
        if not found and base_role in _EVIDENCE_WARN_ROLES:
            found = cls._find_evidence_files(artifacts_dir, assign_ts, now)
            shared = True

        if found:
            found.sort(key=lambda item: item[0], reverse=True)
            newest = found[:_EVIDENCE_MAX_FILES]
            # Issue #182: flag a file whose content is byte-identical to an
            # earlier one in this same batch — the first occurrence of a hash
            # is trusted at face value, every later one is tagged so Lead
            # doesn't mistake a repeated frame for N distinct captures.
            seen_hashes: dict[str, pathlib.Path] = {}
            entries = []
            for _, p, size in newest:
                digest = cls._evidence_content_hash(p, size)
                dup_of = None
                if digest is not None:
                    first = seen_hashes.get(digest)
                    if first is not None:
                        dup_of = first
                    else:
                        seen_hashes[digest] = p
                entries.append(cls._evidence_format_entry(p, size, dup_of=dup_of))
            paths = ", ".join(entries)
            suffix = " (shared dir)" if shared else ""
            return f"📸 evidence: {paths}{suffix}"
        if base_role not in _EVIDENCE_WARN_ROLES:
            return ""
        if note and _EVIDENCE_CITE_RE.search(note):
            return ""
        return "⚠ no evidence cited"

    @staticmethod
    def _build_blocked_handoff(from_role: str, body: str, what: str) -> str:
        """Lead-facing prompt for a report that is BLOCKED, not FAILED (#296).

        Deliberately offers no role and no fix loop: the next action belongs to
        a human, and naming a role here is exactly what made Lead route a
        missing super-admin password to backend twice in one session.
        """
        missing = f" — {what}" if what else ""
        return (
            f"[{from_role} BLOCKED] {body}\n\n"
            f"🚧 รายงานนี้คือ **ติด blocker ไม่ใช่เจอบั๊ก**{missing}\n"
            "งานรันไม่ได้เพราะขาดของนอกระบบ ไม่ใช่โค้ดพัง — **ไม่มี role ไหนแก้ได้**\n"
            "1. อ่านว่าขาดอะไร แล้วสรุปให้เจ้าของเป็นข้อๆ (ต้องการอะไร เพื่ออะไร)\n"
            "2. รอเจ้าของจัดหาให้ แล้วค่อย re-assign งานเดิมกลับไปที่ role เดิม\n"
            "**ห้าม** propose ให้ role อื่นไปแก้ระบบเพื่อให้ผ่าน (เช่น รีเซ็ต/เดารหัสผ่าน "
            "หรือปลดการยืนยันตัวตน) — นั่นคือการแก้ของที่ไม่ได้พัง"
        )

    @staticmethod
    def _build_verify_fail_handoff(from_role: str, note: str) -> str:
        """Lead-facing prompt when a pane reports `done --fail` (QA/verify failed).

        Surfaces the failure and tells Lead to PROPOSE a fix loop — never
        auto-fire. Feedback routing stays human-in-the-loop (propose-then-fire),
        matching the cockpit's safety doctrine.
        """
        body = note.strip() or "(no detail given)"
        # #296: BLOCKED before FAILED. A pane that couldn't run because
        # something outside the codebase is missing has found no bug, so the
        # whole fix-loop framing is wrong — there is no role to route to, only
        # a human who can supply the missing thing. Routing it anyway sends a
        # teammate to "fix" working code; in the reported case that meant
        # backend touching a live auth system so QA could get in.
        try:
            from .routing_planner import classify_blocked

            is_blocked, what = classify_blocked(body)
        except Exception:
            is_blocked, what = False, ""
        if is_blocked:
            return Orchestrator._build_blocked_handoff(from_role, body, what)
        # Tier 2c: signature-based suggestion for which role the fix loop
        # should target. A suggestion only — the Lead proposes, user confirms.
        suggest = ""
        try:
            from .routing_planner import classify_failure

            fix_role, why = classify_failure(body)
            if fix_role:
                suggest = f"🔎 signature ชี้ไปที่ **{fix_role}** ({why}) — เสนอ route กลับ role นี้ก่อน\n"
        except Exception:
            pass
        return (
            f"[{from_role} FAILED] {body}\n\n"
            "⚠️ verify/QA รายงาน FAIL — เสนอ fix loop (propose-then-fire, ห้าม auto):\n"
            f"{suggest}"
            "1. อ่าน failure ข้างบน หา root cause\n"
            "2. Propose assign role ที่ทำงานนั้นให้แก้ (propose table + cwd + รอ confirm)\n"
            "3. แก้เสร็จ → re-verify (QA ท้ายสุดเสมอ)\n"
            "อย่าเพิ่ง fire — render proposal ให้ user confirm ก่อน"
        )

    @staticmethod
    def _build_delivery_pointer_failure_notice(
        from_role: str, note: str, task_file: str | None
    ) -> str:
        """Lead-facing notice for a `done --fail` that `is_delivery_pointer_failure`
        classified as a TASK-DELIVERY failure, not a work failure (issue
        #273) — the pane couldn't open the file-pointer handoff, so the
        assigned work never started.

        Deliberately NOT `_build_verify_fail_handoff`'s fix-loop-propose
        wording: there is no root cause in the WORK to find yet, so telling
        Lead to hunt for one wastes time on a task that was never
        attempted. Points straight at the fix instead — just reassign.
        """
        body = note.strip() or "(no detail given)"
        file_note = f" ({task_file})" if task_file else ""
        return (
            f"[{from_role} delivery-failed] {body}\n\n"
            f"⚠️ นี่คือ delivery failure ไม่ใช่ task failure (#273) — pane เปิด "
            f"task-pointer file ไม่ได้{file_note} จึงยังไม่เคยเริ่มงานเลยแม้แต่บรรทัดเดียว "
            "ไม่ต้องหา root cause ของ 'งาน' (ยังไม่มีงานให้หา) แค่ assign ใหม่ก็พอ — "
            "ถ้า pane role/provider นี้ยังเจอซ้ำ ให้เช็ค "
            "ProviderSpec.supports_agent_file_read ของ provider นั้น"
        )

    @staticmethod
    def _condense_done_note(
        raw_note: str, merged_note: str, evidence_line: str, session_md_path: str | None
    ) -> str:
        """Build the Lead-facing body for a clean (non-``--fail``) `done()` report.

        Symmetrizes the return path with the file-based task handoff (issue
        #1): a note at/above ``TASK_HANDOFF_THRESHOLD`` chars condenses to its
        first line (truncated to ~200 chars) plus a pointer at the session-md
        file ``_save_decision_note`` already wrote — mirroring
        ``_task_handoff_pointer``'s task→pane wording for the pane→Lead
        direction. Short notes, or a note whose file failed to write (no
        ``session_md_path``), paste inline unchanged — same as before this
        existed. The evidence line always stays on the notice tail regardless
        of truncation (it's the signal qa/critic/designer workflows depend
        on).
        """
        if len(raw_note) < TASK_HANDOFF_THRESHOLD or not session_md_path:
            return merged_note.rstrip()
        headline = raw_note.strip().splitlines()[0] if raw_note.strip() else ""
        # #241: back up to a word boundary rather than a hard char slice, so
        # the headline never severs a word/identifier mid-way — the pointer
        # right after it means nothing is actually lost, just not inline.
        headline = _truncate_at_word_boundary(headline, 200)
        body = f"{headline} · 📄 รายงานเต็ม: {session_md_path} (เปิดด้วย file-read tool)"
        if evidence_line:
            body = f"{body}\n{evidence_line}"
        return body

    @staticmethod
    def _compute_digest_facts(
        from_role: str,
        ref: str | None,
        headline: str,
        report_path: str | None,
        had_worktree: dict | None,
        pane_cwd: str | None,
        assign_base_sha: str | None,
        assign_git_root: str | None,
        assign_dirty_snapshot: dict[str, tuple[str, int | None, int | None]] | None,
        git_facts: dict | None = None,
    ) -> tuple[object, dict | None]:
        """One-shot git-fact gather for the Lead Inbox Digest bullet (#245,
        follow-up to #244). Fired exactly once per `done()` event — never
        per-tick (see #229/#244 for why that boundary matters on the Qt
        main thread).

        Returns ``(facts, precomputed)``. For an isolated worktree pane,
        *precomputed* is the SAME git reads `_finalize_worktree` needs a few
        lines later in `done()`, so that method reuses them instead of
        re-running the identical subprocess calls twice per event.
        *precomputed* is always None for a shared-tree pane — there is
        nothing for `_finalize_worktree` to reuse there (it only runs at
        all for worktree panes).

        Provider-neutral (#103): every field here is git state or a plain
        cwd/sha string the orchestrator already carries — nothing read from
        a specific CLI's terminal output.
        """
        from .digest_facts import DigestFacts, union_files_touched
        from .worktree_manager import (
            WorktreeInfo,
            WorktreeManager,
            changed_dirty_paths,
            parse_porcelain_paths,
            summarize_diffstat,
        )

        mgr = WorktreeManager()

        # #408: *git_facts* is `WorktreeManager.collect_done_git_facts`'s
        # output when the caller already ran the git reads off the Qt
        # thread; every `mgr.*` call below is skipped in that case.
        gf = git_facts if isinstance(git_facts, dict) else None
        if had_worktree:
            info = WorktreeInfo.from_dict(had_worktree)
            if gf is not None and gf.get("kind") == "worktree":
                commits = int(gf.get("commits", 0))
                dirty = bool(gf.get("dirty", False))
                uncommitted = int(gf.get("uncommitted", 0))
            else:
                commits = mgr.commit_count(info)
                dirty = mgr.is_dirty(info)
                uncommitted = mgr.uncommitted_count(info) if dirty else 0
            if commits > 0:
                merge_conflicts = (
                    gf.get("merge_conflicts")
                    if gf is not None and gf.get("kind") == "worktree"
                    else mgr.merge_conflicts_with_base(info.git_root, info.branch)
                )
                merge_note = ""
            else:
                # Nothing committed yet — "merge clean?" is not a question
                # that applies (there's nothing on the branch to merge).
                merge_conflicts = None
                merge_note = "N/A (ยังไม่มี commit)"
            diffstat = (
                str(gf.get("diffstat", ""))
                if gf is not None and gf.get("kind") == "worktree"
                else mgr.diffstat(info)
            )
            pushed = (
                bool(gf.get("pushed", False))
                if gf is not None and gf.get("kind") == "worktree"
                else mgr.remote_branch_exists(info.git_root, info.branch)
            )
            files_touched, dirs = summarize_diffstat(diffstat)
            facts = DigestFacts(
                role=from_role,
                ref=ref,
                branch=info.branch,
                commits_ahead=commits,
                uncommitted=uncommitted,
                merge_conflicts=merge_conflicts,
                merge_note=merge_note,
                pushed=pushed,
                files_touched=files_touched,
                files_dirs=tuple(dirs),
                report_path=report_path,
                headline=headline,
            )
            precomputed = {
                "commits": commits,
                "dirty": dirty,
                "uncommitted": uncommitted,
                "merge_conflicts": merge_conflicts,
                "diffstat": diffstat,
                "pushed": pushed,
            }
            return facts, precomputed

        # Shared-tree pane: HEAD covers committed changes in the assignment
        # interval; the dirty-path metadata snapshot covers uncommitted state
        # while excluding unchanged dirt that pre-dated assign (#251).
        shared_gf = gf if gf is not None and gf.get("kind") == "shared" else None
        if shared_gf is not None:
            branch = shared_gf.get("branch")
        else:
            branch = mgr.current_branch(pane_cwd) if pane_cwd else None
        if (
            not pane_cwd
            or not assign_base_sha
            or not assign_git_root
            or assign_dirty_snapshot is None
        ):
            return (
                DigestFacts(
                    role=from_role,
                    ref=ref,
                    branch=branch,
                    report_path=report_path,
                    headline=headline,
                    files_note=(
                        "ตรวจไม่ได้ (snapshot ตอน assign ไม่ครบ — cwd ไม่ใช่ git repo, "
                        "HEAD ว่าง, หรืออ่าน git status ไม่สำเร็จ)"
                    ),
                ),
                None,
            )
        # One porcelain read feeds BOTH the uncommitted count and the metadata
        # comparison; done() does not pay for two status subprocesses.
        if shared_gf is not None:
            commits_ahead = int(shared_gf.get("commits_ahead", 0))
            porcelain = shared_gf.get("porcelain")
        else:
            commits_ahead = mgr.commits_since(pane_cwd, assign_base_sha)
            porcelain = mgr.shared_tree_status_porcelain(pane_cwd)
        if porcelain is None:
            return (
                DigestFacts(
                    role=from_role,
                    ref=ref,
                    branch=branch,
                    commits_ahead=commits_ahead,
                    merge_conflicts=None,
                    merge_note=(
                        "N/A (shared tree — commit อยู่บน branch ที่ Lead เห็นอยู่แล้ว ไม่ต้อง merge)"
                    ),
                    report_path=report_path,
                    headline=headline,
                    files_note="ตรวจไม่ได้ (อ่าน git status ตอน done ไม่สำเร็จ)",
                ),
                None,
            )
        uncommitted = len(parse_porcelain_paths(porcelain))
        current_dirty_snapshot = mgr.dirty_snapshot(assign_git_root, porcelain)
        changed_uncommitted = changed_dirty_paths(assign_dirty_snapshot, current_dirty_snapshot)
        diffstat = (
            str(shared_gf.get("diffstat", ""))
            if shared_gf is not None
            else mgr.diffstat_since(pane_cwd, assign_base_sha)
        )
        files_touched, dirs = union_files_touched(diffstat, changed_uncommitted)
        facts = DigestFacts(
            role=from_role,
            ref=ref,
            branch=branch,
            commits_ahead=commits_ahead,
            uncommitted=uncommitted,
            merge_conflicts=None,
            merge_note=("N/A (shared tree — commit อยู่บน branch ที่ Lead เห็นอยู่แล้ว ไม่ต้อง merge)"),
            files_touched=files_touched,
            files_dirs=tuple(dirs),
            files_note=(
                "เทียบ HEAD + dirty path/mtime/size ตอน assign — shared tree ยังอาจรวม "
                "การเปลี่ยนของ pane อื่นที่เกิดในช่วงเวลาเดียวกัน"
            ),
            report_path=report_path,
            headline=headline,
        )
        return facts, None

    def _pane_reports_undelivered_task(self, project_ns: str, role: str, pane) -> bool:
        """(#278/#276) True when this pane is reporting on an assignment that
        provably never reached it.

        The failure this exists for (measured in #278): codex read the
        injected orchestrator instructions, decided "answered the prompt =
        finished the job", and ran `takkub done` before the task had landed.
        `done()` accepted it, Lead got a complete-looking report, and the work
        was closed having never run — the target file still held its original
        code. #276 is the same hole from the other side: a pane wedged in its
        provider's boot phase never receives its task, so any `done` it emits
        belongs to something else.

        The test is narrow on purpose — an assignment IS on record for this
        pane, and none of the ways a task can actually arrive happened:

          * `spawn_initial_task_state` — preloaded into the spawn itself;
          * `_last_delivery_ids` — a delivery was written to its PTY;
          * `last_send_ts` — Lead messaged it directly with `takkub send`;
          * `pane.state == "working"` — the orchestrator itself believes it
            is mid-task (deliberately last, and deliberately inclusive: a
            state this gate did not model must never cost a real report).

        A pane with NO assignment on record is explicitly allowed through:
        `takkub spawn` followed by instructions typed into the pane by hand is
        a legitimate flow the cockpit has no bookkeeping for, and refusing its
        report would break a working habit to defend against a case that
        cannot occur there anyway (with no assignment there is no task for a
        premature `done` to close out).

        Every signal is cockpit-owned bookkeeping, so it reads the same for a
        codex/gemini/opencode pane as for claude (#103) — nothing is parsed
        out of a specific CLI's terminal output.
        """
        ps = getattr(self, "_pane_state", {}).get(f"{project_ns}::{role}")
        if ps is None:
            return False
        if not ps.assign_ts and not ps.last_assigned_task:
            return False  # no assignment on record — hand-driven pane
        if ps.spawn_initial_task_state in ("delivered", "fallback"):
            return False
        if getattr(self, "_last_delivery_ids", {}).get((project_ns, role)):
            return False
        if ps.last_send_ts:
            return False
        return getattr(pane, "state", "") != "working"

    def done(
        self,
        from_role: str,
        note: str = "",
        project: str | None = None,
        failed: bool = False,
        blocked: bool = False,
        force: bool = False,
        git_facts: dict | None = None,
    ) -> tuple[bool, str]:
        """`blocked=True` (#296) means the work could not RUN — something
        outside the codebase is missing. It still counts as not-done (callers
        pass `failed=True` alongside it), but the Lead-facing handoff asks the
        owner for the missing thing instead of proposing a fix loop."""
        try:
            from_role = validate_name(from_role, "role")
        except ValueError as exc:
            return False, str(exc)
        if from_role == LEAD.name:
            return False, "lead cannot call done on itself"
        project_ns = self._resolve_project(project)
        project_panes = self._project_panes(project_ns)
        pane = project_panes.get(from_role)
        if pane is None:
            return False, f"unknown role: {from_role}"

        # #433: a frontend/mobile done on a UI-shaped task must carry real
        # screenshot evidence — see `orchestrator_text.ui_evidence_gate`.
        # Skipped for FAILED/blocked reports (nothing to show) and `--force`.
        if not force and not failed and not blocked:
            _ps_done = self._pane_state.get(f"{project_ns}::{from_role}")
            _gate_msg = ui_evidence_gate(
                from_role,
                note,
                getattr(_ps_done, "last_assigned_task", None),
                getattr(pane, "_session_cwd", None),
            )
            if _gate_msg:
                _log_event("done_rejected_ui_evidence", role=from_role, project=project_ns)
                return False, _gate_msg

        # #278/#276: refuse a report about an assignment that never reached
        # this pane — see `_pane_reports_undelivered_task`. `--force` stays
        # available for the case where the orchestrator's record is simply
        # behind reality (work driven by hand in the pane's own terminal).
        if not force and self._pane_reports_undelivered_task(project_ns, from_role, pane):
            _log_event("done_rejected_no_task", role=from_role, project=project_ns)
            return False, (
                f"ปฏิเสธ done: pane '{from_role}' มี task ที่ถูก assign ไว้ แต่ task นั้น "
                "**ยังไม่เคยถูกส่งถึง pane นี้เลย** (ไม่มี delivery / ไม่มี takkub send / "
                "cockpit ไม่ได้ mark ว่า working) — แปลว่ายังไม่ได้เริ่มงานที่ถูกสั่ง. "
                "`takkub done` ใช้รายงาน **task ที่ได้รับมอบหมาย** เท่านั้น ไม่ใช่เมื่อตอบคำถามจบ "
                "หรือเมื่อจบ turn แรก. ถ้ากำลังรอ task อยู่ ให้รอของจริงเข้ามาก่อน; "
                "ถ้าทำงานนั้นเสร็จจริงด้วยมือ ให้ใช้ `takkub done --force` (issue #278)"
            )

        key = f"{project_ns}::{from_role}"

        # #228: capture this pane instance's own auth token *before* any
        # teardown below. close() (scheduled 2.5s later) is what revokes it,
        # so it is still the live, current token for (project_ns, from_role)
        # right now — the exact spawn-instance identity of the pane that
        # called done(). Threaded through to the Lead-facing notice so a
        # later respawn under the same role name can be told apart from the
        # pane that actually generated this report, however long it sits
        # queued before Lead reads it.
        origin_pane_token = self._current_pane_identity(project_ns, from_role)

        resource_token = getattr(self, "_resource_tokens", {}).pop((project_ns, from_role), None)
        resource_governor = getattr(self, "_resource_governor", None)
        if resource_governor is not None:
            resource_governor.release_slot(resource_token)
            resource_governor.cancel_waiting(project_id=project_ns, pane_id=from_role)

        delivery_id = getattr(self, "_last_delivery_ids", {}).pop((project_ns, from_role), None)
        delivery_manager = getattr(self, "_delivery_manager", None)
        if delivery_id and delivery_manager is not None:
            if failed:
                # The teammate ran the task and reported it failed — the
                # DELIVERY succeeded (#331). Same terminal state, entirely
                # different meaning, hence the reason.
                delivery_manager.mark_failed(delivery_id, "agent_reported_failed")
            else:
                delivery_manager.mark_done(delivery_id)

        # Read state before teardown so fields are available after the pop.
        _ps_done = getattr(self, "_pane_state", {}).get(key) or PaneState()
        had_requires_commit = _ps_done.requires_commit_on_done
        had_auto_chain = _ps_done.auto_chain
        had_shard_total = _ps_done.shard_total
        had_pipeline_run_id = _ps_done.pipeline_run_id
        had_plan_fanout = _ps_done.plan_fanout
        had_worktree = _ps_done.worktree
        if not had_worktree and isinstance(git_facts, dict):
            # #410: PaneState.worktree bookkeeping is gone (most commonly a
            # cockpit restart between assign(isolation="worktree") and this
            # done() — the snapshot restore below now carries it through
            # going forward, but an OLDER snapshot, or any other bookkeeping
            # loss, still lands here) — `collect_done_git_facts` (run off the
            # Qt thread by cli_server, #408) already tried to reconstruct it
            # from git state via `WorktreeManager.rediscover_worktree`; use
            # that instead of silently treating a real worktree pane as a
            # bare shared-tree one with no baseline.
            _rediscovered_worktree = git_facts.get("rediscovered_worktree")
            if _rediscovered_worktree:
                had_worktree = _rediscovered_worktree
                _log_event(
                    "worktree_rediscovered",
                    role=from_role,
                    project=project_ns,
                    branch=_rediscovered_worktree.get("branch"),
                )
        had_assign_ts = _ps_done.assign_ts
        had_task_file = _ps_done.last_assigned_task_file
        had_assign_base_sha = _ps_done.assign_base_sha
        had_assign_git_root = _ps_done.assign_git_root
        had_assign_dirty_snapshot = _ps_done.assign_dirty_snapshot
        if not hasattr(self, "_last_done_task_ids"):
            self._last_done_task_ids = {}
        had_task_id = _ps_done.task_id or self._last_done_task_ids.get(key) or f"pane-{id(pane)}"
        self._last_done_task_ids[key] = had_task_id
        # #244: the issue/task ref shown to Lead must come from the ORIGINAL
        # assign spec Lead itself sent (last_assigned_task), never from the
        # agent's own done() note — an agent has mistyped the issue number
        # it was reporting on before (real incident: wrote "#234" while
        # actually fixing #229). Captured before the PaneState pop below.
        from .notice_facts import extract_issue_ref

        issue_ref = extract_issue_ref(_ps_done.last_assigned_task)

        # Opt-in commit handoff: if assign() was called with requires_commit=True,
        # check for a dirty working tree and warn Lead (the agent isn't blocked —
        # Lead reviews + commits). M2: the check runs ASYNC via QProcess so a slow
        # or large repo can't freeze the Qt main thread for up to the git timeout.
        # The main done notice below goes out immediately; if the tree turns out
        # dirty, a follow-up `[requires-commit]` warning is delivered to Lead.
        if had_requires_commit:
            spawn_cwd = getattr(pane, "_session_cwd", None) or str(DATA_HOME)
            self._check_uncommitted_async(project_ns, from_role, spawn_cwd)

        # Agent finished cleanly — pop all per-pane state atomically.
        # close() (scheduled 2.5 s below) will also pop; second pop is a no-op.
        self._idle_state.pop(key, None)
        getattr(self, "_pane_state", {}).pop(key, None)

        # Screenshot evidence auto-attach (issue #5): scan the pane's artifacts
        # dir for images newer than its assign_ts and fold the result into
        # `note` so every downstream consumer (the done/FAIL notice, the shard
        # aggregate, the decision note) carries it — `done --fail` gets
        # evidence exactly the same way a clean done does.
        raw_note = note
        evidence_line = self._scan_done_evidence(project_ns, from_role, had_assign_ts, raw_note)
        if evidence_line:
            note = f"{note}\n{evidence_line}" if note else evidence_line

        # Symmetrize the return path with the file-based task handoff (#1):
        # persist the session note to disk BEFORE the Lead-facing notice is
        # built/sent, so a long note's notice can point at the file instead of
        # carrying the full text inline. `now` is shared with the
        # `_recent_done` stamp further below so the two never disagree.
        now = datetime.now()
        # Task Ledger (A7): flip the role's open row to ok/fail. Best-effort —
        # never blocks the done() report itself.
        try:
            from .task_ledger import mark_done

            ledger_warning = mark_done(project_ns, from_role, "fail" if failed else "ok", ts=now)
            if ledger_warning:
                self._notify_lead(
                    project_ns, ledger_warning, from_role=from_role, note="", kind="ledger-warning"
                )
            self.ledgerChanged.emit(project_ns)
        except Exception:
            _log_event("ledger_hook_error", role=from_role, project=project_ns, stage="done")
        transcript_path = getattr(pane, "_transcript_path", None)
        session_md_path = self._save_decision_note(
            project_ns, from_role, note, now=now, transcript_path=transcript_path, failed=failed
        )

        # Core V2 Conversation hook (#309 Phase 6) — flag OFF (default) short-
        # circuits before any import, so done() stays byte-identical; flag ON
        # runs in a background thread (I/O must never land on the Qt main
        # thread) and is fail-open around both the flag check and the thread
        # body itself (core.conversation.facade.on_pane_done already wraps
        # its own body in try/except, this is belt-and-suspenders).
        try:
            from .core.conversation.flag import v2_conversation_enabled

            if v2_conversation_enabled():
                import threading

                from .core.conversation.facade import on_pane_done as _cv_on_pane_done
                from .provider_config import effective_provider_for as _cv_provider_for

                threading.Thread(
                    target=_cv_on_pane_done,
                    args=(project_ns, from_role),
                    kwargs={
                        "note": raw_note,
                        "failed": failed,
                        "task_id": had_task_id,
                        "provider_id": _cv_provider_for(from_role, project_ns),
                        "cwd": getattr(pane, "_session_cwd", None),
                        "session_id": _ps_done.session_uuid,
                    },
                    daemon=True,
                ).start()
        except Exception:
            _log_event(
                "conversation_v2_hook_error", role=from_role, project=project_ns, stage="done"
            )

        # notify Lead in the same project (a teammate in project-a mustn't
        # nudge the Lead in project-b by mistake). `done --fail` swaps the plain
        # done notice for a fix-loop proposal prompt (feedback routing MVP) —
        # Lead proposes the fix, never auto-fires, and ALWAYS keeps the full
        # note (Lead's fix-loop propose + classify_failure both read it) — only
        # the clean-done path gets the file-pointer condensation.
        # #244: the ref badge is computed (from the assign spec, above) and
        # prepended only to the Lead-facing `notice` text — never mixed into
        # `notice_body`, which stays the raw/condensed note other consumers
        # (shard aggregate, role_memory failure capture) read unmodified.
        ref_tag = f"[ref {issue_ref}] " if issue_ref else ""
        # #245: the digest bullet's fact table, computed once here (never
        # per-tick — see _compute_digest_facts' own docstring for the
        # #229/#244 boundary this respects). `_worktree_digest_precomputed`
        # lets `_finalize_worktree` below reuse these SAME git reads instead
        # of re-running them a few lines later. Both stay None for a FAILED
        # report — FAILED notices bypass the digest queue entirely
        # (`_is_blocking_lead_notice`), so no bullet is ever rendered from
        # them; `_finalize_worktree` still runs for a failed worktree pane
        # and simply computes its own facts fresh in that (rarer) case.
        digest_facts = None
        _worktree_digest_precomputed = None
        if failed:
            notice_body = note
            elapsed_since_assign = (time.time() - had_assign_ts) if had_assign_ts else float("inf")
            if is_delivery_pointer_failure(note, had_task_file, elapsed_since_assign):
                # #273: a pane that couldn't open the file-pointer handoff
                # never started the assigned WORK at all — this is a
                # delivery failure, not a task failure. Deliberately skip
                # the fix-loop-propose wording (there is no root cause in
                # the work to find yet) and the role_memory capture below
                # (it would poison this role's future-spawn context with a
                # failure that was never about anything it did).
                notice = self._build_delivery_pointer_failure_notice(
                    from_role, f"{ref_tag}{note}", had_task_file
                )
                _log_event(
                    "delivery_pointer_failure",
                    project=project_ns,
                    role=from_role,
                    note=(note or "")[:200],
                    task_file=had_task_file,
                )
            elif blocked:
                # Reported BLOCKED outright (#296) — no signature guessing
                # needed, the pane said so. Still recorded as a failure for
                # the ledger (the task is not done), but routed to a human.
                from .routing_planner import classify_blocked

                notice = self._build_blocked_handoff(
                    from_role, f"{ref_tag}{note}".strip(), classify_blocked(note)[1]
                )
                _log_event(
                    "verify_blocked",
                    project=project_ns,
                    role=from_role,
                    note=(note or "")[:200],
                    declared=True,
                )
            else:
                notice = self._build_verify_fail_handoff(from_role, f"{ref_tag}{note}")
                _log_event(
                    "verify_failed", project=project_ns, role=from_role, note=(note or "")[:200]
                )
                # ReflexionMemory-style auto-capture: a FAILED report used to go to
                # Lead and nowhere else, so the same role could repeat the same
                # failure cold next spawn. No agent decision required here.
                try:
                    from .role_memory import append_failure_entry

                    fail_role, _ = _split_shard(from_role)
                    fail_reason = raw_note.strip().splitlines()[0] if raw_note.strip() else ""
                    append_failure_entry(project_ns, fail_role, fail_reason)
                except Exception:
                    pass
        else:
            notice_body = self._condense_done_note(raw_note, note, evidence_line, session_md_path)
            notice = f"[{from_role} done] {ref_tag}{notice_body}".rstrip()
            headline = _truncate_at_word_boundary(
                raw_note.strip().splitlines()[0] if raw_note.strip() else "", 200
            )
            pane_cwd = getattr(pane, "_session_cwd", None)
            try:
                digest_facts, _worktree_digest_precomputed = self._compute_digest_facts(
                    from_role,
                    issue_ref,
                    headline,
                    session_md_path,
                    had_worktree,
                    pane_cwd,
                    had_assign_base_sha,
                    had_assign_git_root,
                    had_assign_dirty_snapshot,
                    git_facts=git_facts,
                )
            except Exception as exc:  # digest cosmetics must never break done()
                _log_event(
                    "digest_facts_error", role=from_role, project=project_ns, error=str(exc)[:200]
                )
                from .digest_facts import DigestFacts

                digest_facts = DigestFacts(
                    role=from_role,
                    ref=issue_ref,
                    report_path=session_md_path,
                    headline=headline,
                    files_note="ตรวจไม่ได้ (เกิด error ระหว่างคำนวณ)",
                )
                _worktree_digest_precomputed = None

        # Core V2 Second Brain Reflection hook (#309 Phase 7c) — flag OFF
        # (default, `TAKKUB_V2_BRAIN`) short-circuits before any import, so
        # done() stays byte-identical; flag ON runs in a background thread
        # (fail-open around both the flag check and the thread body itself —
        # `core.brain.facade.on_pane_done` already wraps its own body in
        # try/except, belt-and-suspenders). Placed after `digest_facts` is
        # finalized (both the happy path and its except-fallback above) so
        # the cockpit-measured facts are available to fold in alongside the
        # agent's own note.
        try:
            from .core.brain.flag import v2_brain_enabled

            if v2_brain_enabled():
                import threading

                from .core.brain.facade import on_pane_done as _brain_on_pane_done

                threading.Thread(
                    target=_brain_on_pane_done,
                    args=(project_ns, from_role),
                    kwargs={
                        "note": raw_note,
                        "digest_facts": digest_facts,
                        "failed": failed,
                        "task_id": had_task_id,
                    },
                    daemon=True,
                ).start()
        except Exception:
            _log_event("brain_v2_hook_error", role=from_role, project=project_ns, stage="done")

        # #280: fold everything the watchdogs observed about this pane into
        # its own report instead of having interrupted Lead with each
        # observation as it happened (slow boot, unconfirmed paste, degrade +
        # respawn …). Drained unconditionally — even when the notice below is
        # suppressed for a shard — so one pane lifecycle's health can never
        # leak into the next pane that takes over this role slot.
        health_line = self._drain_pane_health(project_ns, from_role)
        if health_line:
            notice = f"{notice}\n{health_line}"

        # Shard panes suppress clean per-shard notices in favour of the
        # consolidated handoff. Failures still surface immediately so the
        # fix-loop proposal cannot be delayed or lost.
        # Planner panes: suppress too — the "[qa plan ready] fan-out …" message
        # from _fire_qa_plan_fanout is the meaningful one Lead acts on.
        # Non-shard, non-planner panes use the normal notice path.
        if failed or (had_shard_total == 0 and not had_plan_fanout):
            # Route through _notify_lead so concurrent done notices are serialised
            # and never injected while Lead is mid-generation (the root cause of the
            # "Lead goes silent after parallel dispatch" bug).
            completion_generation = int(getattr(pane, "_session_generation", 0))
            notice_id = make_notice_id(
                project_ns,
                from_role,
                had_task_id,
                completion_generation,
            )
            deduper = getattr(self, "_notice_deduper", None)
            if deduper is None:
                deduper = self._notice_deduper = NoticeDeduper(RUNTIME_DIR / "notice-dedupe.json")
            if deduper.mark_once(notice_id):
                self._notify_lead(
                    project_ns,
                    notice,
                    from_role=from_role,
                    note=note,
                    pane_token=origin_pane_token,
                    digest_facts=digest_facts,
                    kind="failed" if failed else "done",
                )
            else:
                _log_event(
                    "done_notice_deduped",
                    project=project_ns,
                    role=from_role,
                    notice_id=notice_id,
                )

        # Fix A: when this done event belongs to a background tab, emit a
        # cross-tab signal so main_window can flash the status bar even if
        # the user is currently looking at a different project's tab.
        try:
            active_ns, _ = active_project()
        except Exception:
            active_ns = None
        if active_ns and project_ns != active_ns:
            self.crossTabDone.emit(project_ns, from_role, note)

        # Worktree isolation (issue #81): the pane ran in its own git worktree.
        # If its branch has commits, send Lead a MERGE PROPOSAL (never auto);
        # otherwise safe-remove the empty worktree.
        if had_worktree:
            self._finalize_worktree(
                project_ns, from_role, had_worktree, precomputed=_worktree_digest_precomputed
            )
        else:
            # graft code-graph refresh (debounced): the pane wrote directly
            # into the project's tracked cwd (not a worktree — those are a
            # throwaway checkout under DATA_HOME/worktrees/ that this must
            # never touch), so the graph there just went stale. Best-effort,
            # never blocks the done() report.
            try:
                from .graft_autobuild import schedule_rebuild_after_done

                schedule_rebuild_after_done(getattr(pane, "_session_cwd", None))
            except Exception:
                pass

        # Auto-chain handoff: if this pane was tagged --auto-chain at
        # assign time, and it was the LAST pending auto-chain pane in
        # the project, inject a pre-authorisation prompt so Lead fires
        # verify (qa+reviewer) without proposing/confirming.
        self._maybe_fire_auto_chain_handoff(project_ns, had_auto_chain)

        # Plan-then-fan-out: this pane was a planner (--plan). Read the bucket
        # plan it just wrote and spawn the QA shards (each with its bucket).
        if had_plan_fanout and not failed:
            base_role_p, _ = _split_shard(from_role)
            self._fire_qa_plan_fanout(project_ns, base_role_p, had_plan_fanout, planner_note=note)

        # Shard aggregate: record this shard's (possibly condensed) note and
        # check if all N done. Reusing `notice_body` here means the
        # consolidated handoff (_inject_shard_fanout_handoff) gets the same
        # threshold/pointer treatment as a non-shard done notice (#1
        # symmetrization, item 4) instead of stitching N full notes together.
        if had_shard_total > 0:
            base_role_d, _ = _split_shard(from_role)
            group_key = f"{project_ns}::{base_role_d}"
            group = self._shard_groups.get(group_key)
            if group and not group.closed:
                if failed:
                    group.failed.add(from_role)
                    group.failed_notes[from_role] = notice_body
                else:
                    group.done[from_role] = notice_body
                if len(group.done) + len(group.failed) >= group.total:
                    group.closed = True
                    self._inject_shard_fanout_handoff(project_ns, group)
                    self._shard_groups.pop(group_key, None)
            else:
                # #3: group already closed (timeout) or popped — shard arrived
                # late.  Send a notice so Lead knows instead of silently dropping.
                late_msg = (
                    f"⚠️ [shard late-complete] {from_role} reported done after its "
                    f"shard group already closed (timeout or all-failed). "
                    f"note: {note!r:.120}"
                )
                self._notify_lead(
                    project_ns,
                    late_msg,
                    from_role=from_role,
                    note="late-complete",
                    kind="shard-late-complete",
                )
                _log_event(
                    "shard_late_complete",
                    project=project_ns,
                    role=from_role,
                    note=note[:200],
                )

        # Pipeline hop advance: if this pane was part of a pipeline run, remove
        # it from the hop's pending set and fire the next hop when all done.
        if had_pipeline_run_id:
            pipeline_key = f"{project_ns}::{had_pipeline_run_id}"
            pl_run = self._pipeline_runs.get(pipeline_key)
            if pl_run and not pl_run.closed:
                pl_run.hop_pending.discard(from_role)
                if not pl_run.hop_pending:
                    self._advance_pipeline(project_ns, pipeline_key, pl_run)

        # mark pane done, auto-close after a delay so user can see it.
        # Capture current session so the delayed close is a no-op if the pane
        # has already been respawned with a new session by the time the timer fires.
        pane.set_state("done", note=note[:80] if note else "done")
        _done_sess = pane.session

        def _close_if_same_session() -> None:
            _pp = self._project_panes(project_ns).get(from_role)
            if _pp is not None and _pp.session is _done_sess and _pp.state == "done":
                self.close(from_role, project=project_ns)

        QTimer.singleShot(2_500, _close_if_same_session)
        _log_event(
            "done",
            role=from_role,
            note=note[:200],
            project=project_ns,
            session_uuid=self._session_uuid_for(_exit_key(project_ns, from_role)),
        )
        # `now`/`transcript_path`/the actual _save_decision_note write already
        # happened above, ahead of the notice — see the comment there. Reuse
        # the same `now` so this stamp can't disagree with the written file.
        stamp = now.strftime("%Y-%m-%dT%H%M%S")
        self._recent_done.insert(0, (project_ns, from_role, f"{stamp}-{from_role}.md"))
        del self._recent_done[20:]
        # #242: record this resolution for any `takkub wait` registration
        # currently watching (project_ns, from_role) — see _wait_done_events
        # init comment. Additive-only; never read by anything except
        # LeadWaitMixin.poll_wait.
        getattr(self, "_wait_done_events", {})[(project_ns, from_role)] = {
            "ts": now.timestamp(),
            "failed": bool(failed),
        }
        # Refresh hot.md immediately so Obsidian shows the done event
        # without waiting up to a minute for the periodic tick.
        self._write_hot_md()
        self.agentDone.emit(project_ns, from_role, note)
        return True, f"{from_role} reported done"

    def progress(
        self, from_role: str, note: str = "", project: str | None = None
    ) -> tuple[bool, str]:
        """Report a status update to Lead without ending the task.

        #234: `done()` is the *only* thing that unconditionally schedules a
        pane's teardown 2.5s later (see the auto-close timer above) — a
        devops pane mid-`docker compose build --no-cache` that called
        `done()` just to say "still building, will report again when
        finished" got that build's subprocess tree killed by the very next
        auto-close tick, with the note's own text making clear the task was
        *not* actually finished. `progress()` is the same one-line "tell
        Lead what's happening" primitive as `done()`'s notice, minus every
        teardown side effect: no auto-close timer, no `_pane_state`/
        `_idle_state` pop, no resource-slot release, no worktree
        merge-proposal, no task-ledger flip. The pane keeps running exactly
        as before the call.
        """
        try:
            from_role = validate_name(from_role, "role")
        except ValueError as exc:
            return False, str(exc)
        if from_role == LEAD.name:
            return False, "lead cannot call progress on itself"
        project_ns = self._resolve_project(project)
        project_panes = self._project_panes(project_ns)
        pane = project_panes.get(from_role)
        if pane is None:
            return False, f"unknown role: {from_role}"
        note = note.strip()
        if not note:
            return False, "progress requires a non-empty message"

        # Counts as evidence of life for the same reason a peer send() does
        # (spawn_engine._ps) — a long build/test run that only ever talks to
        # Lead via progress() must not look idle to the stall watchdog.
        _ps_self = self._ps(f"{project_ns}::{from_role}")
        _ps_self.last_send_ts = time.time()
        # #461: reuse the exact suppression `takkub send --to lead` already
        # gets (consume_pane_hook's blocked_on_lead_ts check, same 30-min
        # window as the idle-watchdog's forgot-done reminder) — a pane that
        # just called progress() (e.g. "waiting on credentials") is telling
        # Lead the same thing a direct send would, and must not have the
        # Stop-hook done-gate force it into `takkub done` on the very next
        # turn end. Each progress() call refreshes the window; a pane that
        # goes silent past it still gets nudged, same as today.
        _ps_self.blocked_on_lead_ts = time.time()

        # #463: a `progress()` call is unambiguous proof the task text
        # already reached this pane and it engaged with it — strictly
        # stronger evidence than the ready-marker scrape `_on_settled`
        # (lead_inbox.py) uses to resolve ACCEPTED/UNCERTAIN. Advance any
        # still-in-flight delivery for this role straight to RUNNING so it
        # drops out of `_UNCONFIRMED_STATES`/self-heal-resend eligibility AND
        # (task_delivery._RESEND_ELIGIBLE_STATES, #463 follow-up)
        # `supersede_for_session`'s cancel-worthy set — a later `send()` into
        # this pane then has nothing left to touch at all: no ambiguous
        # confirmation to cancel, and no live-but-cancellable delivery either,
        # so the delivery stays RUNNING for this role's own later `done()` to
        # `mark_done()` (no more confusing "delivery-superseded" notice for a
        # task Lead already knows landed, and no delivery silently flipped to
        # CANCELLED out from under a task that is actually still running,
        # #255/#392). Best-effort: a missing or already-terminal delivery is
        # a no-op, never raises.
        try:
            _delivery_id = getattr(self, "_last_delivery_ids", {}).get((project_ns, from_role))
            _delivery_mgr = getattr(self, "_delivery_manager", None)
            if _delivery_id and _delivery_mgr is not None:
                _delivery_mgr.mark_running(_delivery_id)
        except Exception:
            pass

        origin_pane_token = self._current_pane_identity(project_ns, from_role)
        body = f"[{from_role} progress] {note}"
        self._notify_lead(
            project_ns,
            body,
            from_role=from_role,
            note="progress",
            pane_token=origin_pane_token,
            kind="progress",
        )
        _log_event("progress", role=from_role, project=project_ns, note=note[:200])
        return True, f"{from_role} progress reported"

    def consume_pane_hook(
        self,
        from_role: str,
        project: str | None = None,
        event: str = "",
        notification_type: str = "",
    ) -> tuple[bool, bool, str]:
        """Consume a Claude Code hook signal (Stop / Notification) from a
        claude-backed pane's `takkub _hook`, as an authoritative turn-end/idle
        marker, and decide the Stop-hook done-gate.

        Returns ``(ok, block, reason)``. ``block=True`` tells the caller to
        emit a Stop-hook block decision nudging the pane to run `takkub done`.

        Idle-state signal: reuses the exact idempotent pattern the PTY-scraping
        watchdog (`_check_idle_teammates`) already uses — `first_idle_ts` is
        only set the first time it's seen `None`, so a hook firing milliseconds
        before/after the next poll tick is a no-op, not a double-count. PTY
        scraping stays the fallback/ground truth (non-claude panes, or a claude
        pane whose hook never fires) and self-corrects any staleness on its own
        next tick (`is_at_ready_prompt()` returning False resets first_idle_ts).

        Done-gate: one-shot per assignment via `PaneState.stop_gate_notified` —
        `stop_hook_active` alone only guards against Claude Code recursively
        re-entering the SAME Stop event, not a fresh Stop event a few seconds
        later if the model ignores the nudge and stops again (would otherwise
        block forever). Also honours the same suppressions the idle watchdog
        does: not blocked-on-lead, not rate-limited, not TTY-prompt-blocked, and
        only while the pane is live and `working` with an outstanding assigned
        task (see docs/reviews/2026-07-02-claude-hooks-design-crosscheck.md).
        """
        try:
            from_role = validate_name(from_role, "role")
        except ValueError:
            return False, False, "invalid role"
        project_ns = self._resolve_project(project)
        key = f"{project_ns}::{from_role}"

        if from_role != LEAD.name and event in ("Stop", "Notification"):
            entry = self._idle_state.setdefault(
                key, {"first_idle_ts": None, "last_reminder_ts": 0.0, "seen_working": False}
            )
            if entry["first_idle_ts"] is None:
                entry["first_idle_ts"] = time.time()

        # Lead never gets the done-gate (it never calls `done` on itself);
        # the gate only applies to a turn actually ending (Stop).
        if from_role == LEAD.name or event != "Stop":
            return True, False, ""

        pane = self._project_panes(project_ns).get(from_role)
        ps = getattr(self, "_pane_state", {}).get(key)

        def _pass() -> tuple[bool, bool, str]:
            # #463 follow-up: every non-blocking return below means the Stop
            # hook genuinely ended this turn (Claude Code isn't being nudged
            # to keep going) — stamp it so `_derive_display_state`'s
            # waiting-lead tier can tell a `progress()` call mid-turn (the
            # pane keeps working right after) apart from one after the turn
            # actually ended. See `blocked_on_lead_ts`/`waiting_for_lead` in
            # this same method's caller.
            if ps is not None:
                ps.last_turn_end_ts = time.time()
            return True, False, ""

        if pane is None or pane.session is None or not pane.session.is_alive:
            return _pass()
        if pane.state != "working":
            return _pass()

        if ps is None or not ps.last_assigned_task or ps.stop_gate_notified:
            return _pass()

        now = time.time()
        # Same 30-minute window _check_idle_teammates uses to suppress the
        # forgot-done reminder while genuinely waiting on Lead's reply.
        # #461: progress() stamps this same field, so a pane that just
        # reported status (e.g. waiting on credentials) gets the same grace
        # here as one that used `takkub send --to lead` directly.
        if ps.blocked_on_lead_ts is not None and (now - ps.blocked_on_lead_ts) < 30 * 60:
            return _pass()
        if ps.rate_limited_until > now:
            return _pass()
        if ps.tty_blocked_since is not None:
            return _pass()

        ps.stop_gate_notified = True
        return (
            True,
            True,
            'ยังไม่จบ → `takkub progress "<สถานะ>"` แล้วจบ turn รอ takkub send จาก Lead ได้ '
            "· จบแล้ว → รายงานผลด้วย takkub done ก่อนจบ",
        )

    def consume_session_report(
        self,
        from_role: str,
        project: str | None = None,
        session_id: str = "",
        source: str = "",
        cwd: str = "",
    ) -> tuple[bool, str]:
        """Consume a Claude Code `SessionStart` hook signal from a claude-backed
        pane's `takkub session-report`, keeping `PaneState.session_uuid` truthful
        across manual `/resume` / `/clear` / post-compact session switches.

        Root cause this fixes: `session_uuid` used to be stamped ONLY at spawn
        time (spawn_engine.py's `--session-id`/`--resume` decision). If the
        user later runs `/resume` inside the pane, claude silently switches to
        writing a DIFFERENT transcript uuid that the orchestrator never learns
        about, so the remote/mobile mirror's exact-uuid lookup misses and shows
        a blank chat (see `remote/notify.py`). `SessionStart` fires on every
        session start (startup/resume/clear/compact) carrying the real, current
        session_id — reporting it here keeps `pane_state.session_uuid` correct
        without ever guessing (no newest-file heuristic — deliberately removed;
        do not re-add).

        Fails open: malformed input (invalid role, empty session_id) is
        reported back but never raises — a hook must never break the pane's
        session start.

        (Historical: this used to also (re-)fire the `/remote-control`
        auto-bridge for the Lead role. That bridge was removed 2026-07-10 — it
        kept racing claude's `/resume` picker and cancelling it. This method now
        only keeps `pane_state.session_uuid` in sync for the mobile mirror.)
        """
        try:
            from_role = validate_name(from_role, "role")
        except ValueError:
            return False, "invalid role"
        if not session_id:
            return False, "missing session_id"
        project_ns = self._resolve_project(project)
        key = f"{project_ns}::{from_role}"
        ps = self._ps(key)
        ps.session_uuid = session_id
        # #422 item 3: PaneState is popped by done()/close() BEFORE their
        # events are logged, so keep the last reported uuid per slot here —
        # overwritten by the next report, never popped — for correlation.
        if not hasattr(self, "_last_session_uuid"):
            self._last_session_uuid: dict[str, str] = {}
        self._last_session_uuid[key] = session_id
        if cwd:
            ps.session_uuid_cwd = cwd
        # Also push the rollover onto the *live* pane (issue #129): the token
        # meter reads pane.model.session_uuid every 5 s tick, a separate copy
        # from PaneState.session_uuid stamped at attach time. Without this, a
        # manual /resume or /clear inside the pane would update PaneState but
        # leave the meter polling the pre-rollover uuid's (now-stale) file.
        pane = self._panes_by_project.get(project_ns, {}).get(from_role)
        if pane is not None:
            pane.model.set_session_uuid(session_id)
        _log_event(
            "session_report",
            project=project_ns,
            role=from_role,
            source=source,
            uuid=session_id[:8],
        )
        # (The /remote-control auto-bridge was removed 2026-07-10 at user
        # request — it kept racing claude's /resume picker. `session_uuid` is
        # still stamped above so the mobile mirror tracks the live session;
        # /remote-control is now typed by hand when the user wants it.)
        return True, ""

    @staticmethod
    def _save_decision_note(
        project: str,
        role: str,
        note: str,
        now: datetime | None = None,
        transcript_path: str | None = None,
        failed: bool = False,
    ) -> str | None:
        """Persist a teammate's `takkub done` note as a small markdown
        file under `runtime/sessions/<YYYY-MM-DD>/<project>/<role>-<HHMMSS>.md`,
        then mirror the same file into the Obsidian vault (if one is
        configured) at
        `<vault>/01-Projects/<project>/sessions/<YYYY-MM-DD>T<HHMMSS>-<role>.md`
        so the user can browse the decision trail from Obsidian's
        Dataview / graph view alongside the project's wiki page.

        events.log already captures the same data but is one long
        machine-readable stream. The per-role markdown gives the user a
        human-friendly paper trail that survives cockpit restarts and
        is trivial to grep / link to from a wiki later. Best-effort:
        any IO error is swallowed so a disk hiccup never breaks the
        done flow.

        `now` is injected by `done()` so the caller and this writer
        agree on the timestamp — otherwise the hot.md "Recent" entry
        and the on-disk filename could disagree by a second under load.

        Returns the forward-slashed absolute path of the runtime session
        file actually written, or ``None`` if nothing was written (empty/junk
        note, junk project, dedup, invalid project name, or a write failure)
        — the caller (``done()``) uses this to decide whether a long-note
        pointer notice has somewhere to point at (issue #symmetrize-return).
        """
        if not (note or "").strip():
            return None
        # Junk filter: skip 1-word "ok" / "wip" / "done" stubs and
        # scratch/test workspaces. Keeps the Obsidian vault from
        # filling up with content-less session files that don't
        # connect to anything (no useful note body to backlink from).
        if _is_junk_note(note):
            return None
        if _is_junk_project(project):
            return None
        if _is_dedup_note(project, role, note):
            return None
        if now is None:
            now = datetime.now()
        body = _render_decision_note(
            project, role, note, now, transcript_path=transcript_path, failed=failed
        )
        try:
            safe_project = validate_name(project, "project")
        except ValueError:
            import logging

            logging.getLogger(__name__).warning(
                "_save_decision_note: rejected unsafe project name %r", project
            )
            return None
        session_md_path: str | None = None
        try:
            day = RUNTIME_DIR / "sessions" / now.strftime("%Y-%m-%d") / safe_project
            day.mkdir(parents=True, exist_ok=True)
            path = day / f"{role}-{now.strftime('%H%M%S')}.md"
            path.write_text(body, encoding="utf-8")
            session_md_path = str(path).replace(os.sep, "/")
        except OSError:
            pass

        # Local knowledge-base distillation (#168): unlike the vault mirror
        # below, this always runs — no Obsidian vault required — so every
        # cockpit install gets cross-session knowledge capture regardless of
        # provider or whether a vault is configured. Best-effort, never
        # blocks the done() report it's called alongside.
        try:
            distill_to_knowledge_base(safe_project, role, note, RUNTIME_DIR, now=now)
        except Exception:
            pass

        vault = _resolve_vault_dir()
        if vault is not None:
            try:
                sessions = vault / "99-Logs" / "sessions" / safe_project
                sessions.mkdir(parents=True, exist_ok=True)
                stamp = now.strftime("%Y-%m-%dT%H%M%S")
                (sessions / f"{stamp}-{role}.md").write_text(body, encoding="utf-8")
            except OSError:
                pass

            # Phase B: distill durable facts into 01-Projects/<project>.md (best-effort)
            distill_session_facts(project, role, note, vault, now=now)

        return session_md_path

    def end_session(self, project: str | None = None, note: str = "") -> tuple[bool, str]:
        """Write a Lead session summary to runtime/sessions and the vault mirror.

        Called via `takkub end-session [--note '...']` from the Lead pane.
        Never closes any pane — Lead stays open, teammates continue as-is.
        """
        project_ns = self._resolve_project(project)
        try:
            project_ns = validate_name(project_ns, "project")
        except ValueError:
            import logging

            logging.getLogger(__name__).warning(
                "end_session: rejected unsafe project name %r", project_ns
            )
            return False, f"unsafe project name rejected: {project_ns!r}"
        if not note.strip():
            note = "session ended"
        now = datetime.now()
        day_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H%M%S")

        # Gather teammate done-event files from today's session dir.
        session_day = RUNTIME_DIR / "sessions" / day_str / project_ns
        done_files: list[str] = []
        if session_day.is_dir():
            for f in sorted(session_day.iterdir()):
                if f.name.startswith("lead-"):
                    continue
                if f.suffix == ".md":
                    done_files.append(f"runtime/sessions/{day_str}/{project_ns}/{f.name}")

        # Gather still-open teammate panes.
        active_teammates: list[tuple[str, str]] = [
            (name, pane.state)
            for name, pane in self._project_panes(project_ns).items()
            if name != LEAD.name
        ]

        # Build markdown body.
        iso = now.isoformat(timespec="seconds")
        body = (
            f"---\n"
            f"role: lead\n"
            f"project: {project_ns}\n"
            f"date: {iso}\n"
            f"tags: [session, lead, {project_ns}]\n"
            f"---\n\n"
            f"# lead session end · {iso}\n\n"
            f"**Role:** lead\n\n"
            f"## Note\n\n{note.strip()}\n"
        )
        if done_files:
            body += "\n## Teammate done events\n\n"
            body += "\n".join(f"- {p}" for p in done_files) + "\n"
        else:
            body += "\n## Teammate done events\n\n_(none today)_\n"

        if active_teammates:
            body += "\n## Active teammates at end-session\n\n"
            body += "\n".join(f"- {role}: {state}" for role, state in active_teammates) + "\n"
        else:
            body += "\n## Active teammates at end-session\n\n_(none)_\n"

        # Write local file.
        rel_path = f"runtime/sessions/{day_str}/{project_ns}/lead-{time_str}.md"
        try:
            session_day.mkdir(parents=True, exist_ok=True)
            local_path = RUNTIME_DIR / "sessions" / day_str / project_ns / f"lead-{time_str}.md"
            local_path.write_text(body, encoding="utf-8")
        except OSError as exc:
            return False, f"failed to write session file: {exc}"

        # Mirror to vault (best-effort, never fails the call).
        # project_ns is already validated above so safe to use directly.
        vault = _resolve_vault_dir()
        if vault is not None:
            try:
                vault_sessions = vault / "99-Logs" / "sessions" / project_ns
                vault_sessions.mkdir(parents=True, exist_ok=True)
                stamp = now.strftime("%Y-%m-%dT%H%M%S")
                (vault_sessions / f"{stamp}-lead.md").write_text(body, encoding="utf-8")
            except OSError:
                pass

            # Phase B: distill durable facts from the session note (best-effort)
            distill_session_facts(project_ns, "lead", note, vault, now=now)

        # Append today's Finish-Job digest to the vault's 05-Daily note.
        # Best-effort: the local session summary above is the contract, so a
        # digest failure (vault glitch, chatlog scan error) must never fail
        # end_session. write_daily_digest already no-ops when no vault is
        # configured and swallows its own IO errors; the try/except here is a
        # belt-and-braces guard against any unexpected raise.
        try:
            self.write_daily_digest(project_ns)
        except Exception:
            import logging

            logging.getLogger(__name__).debug(
                "end_session: write_daily_digest failed (non-fatal)", exc_info=True
            )

        _log_event("end_session", project=project_ns, note=note[:200])
        return True, f"lead session summary written: {rel_path}"

    # Issue #163: a `done()` report is queued for Lead but the reporting
    # pane's own auto-close timer fires a fixed 2.5s later regardless — the
    # Lead Inbox Digest can hold a clean notice up to its debounce window,
    # the delivery pump can spend further time waiting for Lead to be ready,
    # and a Lead whose composer reads "ready but holding an unsubmitted
    # draft" parks the notice in the durable store indefinitely by design
    # (never force-bypassing a live draft — see _reap_pending_done_notices).
    # None of those delays are bounded by the pane's own 2.5s lifetime, so
    # `takkub list` used to just drop the role the moment its pane closed —
    # looking exactly like the work vanished, even though the report was
    # still genuinely in flight. Confirmed live (2026-08-13): a backend
    # pane's clean done() at 15:10 didn't reach the Lead Inbox Digest until
    # ~15:40, but the pane itself was already gone from `takkub list` well
    # before that.
    _PENDING_NOTICE_STATE = "done (report queued — not yet delivered to Lead)"

    def _has_pending_lead_notice(self, project_ns: str, role_name: str) -> bool:
        """True if `role_name`'s done/FAILED report is still somewhere in the
        outbound-to-Lead pipeline (digest queue, live notify queue, or the
        durable pending store) rather than actually written into Lead's pane.

        Matched by the literal `[<role> done]` / `[<role> FAILED]` tag
        `done()` builds the notice with, plus the `[<role>] done` shape the
        notice takes once folded into a Lead Inbox Digest line (see
        `_format_digest_item`). Used by list_status/list_status_detailed to
        keep surfacing a role until its notice has genuinely left the
        pipeline, instead of trusting pane-registry membership alone (#163).
        """
        tag = re.compile(
            rf"\[{re.escape(role_name)}\s+(?:done|FAILED)\]"
            rf"|^•\s*\[{re.escape(role_name)}\]\s*done",
            re.IGNORECASE | re.MULTILINE,
        )

        def _any_match(bodies) -> bool:
            # #228: digest/live-queue entries are (body, pane_token) tuples;
            # a bare string means no origin was recorded (system notices,
            # legacy call sites, test fixtures) — either shape is searched.
            for b in bodies:
                text = b[0] if isinstance(b, tuple) else b
                if isinstance(text, str) and tag.search(text):
                    return True
            return False

        if _any_match(getattr(self, "_lead_digest_queue", {}).get(project_ns, ())):
            return True
        if _any_match(getattr(self, "_lead_notify_queue", {}).get(project_ns, ())):
            return True
        for item in getattr(self, "_pending_done_notices", {}).get(project_ns, ()):
            if isinstance(item, dict) and item.get("role") == role_name:
                return True
        return False

    def _pending_notice_outside(
        self, project_ns: str, watched_roles: set[str]
    ) -> dict[str, str] | None:
        """(#253) The first pending Lead notice — digest, live, or durable
        queue — whose origin role is outside *watched_roles* and needs
        immediate Lead attention (FAILED / spawn-failed / delivery-
        unconfirmed / spawn-stuck — same triage `_is_blocking_lead_notice`
        already uses to jump the digest queue). `poll_wait` uses this to
        wake a `takkub wait` early instead of leaving Lead deaf to a
        blocking report from a role it isn't watching for up to the full
        --timeout — the #253 incident: `wait --role qa` sat blind for 9
        minutes while devops's done report queued up behind it.

        Plain "done" notices from an unwatched role are deliberately NOT a
        wake trigger — those are routine parallel-fan-out noise a Lead
        doesn't need to react to mid-wait; only reports that need a
        decision jump the wait early. Roles in *watched_roles* are
        excluded so a role's own report never "interrupts" the very wait
        that is watching for it — that path already resolves it normally.

        Returns ``{"role": ..., "detail": ...}`` for the first match
        (first-found, not severity-ranked — a second call after Lead
        handles it and re-waits will surface the next one), else None.

        #259: `inbox_report` tags [spawn-failed]/[delivery-unconfirmed]/
        [spawn-stuck]/[delivery-boot-stall] notices "system" (`_notice_role_tag`
        doesn't parse their shape — see `_system_marker_role`'s comment),
        so before this fix EVERY notice of these 4 kinds was silently
        dropped here regardless of watched/unwatched, not just for a
        watched role as originally reported — `_system_marker_role` below
        recovers the real target role so a genuinely outside role's
        boot-stall/unconfirmed/stuck notice reaches this interrupt too.
        """
        for item in self.inbox_report(project=project_ns):
            role = item.get("role")
            body = str(item.get("body", ""))
            if role == "system":
                role = _system_marker_role(body)
            if not role or role in watched_roles:
                continue
            if not _is_blocking_lead_notice(body):
                continue
            first_line = body.strip().splitlines()[0] if body.strip() else ""
            return {"role": role, "detail": first_line[:200]}
        return None

    def _pending_system_notice_for_watched(
        self, project_ns: str, pending_roles: set[str]
    ) -> dict[str, str] | None:
        """(#259) A delivery-health system notice about a role Lead IS
        watching — [delivery-unconfirmed]/[spawn-stuck]/[delivery-boot-stall]/
        [spawn-failed] — never calls `done()`/`failed()`, so unlike a
        watched role's own done/FAILED report (already resolved every
        poll tick via `_wait_done_events`), this class of notice used to
        sit invisible in the inbox for the role's own wait until the full
        --timeout elapsed: `_pending_notice_outside` deliberately skips
        roles in the watched set (a role's own done/FAILED must resolve
        through the normal path, not double-fire as an "interrupt"
        against itself), and nothing else ever checked the watched set
        for this notice class. Reported live: `wait --role kimi` sat
        blind for the full 30 minutes while a `[delivery-unconfirmed]
        kimi ...` notice had been sitting in the inbox since minute 1.5.

        Deliberately restricted to `_system_marker_role` (the 4 system
        markers only), NOT the broader `_is_blocking_lead_notice` (which
        also matches `[role FAILED]`) — a watched role's own FAILED
        already resolves through `_wait_done_events` the moment `failed()`
        runs; matching it here too would be redundant and races the two
        resolutions against each other for no benefit.

        Only checked against *pending_roles* (this tick's still-pending
        watched roles, passed by the caller) — a role that already
        resolved this tick doesn't need waking.
        """
        for item in self.inbox_report(project=project_ns):
            body = str(item.get("body", ""))
            role = _system_marker_role(body)
            if not role or role not in pending_roles:
                continue
            first_line = body.strip().splitlines()[0] if body.strip() else ""
            return {"role": role, "detail": first_line[:200]}
        return None

    def _pending_notice_roles(self, project_ns: str, known: dict) -> dict[str, str]:
        """Roles from `_recent_done` no longer in `known` whose report is
        still pending delivery — see `_has_pending_lead_notice` (#163)."""
        extra: dict[str, str] = {}
        for done_ns, done_role, _fname in getattr(self, "_recent_done", ()):
            if done_ns != project_ns or done_role in known or done_role in extra:
                continue
            if self._has_pending_lead_notice(project_ns, done_role):
                extra[done_role] = self._PENDING_NOTICE_STATE
        return extra

    def _queued_resource_roles(self, project_ns: str, known: dict) -> dict[str, str]:
        """Roles blocked on a resource-governor slot whose pane hasn't
        spawned yet (#240 point 3): `assign()` answers `ok` and the role has
        no pane entry at all until a slot frees, so it used to vanish from
        `takkub list`/`status` entirely with nothing telling Lead it was
        still queued — only `runtime/events.log` had the truth."""
        governor = getattr(self, "_resource_governor", None)
        if governor is None:
            return {}
        try:
            waiting = governor.snapshot().get("waiting_tasks", [])
        except Exception:
            return {}
        extra: dict[str, str] = {}
        for item in waiting:
            if item.get("project_id") != project_ns:
                continue
            role = str(item.get("pane_id") or "")
            if not role or role in known or role in extra:
                continue
            reason = str(item.get("reason") or "resources")
            try:
                resource_class = ResourceClass(str(item.get("resource_class") or ""))
            except ValueError:
                extra[role] = f"{role} queued — waiting for resources ({reason})"
                continue
            holders = governor.holders_for_class(resource_class)
            queue_len = governor.queue_length_for_class(resource_class)
            extra[role] = _describe_resource_wait(
                role,
                resource_class,
                reason,
                holders,
                metrics=governor.current_metrics(),
                own_project=project_ns,
                queue_len=queue_len,
            )
        return extra

    def _resource_wait_for_role(self, project_ns: str, role: str) -> dict[str, str] | None:
        """(#412) The resource-governor's own queued-wait entry for *role* in
        *project_ns*, if any — checked regardless of whether a pane already
        exists for it.

        `_queued_resource_roles` above only ever surfaces a queued wait for a
        role with NO pane entry at all (a brand-new spawn blocked before it
        ever got one) — a re-assign to an ALREADY-spawned pane that then gets
        governor-queued (e.g. hitting `heavy_project_limit`) used to vanish
        from `takkub status`/`list` entirely: the pane's stale PREVIOUS
        display state kept showing, with nothing telling Lead a new task was
        actually parked behind a resource limit until the slot finally freed.
        Feeds `_derive_display_state`'s `queued:<reason>` tier via
        `list_status_detailed`, which now calls this for every role, not
        just ones missing from the pane map.
        """
        governor = getattr(self, "_resource_governor", None)
        if governor is None:
            return None
        try:
            waiting = governor.snapshot().get("waiting_tasks", [])
        except Exception:
            return None
        for item in waiting:
            if item.get("project_id") != project_ns:
                continue
            if str(item.get("pane_id") or "") != role:
                continue
            reason = str(item.get("reason") or "resources")
            try:
                resource_class = ResourceClass(str(item.get("resource_class") or ""))
            except ValueError:
                return {
                    "reason": reason,
                    "message": f"{role} queued — waiting for resources ({reason})",
                }
            holders = governor.holders_for_class(resource_class)
            queue_len = governor.queue_length_for_class(resource_class)
            message = _describe_resource_wait(
                role,
                resource_class,
                reason,
                holders,
                metrics=governor.current_metrics(),
                own_project=project_ns,
                queue_len=queue_len,
            )
            return {"reason": reason, "message": message}
        return None

    def inbox_report(self, project: str | None = None, role: str | None = None) -> list[dict]:
        """Read-only snapshot of every done/FAILED report still sitting
        somewhere in the outbound-to-Lead pipeline instead of already
        written into Lead's pane (#231): the digest debounce window, the
        ready-prompt live-notify queue, and the durable pending store
        (survives a restart).

        `takkub status` could only ever say a report was "queued — not yet
        delivered"; there was no command that read its actual content back
        out, forcing Lead to Glob `runtime/sessions/**` by hand. This is
        that command's backing data — `takkub inbox` prints it.

        Returns a list of ``{role, queue, body, origin_confirmed, queued_ts}``,
        newest first within each queue tier (digest, then live, then
        durable). ``origin_confirmed`` is `False` when the reporting pane's
        role slot was respawned since this item was queued (#228 — the same
        provenance check `_flush_lead_digest`/`_pump_lead_notify` apply at
        delivery time), `True` when confirmed live, `None` when no origin
        was recorded to check (system notices, CC relays, combined
        digests). ``queued_ts`` (#241) is the epoch time the item joined the
        digest debounce window, or `None` for tiers that don't track it.
        Optionally filtered to a single *role*.

        As a side effect (#241), every body returned here is fingerprinted
        into `_inbox_seen[project_ns]` — if the SAME body later flushes out
        of `_lead_digest_queue` via the normal digest pump, `_flush_lead_digest`
        collapses it to a one-line reference instead of re-pasting content
        Lead already read through this call.
        """
        project_ns = self._resolve_project(project)
        if not hasattr(self, "_inbox_seen"):
            self._inbox_seen = {}
        seen = self._inbox_seen.setdefault(project_ns, set())

        def _origin_confirmed(
            item_role: str | None, pane_token: str | None, queued_ts: float | None = None
        ) -> bool | None:
            if item_role is None or pane_token is None:
                return None
            return not self._provenance_stale(
                project_ns, item_role, pane_token, queued_ts=queued_ts
            )

        items: list[dict] = []

        for entry in getattr(self, "_lead_digest_queue", {}).get(project_ns, ()):
            body, pane_token, queued_ts = _unwrap_notice_item(entry)
            item_role = _notice_role_tag(body) or "system"
            if role is not None and item_role != role:
                continue
            seen.add(_notice_fingerprint(body))
            items.append(
                {
                    "role": item_role,
                    "queue": "digest",
                    "body": body,
                    "origin_confirmed": _origin_confirmed(
                        item_role if item_role != "system" else None, pane_token, queued_ts
                    ),
                    "queued_ts": queued_ts,
                }
            )

        for entry in getattr(self, "_lead_notify_queue", {}).get(project_ns, ()):
            body, pane_token, live_ts = _unwrap_notice_item(entry)
            item_role = _notice_role_tag(body) or "system"
            if role is not None and item_role != role:
                continue
            items.append(
                {
                    "role": item_role,
                    "queue": "live",
                    "body": body,
                    "origin_confirmed": _origin_confirmed(
                        item_role if item_role != "system" else None, pane_token, live_ts
                    ),
                }
            )

        for item in getattr(self, "_pending_done_notices", {}).get(project_ns, ()):
            if not isinstance(item, dict):
                continue
            item_role = item.get("role") or "system"
            if role is not None and item_role != role:
                continue
            item_body = item.get("body", "")
            tagged_role = _notice_role_tag(item_body) or (
                item_role if item_role != "system" else None
            )
            items.append(
                {
                    "role": item_role,
                    "queue": "durable",
                    "body": item_body,
                    "origin_confirmed": _origin_confirmed(
                        tagged_role, item.get("pane_token"), item.get("queued_ts")
                    ),
                }
            )

        return items

    def list_status(self, project: str | None = None) -> dict[str, str]:
        """Snapshot of `role → state` for one project's panes.

        Defaults to the active project's view, so a Lead in project-a never
        accidentally sees a backend pane that belongs to project-b. Also
        surfaces a just-closed role whose done report hasn't reached Lead
        yet (#163), and a role still queued behind a resource-governor slot
        with no pane yet (#240), instead of letting either silently vanish
        from the list.
        """
        project_ns = self._resolve_project(project)
        status = {
            name: self._pane_display_state(p) for name, p in self._project_panes(project_ns).items()
        }
        status.update(self._pending_notice_roles(project_ns, status))
        status.update(self._queued_resource_roles(project_ns, status))
        return status

    def _pane_display_state(self, pane: AgentPane) -> str:
        """Refine `pane.state == "active"` into spawning/active/ready (#248/#247).

        `pane.state` is set once at spawn (`AgentPane.attach_session` →
        "active") and never distinguished "CLI process launched, nothing
        printed yet" from "CLI is up and idle at its own ready prompt" — both
        read identically as "active" to `takkub list`/`status`, which is
        exactly what made a hung spawn (a codex pane whose CLI never printed
        anything, an agy pane stuck spinning on "Signing in...") look the
        same as a healthy idle pane waiting for a task.

        Every other `pane.state` value ("working", "done", "empty", or a
        pending-notice/queued label) is returned unchanged — this only adds
        a richer label for the "active" case, so every existing
        `pane.state == "working"` / etc. check elsewhere is unaffected."""
        if pane.state != "active":
            return pane.state
        session = pane.session
        if session is None:
            return "spawning"
        try:
            has_content = session.first_content_ts() is not None
        except Exception:
            return "active"  # fail open to the old label
        if not has_content:
            return "spawning"
        try:
            ready = session.is_at_ready_prompt_cached()
        except Exception:
            ready = False
        return "ready" if ready else "active"

    def _compute_last_progress_ts(self, role: str, project_ns: str, pane: AgentPane) -> float:
        """Return the most-recent activity timestamp for `pane` (0.0 = no baseline).

        Checks three signals and returns the largest (= most recent):
          1. Spinner-filtered content-delta clock (`PaneState.last_content_change_ts`,
             maintained every IDLE_WATCHDOG_INTERVAL_MS tick by `_check_stuck_panes`) —
             falls back to raw transcript file mtime only before that clock has ever
             been populated for this pane (e.g. the first few seconds after spawn)
          2. Today's screenshot directory mtime — QA captured a new shot
          3. Last `takkub send` delivery timestamp — orchestrator pushed a message

        #236: signal 1 used to be raw transcript mtime unconditionally, which bumps
        on every PTY byte including an animated spinner ("Doing... (esc to cancel,
        412s)") or a permission-dialog redraw — a pane wedged on either read as
        "last progress: 0s ago" forever, so stall detection could never fire.
        `last_content_change_ts` is the same spinner/counter-filtered hash clock
        `_check_stuck_panes`'s auto-recover watchdog already relies on (Lead is
        skipped there, so Lead panes always fall back to transcript mtime here —
        matches prior behaviour, Lead was never in scope for stall detection).
        """
        ts = 0.0

        ps = (getattr(self, "_pane_state", None) or {}).get(f"{project_ns}::{role}")
        content_ts = ps.last_content_change_ts if ps is not None else None
        if content_ts:
            ts = content_ts
        else:
            transcript_path = getattr(pane, "_transcript_path", None)
            if transcript_path:
                try:
                    mt = pathlib.Path(transcript_path).stat().st_mtime
                    if mt > ts:
                        ts = mt
                except OSError:
                    pass

        if _split_shard(role)[0] in ("qa", "critic", "designer"):
            today = datetime.now().strftime("%Y-%m-%d")
            shot_dir = RUNTIME_DIR / "exports" / today / project_ns / "screenshots"
            try:
                mt = shot_dir.stat().st_mtime
                if mt > ts:
                    ts = mt
            except OSError:
                pass

        send_ts = (
            getattr(self, "_pane_state", {}).get(f"{project_ns}::{role}") or PaneState()
        ).last_send_ts
        if send_ts > ts:
            ts = send_ts

        return ts

    def _role_delivery_unconfirmed(self, project_ns: str, role: str) -> bool:
        """(#263) True when a "task may not have landed" system notice
        (spawn-failed / delivery-unconfirmed / spawn-stuck / delivery-boot-stall
        — `_BLOCKING_NOTICE_MARKERS`, NOT delivery-busy-wait: that one means
        delivery DID land and the pane is just being slow) is currently
        pending for *role*.

        Exists because `pane.state` is an ORCHESTRATOR-DECLARED label
        stamped optimistically at dispatch time ("task assigned = working"),
        never derived from whether the paste actually reached the pane —
        real evidence (#263): a gemini pane read "working" from minute one
        while its screen was stuck on "Signing in..." and the task was
        never pasted in at all, with the cockpit's own
        `[delivery-unconfirmed]` notice sitting unread the whole time.
        `list_status_detailed` surfaces this as an additive flag (not a
        `pane.state` mutation, so every existing `pane.state == "working"`
        check elsewhere is unaffected) — a Lead reading `takkub status` can
        now tell "genuinely running" from "assigned, but delivery layer
        itself isn't sure it landed" instead of both reading identically as
        bare "working".

        Deliberately NOT a full unification of the 3 signals #263
        describes (declared state / ready marker / progress clock) — that
        would mean re-deriving `pane.state` itself from ready-marker
        scraping, which lives in provider-specific calibration
        (`provider_spec.py` / pty_section fixtures) reserved for a parallel
        fix (#256/#257) this change deliberately does not touch. This is
        the narrower, safely-additive slice: expose the ALREADY-COMPUTED
        delivery-health signal next to the existing state instead of
        trusting `pane.state` alone.

        Scans the 3 raw queues directly (same shape `_has_pending_lead_notice`
        already reads) rather than going through `inbox_report` — that
        method's side effect of fingerprinting every returned body into
        `_inbox_seen` (so a LATER `takkub inbox` pull collapses a digest
        line the reader already saw) is exactly wrong here: this check runs
        on every `list_status_detailed()` call (i.e. every `takkub
        status`/`takkub list`), and marking a still-pending digest item
        "already read" just because Lead happened to check status would
        make it render collapsed once it actually flushes — Lead never
        explicitly read it.
        """

        def _matches(body: object) -> bool:
            text = str(body)
            lowered = text.lower()
            if not any(marker in lowered for marker in _BLOCKING_NOTICE_MARKERS):
                return False
            return _system_marker_role(text) == role

        for entry in getattr(self, "_lead_digest_queue", {}).get(project_ns, ()):
            body, *_rest = _unwrap_notice_item(entry)
            if _matches(body):
                return True
        for entry in getattr(self, "_lead_notify_queue", {}).get(project_ns, ()):
            body, *_rest = _unwrap_notice_item(entry)
            if _matches(body):
                return True
        for item in getattr(self, "_pending_done_notices", {}).get(project_ns, ()):
            if isinstance(item, dict) and _matches(item.get("body", "")):
                return True
        return False

    def _derive_display_state(
        self,
        pane: AgentPane,
        base_state: str,
        delivery_unconfirmed: bool,
        quota_stalled: bool = False,
        resource_wait_reason: str | None = None,
        waiting_for_lead: bool = False,
    ) -> str:
        """(#263) Combine the 3 disagreeing sources of truth the issue names —
        `pane.state` (orchestrator-declared at dispatch), the ready-marker
        screen scrape, and the delivery-health notice signal — into ONE state
        string for `takkub list`/`status`'s display, with an explicit
        priority order so no two of them can silently contradict each other
        on screen the way the issue's evidence showed (gemini read "working"
        while stuck on "Signing in..."; codex read "active" while the screen
        plainly showed "Working (0s - esc to interrupt)"; kimi read "active"
        while stuck needing `/login`).

        Deliberately layered ON TOP OF `_pane_display_state`'s existing
        spawning/active/ready/pass-through refinement (`base_state` — the
        `"state"` key `list_status_detailed` already returns) instead of
        replacing it: every existing `pane.state`/`"state"`-keyed check
        elsewhere (`_resolve_role_wait_status`'s `state == "working"`, stall
        detection above, the pre-existing `_pane_display_state` unit tests)
        keeps reading the UNCHANGED base value. This produces a SEPARATE
        `"display_state"` key — additive only, same contract as
        `blocked_reason`/`delivery_unconfirmed` before it.

        Priority (first match wins — screen-scraped ground truth always beats
        an orchestrator-declared or notice-derived label, since the CLI
        itself cannot be doing two contradictory things at once):

          -1. **queued:<reason>** (#412) — `resource_wait_reason` is set, i.e.
             `ResourceGovernor` currently holds a queued task for this exact
             (project, role) — e.g. "queued:heavy_project_limit". Checked
             even ahead of quota-stall: this is the one tier that applies to
             an ALREADY-spawned pane being denied a slot for a NEW task, a
             case `_queued_resource_roles` never covers (it only ever adds an
             entry for a role with no pane at ALL yet) — before this, a
             re-assign that got governor-queued silently kept showing the
             pane's stale PREVIOUS state on `takkub status`/`list`, with
             nothing telling Lead a task was actually parked behind a limit.
          0. **stalled:quota** (#301) — `quota_stalled` is True, i.e. the
             idle watchdog's own `_rate_limit_suppressed` already recorded
             `PaneState.rate_limited_until` for this role this tick. Checked
             FIRST, ahead of even login-required: a pane frozen on its own
             quota banner cannot simultaneously be doing anything else, and
             re-deriving the same verdict here from a second screen-scrape
             risks disagreeing with the watchdog's own recorded state
             (`rate_limit_reset_at()` is a live scrape — the banner text can
             scroll out of view a tick after the watchdog captured it).
             Reuses that ALREADY-COMPUTED signal instead.
          0b. **blocked:provider-account** (#346) — `session.
             account_pending_reason(provider)` matches (gemini/agy stuck on
             its own "Verifying your account..." eligibility gate past its
             grace period). Checked ahead of login-required: this is NOT a
             login/credentials problem (re-authenticating fixes nothing), so
             it must not be lumped into that label's "log back in" wording.
          1. **login-required** — `session.auth_failure_reason(provider)`
             matches (kimi "send /login to login", gemini stuck "Signing
             in..." past its grace period, ...). The screen is definitive: a
             CLI stuck here cannot simultaneously be "working" or "ready" no
             matter what `pane.state` optimistically claims.
          1b. **stalled:tool** (#308) — `base_state == "working"` and
             `session.tool_running_marker(provider)` has matched with no
             other content change for `TOOL_STUCK_TIMEOUT_SEC`. Checked here
             (ahead of booting/waiting-delivery) for the same "screen is
             definitive" reason as login-required: real incident evidence
             was a pane whose idle footer stayed visible the entire time it
             was wedged, so this must not wait behind a tier that a false
             idle-looking screen could otherwise satisfy first.
          2. **booting** — `session.shows_boot_phase_marker()` (codex/agy
             MCP cold-boot chrome: "booting mcp server"). Distinct from
             `base_state == "spawning"` (nothing printed at all yet) — this
             is "printed a banner, still not at its own ready prompt".
             Deliberately NOT the wider `shows_startup_marker()`, which also
             matches a pane that is mid-turn with a queued message (#281).
          2b. **waiting-lead** (#463) — `base_state == "working"` and
             `waiting_for_lead` is True, i.e. this role's `PaneState.
             blocked_on_lead_ts` was stamped recently (`send()`'s
             sent-to-lead path, or `progress()` — #461) and hasn't expired,
             *and* `PaneState.last_turn_end_ts` (stamped by a non-blocking
             Stop hook in `consume_pane_hook`) is not None and is `>=`
             `blocked_on_lead_ts` (#463 follow-up). The second half of that
             AND matters: `progress()` alone is NOT proof the turn ended — a
             pane can call `takkub progress "..."` mid-task and keep working
             for several more minutes before really stopping, and without
             requiring a Stop hook *after* that call, this tier would read
             "waiting-lead" for up to 30 minutes while the pane is still
             actively working (real incident: this exact backend pane did
             so on 2026-09-01, `progress()` at 09:45 followed by 12 more
             minutes of work). This is a HOOK/API-signalled fact (the pane
             itself just told the cockpit it ended its turn waiting on
             Lead), trusted ahead of the live PTY-scrape-derived tiers below
             it: real evidence was a claude 2.1.252 pane whose bottom status
             line kept rendering its own trailing spinner text ("✽
             Mulling…"/"running stop hook · Ns") for minutes after the Stop
             hook had already fired and `progress()` had already been
             called, so `takkub status` kept reporting bare "working" long
             after the pane was genuinely idle waiting on Lead's reply
             (#463). Checked ahead of `waiting-delivery` because a pane that
             just self-reported via `progress()`/`send()` is stronger, more
             current proof its delivery reached it than an older, ambiguous
             health-notice signal. A provider with no Stop hook
             (codex/gemini-agy/opencode/…) never stamps `last_turn_end_ts`,
             so this tier never fires for it — falls back to the
             PTY-scrape tiers below, unchanged (claude-only by
             construction, #103).
          3. **waiting-delivery** — `base_state == "working"` and
             `delivery_unconfirmed` is True: the orchestrator declared the
             task dispatched, but the delivery layer's own health notice
             says it isn't sure the paste ever landed. Covers a provider
             with no calibrated auth marker to catch tier 1's shape of the
             same underlying problem.
          4. **busy** — `base_state == "active"` (i.e. NOT yet promoted to
             "working" by any dispatch) but `session.is_hard_blocked_for`
             says the screen's own hard-blocker markers ("esc to
             interrupt", ...) show the CLI actively generating/interrupting
             anyway — the codex evidence above. Splits "genuinely idle"
             apart from "busy for a reason the orchestrator doesn't know
             about" instead of both reading as the same bare "active".
          5. **unknown** — `base_state == "active"` and this pane's provider
             is in `uncalibrated_providers()` (empty `ready_rules` —
             currently only "cursor"): the active/ready split depends on a
             ready-marker table never confirmed for this provider, so
             claiming either confidently would be a guess dressed up as
             fact (same "ตรวจไม่ได้ beats a confident wrong label"
             principle as #251).
          6. Otherwise `base_state` unchanged.

        All screen-scrape checks are best-effort: any exception from a loose
        test double or a torn-down session falls through to the next tier
        instead of raising, matching `_pane_display_state`'s own
        `except Exception` fallback.
        """
        if resource_wait_reason:
            return f"queued:{resource_wait_reason}"
        if base_state not in ("active", "working"):
            return base_state
        if quota_stalled:
            return "stalled:quota"
        session = pane.session
        if session is None:
            return base_state
        provider = getattr(pane.model, "provider_name", None) or "claude"

        try:
            account_reason = session.account_pending_reason(provider)
        except Exception:
            account_reason = None
        # Same isinstance guard as the tool_marker check below: a loosely
        # mocked test session's un-stubbed attribute call returns a truthy
        # MagicMock, not None/str (#346 fix-loop — this exact trap bit
        # test_quota_detection.py's TestDisplayStateQuotaPriority).
        if not isinstance(account_reason, str) or not account_reason:
            account_reason = None
        if account_reason:
            return "blocked:provider-account"

        try:
            auth_reason = session.auth_failure_reason(provider)
        except Exception:
            auth_reason = None
        if auth_reason:
            return "login-required"

        if base_state == "working":
            try:
                _tool_marker = session.tool_running_marker(provider)
                _tool_stale = session.seconds_since_output()
            except Exception:
                _tool_marker = None
                _tool_stale = 0.0
            # Same isinstance guard as _check_stuck_tool_panes: a loosely
            # mocked test session's un-stubbed attribute call returns a
            # truthy MagicMock, not None/str/float.
            if not isinstance(_tool_marker, str) or not _tool_marker:
                _tool_marker = None
            if not isinstance(_tool_stale, (int, float)):
                _tool_stale = 0.0
            if _tool_marker is not None and _tool_stale >= TOOL_STUCK_TIMEOUT_SEC:
                return "stalled:tool"

        try:
            # #281: boot-phase only. The wider `shows_startup_marker()` also
            # matches "mid-turn with a queued message", so `takkub list` used
            # to label a codex pane that was actively working as "booting".
            booting = session.shows_boot_phase_marker()
        except Exception:
            booting = False
        if booting:
            return "booting"

        if base_state == "working" and waiting_for_lead:
            return "waiting-lead"

        if base_state == "working":
            return "waiting-delivery" if delivery_unconfirmed else base_state

        # base_state == "active" from here on.
        try:
            busy = session.is_hard_blocked_for(provider)
        except Exception:
            busy = False
        if busy:
            return "busy"

        from .provider_spec import uncalibrated_providers

        try:
            uncalibrated = provider in uncalibrated_providers()
        except Exception:
            uncalibrated = False
        if uncalibrated:
            return "unknown"

        return base_state

    def list_status_detailed(self, project: str | None = None) -> dict[str, dict]:
        """Extended status snapshot with stall detection.

        Returns `{role: {"state": str, "stall_minutes": int|None,
        "last_progress_ts": float, "blocked_reason": str|None,
        "delivery_unconfirmed": bool}}`.
        `stall_minutes` is set when the pane is `working` and no progress signal
        has been seen for more than STALL_THRESHOLD_SEC.

        `blocked_reason` (#236) is `None` / "trust" / "permission" / "tty" —
        set whenever the pane is sitting on a prompt that needs a human
        keypress, so a Lead reading `takkub status` sees that concretely
        instead of a bare "working" that never distinguishes "generating"
        from "stuck waiting for a keypress" (the devops pane that sat on a
        permission dialog for 2h51m while status kept reporting "working,
        progress 0s ago"). `pane.state` itself is left untouched — this is
        additive reporting only, not a new internal pane-state value, so
        every existing `pane.state == "working"` check elsewhere is unaffected.

        `delivery_unconfirmed` (#263) is True when a delivery-health system
        notice is currently pending for this role — see
        `_role_delivery_unconfirmed`. Same additive-only contract as
        `blocked_reason`.

        `display_state` (#263) is `_derive_display_state`'s unified verdict —
        the same `"state"` value, further refined into "login-required" /
        "booting" / "waiting-delivery" / "busy" / "unknown" / "stalled:quota"
        (#301) / "blocked:provider-account" (#346) where the raw `"state"`
        would otherwise show a misleadingly
        bare "working"/"active" (see that method's docstring for the full
        priority order). This is what `takkub list`/`status` render to Lead;
        `"state"` itself stays exactly as before for every internal consumer
        (wait resolution, stall detection) that depends on its literal value.

        `quota_resets_at` / `quota_marker` (#301) mirror `PaneState`'s own
        fields whenever `display_state == "stalled:quota"`, else `0.0`/`""` —
        so `takkub status`/`list` can render "resets in Xh" and quote the
        matched banner phrase without a second screen-scrape. `model` (#301)
        is `PtySession.current_model_label(provider)` — non-None only for a
        calibrated provider (currently gemini) that is actually showing a
        model label on screen; surfaces a silent quota-downgrade (Pro→Flash)
        Lead would otherwise never see.
        """
        now = time.time()
        project_ns = self._resolve_project(project)
        result: dict[str, dict] = {}
        for role, pane in self._project_panes(project_ns).items():
            state = pane.state
            stall_minutes: int | None = None
            last_progress_ts = 0.0
            blocked_reason: str | None = None
            delivery_unconfirmed = False
            quota_stalled = False
            quota_resets_at = 0.0
            quota_marker = ""
            model: str | None = None
            ps = getattr(self, "_pane_state", {}).get(f"{project_ns}::{role}")
            if ps is not None and ps.rate_limited_until > now:
                quota_stalled = True
                quota_resets_at = ps.rate_limited_until
                quota_marker = ps.quota_marker
            if state == "working" and pane.session is not None and pane.session.is_alive:
                last_progress_ts = self._compute_last_progress_ts(role, project_ns, pane)
                if last_progress_ts > 0:
                    silent_for = now - last_progress_ts
                    if silent_for >= STALL_THRESHOLD_SEC:
                        stall_minutes = int(silent_for // 60)
                try:
                    blocked_reason = _prompt_block_reason(pane.session)
                except Exception:
                    blocked_reason = None
                try:
                    delivery_unconfirmed = self._role_delivery_unconfirmed(project_ns, role)
                except Exception:
                    delivery_unconfirmed = False
                try:
                    provider = getattr(pane.model, "provider_name", None) or "claude"
                    model = pane.session.current_model_label(provider)
                except Exception:
                    model = None
            display_state_base = self._pane_display_state(pane)
            try:
                resource_wait = self._resource_wait_for_role(project_ns, role)
            except Exception:
                resource_wait = None
            # #463: `progress()`/`send()`'s "sent to lead" path stamps this
            # the moment the pane reports it's waiting on Lead — a hook/API
            # signal that must outrank a live PTY-scrape reading that can
            # still be showing a stale spinner tail past turn-end (see
            # `_derive_display_state`'s "waiting-lead" tier docstring).
            #
            # #463 follow-up: `blocked_on_lead_ts` alone is not proof the
            # turn actually ended — `progress()` stamps it mid-turn too (a
            # pane can call `takkub progress "..."` and keep working for
            # several more minutes before really stopping). Require
            # `last_turn_end_ts` (stamped by a non-blocking Stop hook,
            # `consume_pane_hook`) to be present AND at least as new as
            # `blocked_on_lead_ts`, i.e. the turn genuinely ended *after*
            # the pane last reported waiting on Lead. A provider with no
            # Stop hook (codex/gemini-agy/opencode/…) never stamps
            # `last_turn_end_ts`, so this tier never fires for it — it falls
            # back to the PTY-scrape tiers below, unchanged (claude-only by
            # construction, tracked under #103).
            waiting_for_lead = bool(
                ps is not None
                and ps.blocked_on_lead_ts is not None
                and (now - ps.blocked_on_lead_ts) < 30 * 60
                and ps.last_turn_end_ts is not None
                and ps.last_turn_end_ts >= ps.blocked_on_lead_ts
            )
            try:
                display_state = self._derive_display_state(
                    pane,
                    display_state_base,
                    delivery_unconfirmed,
                    quota_stalled,
                    resource_wait_reason=(resource_wait["reason"] if resource_wait else None),
                    waiting_for_lead=waiting_for_lead,
                )
            except Exception:
                display_state = display_state_base
            result[role] = {
                "state": display_state_base,
                "display_state": display_state,
                "stall_minutes": stall_minutes,
                "last_progress_ts": last_progress_ts,
                "blocked_reason": blocked_reason,
                "delivery_unconfirmed": delivery_unconfirmed,
                "quota_resets_at": quota_resets_at,
                "quota_marker": quota_marker,
                "model": model,
                "resource_wait_message": resource_wait["message"] if resource_wait else None,
            }
        for role, state in self._pending_notice_roles(project_ns, result).items():
            result[role] = {
                "state": state,
                "display_state": state,
                "stall_minutes": None,
                "last_progress_ts": 0.0,
                "blocked_reason": None,
                "delivery_unconfirmed": False,
                "quota_resets_at": 0.0,
                "quota_marker": "",
                "model": None,
                "resource_wait_message": None,
            }
        for role, state in self._queued_resource_roles(project_ns, result).items():
            result[role] = {
                "state": state,
                "display_state": state,
                "stall_minutes": None,
                "last_progress_ts": 0.0,
                "blocked_reason": None,
                "delivery_unconfirmed": False,
                "quota_resets_at": 0.0,
                "quota_marker": "",
                "model": None,
                "resource_wait_message": state,
            }
        return result

    def performance_status(self, project: str | None = None) -> dict:
        """Read-only live reliability metrics for ``takkub doctor --live``."""
        project_ns = self._resolve_project(project)
        governor = getattr(self, "_resource_governor", None)
        resource = governor.snapshot() if governor is not None else {}
        writer_queues: dict[str, dict[str, int]] = {}
        for role, pane in self._project_panes(project_ns).items():
            session = getattr(pane, "session", None)
            if session is None:
                continue
            writer_queues[role] = {
                "depth": int(getattr(session, "writer_queue_depth", 0)),
                "stale_dropped": int(getattr(session, "writer_stale_drop_count", 0)),
                "queue_full": int(getattr(session, "writer_queue_full_count", 0)),
                "output_rate_bps": float(getattr(session, "output_rate_bps", 0.0)),
            }
        deliveries = getattr(self, "_delivery_manager", None)
        delivery_rows = deliveries.snapshot() if deliveries is not None else []
        state_counts: dict[str, int] = {}
        for row in delivery_rows:
            state = str(row.get("state", "unknown"))
            state_counts[state] = state_counts.get(state, 0) + 1
        waiting_roles = {
            str(item.get("pane_id"))
            for item in resource.get("waiting_tasks", [])
            if item.get("project_id") == project_ns
        }
        lifecycle: dict[str, str] = {}
        for role, pane in self._project_panes(project_ns).items():
            if role in waiting_roles:
                lifecycle[role] = "WAITING_RESOURCE"
            elif getattr(pane, "state", "") == "working":
                lifecycle[role] = "RUNNING"
            elif getattr(getattr(pane, "session", None), "is_alive", False):
                lifecycle[role] = "SPAWNED_IDLE"
        return {
            **resource,
            "writer_queues": writer_queues,
            "delivery_states": state_counts,
            "task_lifecycle": lifecycle,
            "duplicate_notices_prevented": int(
                getattr(getattr(self, "_notice_deduper", None), "duplicate_count", 0)
            ),
            "spawn_queue_depth": len(getattr(self, "_spawn_queue", ())),
            "fanout_queue_depth": sum(
                len(queue) for queue in getattr(self, "_fanout_queue", {}).values()
            ),
            "main_thread_stall_count": int(getattr(self, "_main_thread_stall_count", 0)),
            "latest_main_thread_stall": dict(getattr(self, "_latest_main_thread_stall", {})),
        }

    def pane_ram_specs(self) -> list[dict]:
        """Cheap (no psutil) per-pane facts for the RAM report (#364 lever
        6): role, project, provider and root PID for every live pane across
        every project namespace. Split out from `ram_status()` so a caller
        that must not do psutil work on the Qt main thread — the performance
        chip's background worker — can gather this part inline (main thread,
        instant) and hand it to `ram_report.collect_ram_report` off-thread."""
        specs: list[dict] = []
        for project_ns, panes in self._panes_by_project.items():
            for role, pane in panes.items():
                session = getattr(pane, "session", None)
                # #364 lever 1: surface whether this pane's renderer is
                # currently discarded so `doctor --ram` shows it directly
                # instead of just a RAM number the reader has to interpret.
                terminal = getattr(pane, "_terminal", None)
                specs.append(
                    {
                        "role": role,
                        "project": project_ns,
                        "provider": getattr(pane, "provider_name", None),
                        "pid": getattr(session, "pid", None),
                        "discarded": bool(getattr(terminal, "is_discarded", False)),
                    }
                )
        return specs

    def ram_status(self) -> dict:
        """Live per-pane RAM breakdown for `takkub doctor --ram` (#364 lever
        6). Synchronous/on the Qt main thread like `performance_status` above
        — acceptable here because this is only ever invoked by an explicit,
        one-off `takkub doctor --ram` call, never a recurring timer tick (the
        performance chip instead calls `ram_report.collect_ram_report`
        directly from a background worker — see status_header.py)."""
        from . import ram_report

        governor = getattr(self, "_resource_governor", None)
        min_ram = float(governor.limits.min_available_ram_percent) if governor is not None else None
        return ram_report.collect_ram_report(
            self.pane_ram_specs(),
            main_pid=os.getpid(),
            governor_min_ram_percent=min_ram,
        )

    def ram_profile(self) -> dict:
        """On-demand main-process tracemalloc/gc profile for `takkub doctor
        --ram --ram-profile` (#364 lever 5). Same synchronous/opt-in-only
        convention as `ram_status` above — see `ram_report.collect_main_process_profile`'s
        docstring for why this is a lower-bound diagnostic, not a full
        accounting of this process's RSS."""
        from . import ram_report

        return ram_report.collect_main_process_profile()

    # ──────────────────────────────────────────────────────────────
    # #365 phase 10 — workspace diagnostics injection + `takkub doctor
    # --workspace` IPC (13_PERFORMANCE_AND_QT_RULES.md rule 10)
    # ──────────────────────────────────────────────────────────────
    def set_editor_host(self, host) -> None:
        """Injected by main_window right after constructing the one
        app-wide EditorHost. `host` is never imported/typed here —
        `editor_widget.py` pulls in PyQt6.QtWebEngineWidgets, which this
        module has no reason to depend on just for a diagnostics read."""
        self._editor_host_ref = host

    def register_workspace_diag_source(self, project: str, key: str, obj: object) -> None:
        self._workspace_diag_sources.setdefault(project, {})[key] = obj

    def unregister_workspace_diag_sources(self, project: str) -> None:
        self._workspace_diag_sources.pop(project, None)

    def workspace_status(self, project: str | None = None) -> dict:
        """Live workspace diagnostics for `takkub doctor --workspace`
        (#365 phase 10). Synchronous/on the Qt main thread, same
        one-off-only convention as `ram_status`/`performance_status` above
        — every read here is either a trivial attribute (the registered
        sources' `.diagnostics()` methods are pure state reads, see each
        module's own doctstring) or a small local JSONL fold (design
        artifacts), never a fresh subprocess/filesystem scan triggered by
        this call itself.
        """
        project_ns = self._resolve_project(project) if project else None
        projects = [project_ns] if project_ns else sorted(self._workspace_diag_sources)

        editor: dict = {"registered": False}
        host = self._editor_host_ref
        if host is not None:
            editor = {
                "registered": True,
                "has_view": bool(host.has_view()),
                "open_count": int(host.open_count()),
            }

        preview_states: dict[str, dict] = {}
        controller = getattr(self, "_preview_controller", None)
        nav_blocks: dict[str, int] = {}
        if controller is not None:
            nav_blocks = controller.nav_block_counts()
            for proj, state in controller.all_states().items():
                if project_ns is not None and proj != project_ns:
                    continue
                preview_states[proj] = {
                    **state.as_dict(),
                    "nav_blocks": nav_blocks.get(proj, 0),
                }

        design_artifacts: dict[str, dict] = {}
        if project_ns is not None:
            candidate_projects = [project_ns]
        else:
            from .config import load_projects as _load_projects_ws

            candidate_projects = sorted(_load_projects_ws().get("projects") or {})
        for proj in candidate_projects:
            try:
                from .design_actions import DesignArtifactRegistry

                artifacts = DesignArtifactRegistry(proj).all()
            except Exception as exc:  # defensive — a malformed store must not crash doctor
                design_artifacts[proj] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            if not artifacts:
                continue
            by_status: dict[str, int] = {}
            for a in artifacts:
                by_status[a.status] = by_status.get(a.status, 0) + 1
            design_artifacts[proj] = {"count": len(artifacts), "by_status": by_status}

        per_project_sources: dict[str, dict] = {}
        for proj in projects:
            sources = self._workspace_diag_sources.get(proj, {})
            row: dict = {}
            for key in ("tree_index", "file_watch", "git_changes"):
                obj = sources.get(key)
                if obj is not None and hasattr(obj, "diagnostics"):
                    row[key] = obj.diagnostics()
            if row:
                per_project_sources[proj] = row

        return {
            "editor_host": editor,
            "preview": preview_states,
            "design_artifacts": design_artifacts,
            "per_project": per_project_sources,
        }

    # ──────────────────────────────────────────────────────────────
    # #365 phase 5 — Live Preview + design artifact IPC
    #
    # Not lead-only (LEAD_ONLY_COMMANDS/_LEAD_ONLY_CMDS): these commands
    # never touch pane lifecycle (spawn/close/assign) and never read another
    # role's message/report content — same "trust-local, scoped by
    # from_project" tier `list`/`ram-status`/`performance-status` already
    # sit at. Any pane legitimately drives its own project's Preview (e.g.
    # frontend focusing its own dev server, or a Designer/critic pane
    # publishing a mockup) — restricting that to Lead would just make Lead
    # a relay for work that isn't Lead's to do.
    # ──────────────────────────────────────────────────────────────
    def preview_status(self, project: str):
        """Current `PreviewState | None` for *project* — the read-only half
        of `preview_command`'s "status" action, exposed directly for
        in-process callers (`main_window.py`'s tab-switch sync, #369
        BUG-002) that need it without going through the IPC dict-shape."""
        return self._preview_controller.status(project)

    def preview_command(
        self,
        action: str,
        *,
        project: str | None,
        url: str | None = None,
        path: str | None = None,
        device: str | None = None,
    ) -> tuple[bool, str, dict]:
        """`takkub preview` IPC. Returns `(ok, msg, extra)` — `extra` carries
        the resulting `PreviewState` (as a dict) for `open-url`/`open-file`/
        `status`, empty for `close`."""
        from .preview_controller import approved_artifact_roots

        project_ns = self._resolve_project(project)
        controller = self._preview_controller
        try:
            if action == "open-url":
                if not url:
                    return False, "missing arg: 'url'", {}
                state = controller.open_url(project_ns, url, device=device or "desktop")
            elif action == "open-file":
                if not path:
                    return False, "missing arg: 'path'", {}
                roots = approved_artifact_roots(project_ns)
                state = controller.open_file(project_ns, path, roots, device=device or "desktop")
            elif action == "close":
                closed = controller.close(project_ns)
                return closed, ("preview closed" if closed else "no open preview"), {}
            elif action == "status":
                state = controller.status(project_ns)
                if state is None:
                    return True, "no open preview", {"open": False}
                return True, "preview status", {"open": True, **state.as_dict()}
            else:
                return False, f"unknown preview action: {action!r}", {}
        except ValueError as exc:  # loopback/extension/containment policy rejection
            return False, str(exc), {}
        return (
            True,
            f"preview {action.replace('-', ' ')}: {state.mode} {state.target}",
            state.as_dict(),
        )

    def design_publish(
        self,
        project: str | None,
        path: str,
        title: str,
        mode: str,
        *,
        from_role: str | None = None,
    ) -> tuple[bool, str, dict]:
        """`takkub design publish` IPC — validates, records a `draft`
        artifact, and ensures Preview shows it (protocol doc: "cockpit
        validates -> ensures Preview -> opens/focuses")."""
        from .design_actions import DesignArtifactError, publish_design_artifact

        project_ns = self._resolve_project(project)
        try:
            artifact = publish_design_artifact(
                project_ns,
                path,
                title,
                mode,
                created_by_role=from_role,
                preview_controller=self._preview_controller,
            )
        except (DesignArtifactError, ValueError) as exc:
            return False, str(exc), {}
        return True, f"published design artifact {artifact.artifact_id}", artifact.as_dict()

    def _live_design_feedback_role(
        self, project_ns: str, created_by_role: str | None
    ) -> str | None:
        """Pick the live pane role to route design approve/revise feedback
        to (#371 BUG-006) — the artifact's own creator when their pane is
        still alive (most likely to act on it), else the generic
        `designer` role. Provider-agnostic (checks pane liveness only, not
        which CLI backs it) so every provider (claude/codex/gemini-agy/
        opencode/kimi/cursor) qualifies the same way. `None` means no live
        candidate — caller falls back to Lead-only, same as before #371."""
        project_panes = self._project_panes(project_ns)
        for role in dict.fromkeys(r for r in (created_by_role, "designer") if r):
            pane = project_panes.get(role)
            if pane is not None and pane.session is not None and pane.session.is_alive:
                return role
        return None

    def design_approve(self, project: str | None, artifact_id: str) -> tuple[bool, str, dict]:
        from .design_actions import DesignArtifactError, approve

        project_ns = self._resolve_project(project)
        try:
            artifact = approve(project_ns, artifact_id)
        except DesignArtifactError as exc:
            return False, str(exc), {}
        # Short, artifact-id-only notice — never resend the artifact's HTML.
        self._notify_lead(
            project_ns,
            f'✅ [design] artifact {artifact_id} approved — "{artifact.title}"',
            from_role="system",
            note="design-approve",
            kind="design-approve",
        )
        target_role = self._live_design_feedback_role(project_ns, artifact.created_by_role)
        if target_role:
            self.send(
                target_role,
                f'✅ [design-approve] artifact {artifact_id} approved — "{artifact.title}"',
                from_role="system",
                project=project_ns,
            )
        return True, f"artifact {artifact_id} approved", artifact.as_dict()

    def design_revise(
        self, project: str | None, artifact_id: str, *, feedback: str = ""
    ) -> tuple[bool, str, dict]:
        from .design_actions import DesignArtifactError, format_revision_feedback, request_revision

        project_ns = self._resolve_project(project)
        try:
            artifact = request_revision(project_ns, artifact_id, feedback=feedback)
        except DesignArtifactError as exc:
            return False, str(exc), {}
        feedback_note = f" — {feedback}" if feedback else ""
        # #371 BUG-006: route the structured feedback straight to whichever
        # live pane can act on it, instead of only ever telling Lead.
        target_role = self._live_design_feedback_role(project_ns, artifact.created_by_role)
        if target_role:
            self.send(
                target_role,
                format_revision_feedback(artifact, feedback),
                from_role="system",
                project=project_ns,
            )
            lead_note = (
                f"🔁 [design] revision requested for artifact {artifact_id} — "
                f'"{artifact.title}"{feedback_note} (routed to {target_role})'
            )
        else:
            lead_note = (
                f"🔁 [design] revision requested for artifact {artifact_id} — "
                f'"{artifact.title}"{feedback_note} (no live designer pane — Lead fallback)'
            )
        self._notify_lead(
            project_ns, lead_note, from_role="system", note="design-revise", kind="design-revise"
        )
        return True, f"revision requested for artifact {artifact_id}", artifact.as_dict()

    def record_main_thread_stall(self, details: dict) -> None:
        """Record a watchdog stall with a read-only workload snapshot."""
        try:
            snap = self.performance_status()
        except Exception:
            snap = {}
        writers = snap.get("writer_queues") or {}
        enriched = {
            **details,
            "active_panes": sum(
                1
                for pane in self.panes.values()
                if getattr(getattr(pane, "session", None), "is_alive", False)
            ),
            "output_rate_bps": sum(
                float(row.get("output_rate_bps", 0.0)) for row in writers.values()
            ),
            "max_writer_depth": max(
                (int(row.get("depth", 0)) for row in writers.values()), default=0
            ),
            "spawn_in_progress": bool(getattr(self, "_spawn_in_progress", False)),
            "spawn_queue_depth": len(getattr(self, "_spawn_queue", ())),
            "active_heavy_tasks": int(snap.get("active_heavy_tasks", 0)),
        }
        self._main_thread_stall_count = int(getattr(self, "_main_thread_stall_count", 0)) + 1
        self._latest_main_thread_stall = enriched
        _log_event("main_thread_stall", **enriched)

    def reload_performance_settings(self) -> dict:
        """Apply persisted performance policy without restarting live panes."""
        from . import performance_settings

        settings = performance_settings.load()
        limits = GovernorLimits.from_environment()
        self._resource_governor.update_limits(limits)
        for pane in self.panes.values():
            apply_settings = getattr(pane, "apply_performance_settings", None)
            if callable(apply_settings):
                apply_settings(settings)
        self._resource_governor.sample()
        self._resource_governor.dispatch_waiting()
        self.statusChanged.emit()
        return self.performance_status()

    def live_worktree_paths(self, project: str | None = None) -> set[str]:
        """Absolute worktree checkout paths currently held by a LIVE pane.

        Used by `takkub worktree clean`'s live-pane guard (#187): a worktree
        an active pane is sitting in must never be removed, no matter how the
        command was invoked (`--force` included) — see
        `WorktreeManager.clean_isolated`. "Live" = a session process that is
        still alive, regardless of provider (claude/codex/gemini/opencode/
        kimi/cursor — PaneState.worktree is set the same way for all of
        them). Scoped to *project* only, matching the rest of the socket
        protocol's per-project isolation.
        """
        project_ns = self._resolve_project(project)
        paths: set[str] = set()
        for role, pane in self._project_panes(project_ns).items():
            if pane.session is None or not pane.session.is_alive:
                continue
            ps = self._pane_state.get(f"{project_ns}::{role}")
            wt = ps.worktree if ps is not None else None
            path = wt.get("path") if wt else None
            if path:
                paths.add(str(pathlib.Path(path).resolve()))
        return paths

    def pane_status_report(
        self,
        project: str | None = None,
        since_ts: float | None = None,
    ) -> dict:
        """Per-pane summary for `takkub status`.

        Returns `{"panes": {role: {...}}, "any_stalled": bool, "project": str}`.
        Each pane entry includes state, stall info, last-progress timestamps,
        transcript tail, newest screenshot path, and done events in the window.
        `since_ts` defaults to one hour ago when omitted.
        """
        now = time.time()
        if since_ts is None:
            since_ts = now - 3600
        project_ns = self._resolve_project(project)
        detailed = self.list_status_detailed(project=project_ns)
        panes_out: dict[str, dict] = {}

        for role, info in detailed.items():
            state = info["state"]
            display_state = info.get("display_state", state)
            last_ts = info["last_progress_ts"]
            stall_min = info["stall_minutes"]

            if last_ts > 0:
                age_sec = now - last_ts
                if age_sec < 60:
                    human_ts = f"{int(age_sec)}s ago"
                elif age_sec < 3600:
                    human_ts = f"{int(age_sec // 60)}m ago"
                else:
                    human_ts = f"{int(age_sec // 3600)}h ago"
                abs_ts = datetime.fromtimestamp(last_ts).strftime("%H:%M:%S")
            else:
                human_ts = "unknown"
                abs_ts = "unknown"

            pane = self._project_panes(project_ns).get(role)
            transcript_tail = ""
            if pane is not None:
                transcript_path = getattr(pane, "_transcript_path", None)
                if transcript_path:
                    try:
                        raw = _read_tail_bytes(
                            pathlib.Path(transcript_path), _TRANSCRIPT_TAIL_BYTES
                        )
                        lines = raw.decode("utf-8", errors="replace").splitlines()
                        tail_lines = [ln for ln in lines if ln.strip()][-5:]
                        tail_lines = [_ANSI.sub("", ln) for ln in tail_lines]
                        transcript_tail = "\n".join(tail_lines)
                    except OSError:
                        pass

                # #308: the transcript-file tail above is the last N raw
                # rendered lines, which is dominated by whatever chrome sits
                # at the BOTTOM of the screen — usually the composer/idle
                # footer, even while a tool call is genuinely wedged higher
                # up (agy's "? for shortcuts" stayed visible below "Running
                # command..." the whole 13-minute #308 incident). When the
                # LIVE screen shows a tool-running marker right now, surface
                # that real line instead of the misleading empty-looking
                # footer tail — cheap best-effort, never raises.
                if pane.session is not None:
                    try:
                        from .provider_config import effective_provider_for

                        _provider = effective_provider_for(role, project=project_ns)
                        _marker = pane.session.tool_running_marker(_provider)
                        # Guard against a loosely-mocked session in tests —
                        # same isinstance idiom as _check_stuck_tool_panes.
                        if isinstance(_marker, str) and _marker:
                            for _ln in reversed(pane.session.display_lines()):
                                if _marker in _ln.lower():
                                    transcript_tail = _ln.strip()
                                    break
                    except Exception:
                        pass

            last_screenshot = ""
            if _split_shard(role)[0] in ("qa", "critic", "designer"):
                today = datetime.now().strftime("%Y-%m-%d")
                shot_dir = RUNTIME_DIR / "exports" / today / project_ns / "screenshots"
                try:
                    shots = sorted(
                        shot_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True
                    )
                    if shots:
                        last_screenshot = str(shots[0])
                except OSError:
                    pass

            done_events: list[str] = []
            sessions_root = RUNTIME_DIR / "sessions"
            if sessions_root.is_dir():
                for day_dir in sorted(sessions_root.iterdir(), reverse=True):
                    if not day_dir.is_dir():
                        continue
                    proj_dir = day_dir / project_ns
                    if not proj_dir.is_dir():
                        continue
                    for f in sorted(proj_dir.iterdir()):
                        if f.suffix != ".md" or f.name.startswith("lead-"):
                            continue
                        if not f.name.startswith(f"{role}-"):
                            continue
                        try:
                            if f.stat().st_mtime >= since_ts:
                                done_events.append(f.name)
                        except OSError:
                            pass

            quota_resets_at = info.get("quota_resets_at") or 0.0
            quota_resets_human = _human_duration(quota_resets_at - now) if quota_resets_at else ""

            panes_out[role] = {
                "state": state,
                "display_state": display_state,
                "stall_minutes": stall_min,
                "last_progress_ts": last_ts,
                "last_progress_human": human_ts,
                "last_progress_abs": abs_ts,
                "blocked_reason": info.get("blocked_reason"),
                "delivery_unconfirmed": info.get("delivery_unconfirmed", False),
                "quota_resets_at": quota_resets_at,
                "quota_resets_human": quota_resets_human,
                "quota_marker": info.get("quota_marker") or "",
                "model": info.get("model"),
                "transcript_tail": transcript_tail,
                "last_screenshot": last_screenshot,
                "done_events": done_events,
                "resource_wait_message": info.get("resource_wait_message"),
            }

        any_stalled = any(info["stall_minutes"] is not None for info in panes_out.values())
        return {"panes": panes_out, "any_stalled": any_stalled, "project": project_ns}

    def harvest_info(
        self,
        role: str,
        project: str | None = None,
        since_ts: float | None = None,
        limit: int = 100,
    ) -> tuple[bool, str, dict]:
        """Return pane state + artifact list for `takkub harvest`.

        Returns (ok, msg, payload). When ok=False and the role is not running,
        msg contains 'role not running: <role>' so the CLI can set exit_code 2.
        payload keys: state, spawn_ts, since_ts, artifacts.
        """
        from .config import load_projects as _load_projects

        project_ns = self._resolve_project(project)
        pane = self._project_panes(project_ns).get(role)
        if pane is None:
            return False, f"role not running: {role}", {}

        spawn_ts_raw: float = getattr(pane, "_spawn_ts", 0.0) or 0.0
        if since_ts is None:
            since_ts = spawn_ts_raw if spawn_ts_raw > 0 else (time.time() - 3600)

        # Build scan paths: configured project paths + runtime/exports/<date>/<project>/
        try:
            data = _load_projects()
            paths_cfg: dict = data.get("projects", {}).get(project_ns, {}).get("paths", {})
        except Exception:
            paths_cfg = {}

        today = datetime.now().strftime("%Y-%m-%d")
        scan_bases: list[pathlib.Path] = [
            RUNTIME_DIR / "exports" / today / project_ns,
        ]
        for v in paths_cfg.values():
            scan_bases.append(pathlib.Path(str(v)))

        artifacts = scan_artifacts(scan_bases, since_ts, limit=limit)

        return (
            True,
            "ok",
            {
                "state": pane.state,
                "spawn_ts": spawn_ts_raw,
                "since_ts": since_ts,
                "artifacts": artifacts,
            },
        )

    def task_show_info(self, role: str, project: str | None = None) -> tuple[bool, str, dict]:
        """Return the full text of the last task assigned to `role` for `takkub
        task show` (issue #1 file-based task handoff).

        A short task never got a handoff file (paste stayed inline) — in that
        case this reads straight from `PaneState.last_assigned_task`, the same
        full-text field the crash-replay path uses, so the CLI works
        uniformly whether or not a pointer was used. Payload keys: `task`
        (full text), `task_file` (path or None).
        """
        project_ns = self._resolve_project(project)
        key = _exit_key(project_ns, role)
        ps = self._pane_state.get(key)
        if ps is None or not ps.last_assigned_task:
            return False, f"no task assigned to '{role}' yet", {}
        task_file = ps.last_assigned_task_file
        if task_file:
            try:
                content = pathlib.Path(task_file).read_text(encoding="utf-8")
            except OSError as e:
                return False, f"task file unreadable: {task_file} ({e})", {}
            return True, "task", {"task": content, "task_file": task_file}
        return True, "task", {"task": ps.last_assigned_task, "task_file": None}

    def cancel_task_delivery(self, role: str, project: str | None = None) -> tuple[bool, str]:
        """`takkub task cancel --role <r>` — cancel any task delivery still
        retrying toward `role`'s CURRENT pane session (issue #255).

        A delivery keeps self-healing (resend/repaste) until the target
        pane reaches its ready prompt or BUSY_WAIT_CEILING_SEC elapses — up
        to 30 minutes. If Lead recovers a wedged pane manually in the
        meantime (`takkub send` straight into it once it becomes reachable
        some other way — see `send()`'s own auto-cancel for that exact
        case), the ORIGINAL delivery this method targets is for a pane
        Lead is NOT actively fighting: e.g. Lead decided to abandon the
        assign entirely and hand the role a different task by hand, or
        wants to stop the resend storm before closing the pane. Without
        this, the only way to stop a still-retrying delivery was
        `close()` (which also tears down the session) —
        `task_delivery.DeliveryManager.cancel_for_session` already existed
        and is correct, it simply had no CLI entry point until now."""
        project_ns = self._resolve_project(project)
        pane = self._project_panes(project_ns).get(role)
        if pane is None:
            return self._cancel_queued_resource_task(role, project_ns)
        delivery_manager = getattr(self, "_delivery_manager", None)
        if delivery_manager is None:
            return True, f"no pending delivery for '{role}' (nothing has ever been delivered)"
        generation = int(getattr(pane, "_session_generation", 0))
        cancelled = delivery_manager.cancel_for_session(project_ns, role, generation)
        if cancelled:
            last_ids = getattr(self, "_last_delivery_ids", None)
            if last_ids is not None:
                last_ids.pop((project_ns, role), None)
            return True, f"cancelled {cancelled} pending delivery(ies) for '{role}'"
        return True, f"no pending delivery for '{role}'"

    def _cancel_queued_resource_task(self, role: str, project_ns: str) -> tuple[bool, str]:
        """`takkub task cancel --role <r>` for a role that never got a pane
        at all — still parked behind the resource governor's admission
        queue (#303 item 2).

        The pane-session cancel path above only ever handled a task that had
        already spawned; a gate-blocked assign has no pane, no session, no
        `PaneState`, so `cancel_task_delivery` used to be unreachable for it
        entirely (`_unknown_pane_message` unconditionally) — the only way
        out was waiting for a slot to free on its own, which reported as
        taking up to an hour in the field. Removes the item from the
        governor's waiting list (so it's never admitted later) and closes
        its `"queued"` ledger row (written at `assign()`'s enqueue time —
        see `_latest_queued_task_text`) the same way an ordinary `takkub
        task close` would.
        """
        governor = getattr(self, "_resource_governor", None)
        removed = governor.cancel_waiting(project_id=project_ns, pane_id=role) if governor else 0
        if not removed:
            return False, self._unknown_pane_message(role, project_ns)
        from . import task_ledger

        _closed, ledger_msg = task_ledger.close_role(project_ns, role, self._live_roles(project_ns))
        msg = f"cancelled {removed} queued task(s) for '{role}' waiting on a resource-governor slot"
        if ledger_msg:
            msg = f"{msg}\n{ledger_msg}"
        return True, msg

    def _live_roles(self, project_ns: str) -> frozenset[str]:
        """Roles in *project_ns* with a currently-alive pane session — the
        set `task_ledger`'s reconcile/close guards check before ever
        touching a role's ledger row (issue #166)."""
        return frozenset(
            role
            for role, pane in self._project_panes(project_ns).items()
            if pane.session is not None and pane.session.is_alive
        )

    def task_reconcile(self, project: str | None = None, dry_run: bool = False) -> tuple[bool, str]:
        """`takkub task reconcile [--dry-run]` — close ledger rows orphaned
        by a cockpit session that exited without ever calling `takkub done`
        (issue #166). Delegates the actual safety gate to
        `task_ledger._orphan_candidates`; this method only supplies the
        live-pane set that gate needs."""
        from . import task_ledger

        project_ns = self._resolve_project(project)
        live_roles = self._live_roles(project_ns)
        if dry_run:
            preview = task_ledger.preview_reconcile(project_ns, live_roles)
            if not preview:
                return True, "no orphaned rows to reconcile"
            lines = "\n".join(f"  - {c['role']} ({c['date']}) — {c['summary']}" for c in preview)
            return True, f"would close {len(preview)} orphaned row(s):\n{lines}"

        closed, warning = task_ledger.reconcile_orphaned(project_ns, live_roles)
        if not closed:
            msg = "no orphaned rows to reconcile"
        else:
            msg = f"closed {len(closed)} orphaned row(s): {', '.join(closed)}"
        if warning:
            msg = f"{msg}\n{warning}"
        return True, msg

    def task_close_role(
        self,
        role: str,
        project: str | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        """`takkub task close --role <r> [--force] [--dry-run]` — manually
        close one role's open ledger row (issue #166's user-facing escape
        hatch alongside the automatic `task_reconcile`)."""
        from . import task_ledger

        project_ns = self._resolve_project(project)
        live_roles = self._live_roles(project_ns)
        if dry_run:
            state = task_ledger.load_state(project_ns)
            ptr = state.get("open", {}).get(role)
            if ptr is None:
                return False, f"no open ledger row for role '{role}'"
            if role in live_roles and not force:
                return (
                    False,
                    f"'{role}' has a live pane right now — close the pane first "
                    f"(`takkub close --role {role}`), or pass --force to override",
                )
            return True, f"would close ledger row for '{role}' (assigned {ptr['date']})"

        return task_ledger.close_role(project_ns, role, live_roles, force=force)

    def _build_post_compact_brief(self, project_ns: str) -> str | None:
        """Return a markdown snippet summarising alive teammates for post-compact injection.

        Fires when `_LAST_SESSION_FILE` was written within _POST_COMPACT_DETECT_SEC
        and live teammates exist — indicating a cockpit restart after session compact.
        Returns None when no snapshot is fresh enough or no teammates are running.
        """
        if not _LAST_SESSION_FILE.is_file():
            return None
        try:
            age = time.time() - _LAST_SESSION_FILE.stat().st_mtime
        except OSError:
            return None
        if age > _POST_COMPACT_DETECT_SEC:
            return None

        project_panes = self._project_panes(project_ns)
        alive_teammates = [
            (role, pane)
            for role, pane in project_panes.items()
            if role != LEAD.name and pane.session is not None and pane.session.is_alive
        ]
        if not alive_teammates:
            return None

        now = time.time()
        lines: list[str] = [
            "",
            "---",
            "",
            "## 🔄 Post-compact status (auto-injected)",
            "",
            "cockpit เพิ่ง restart จาก session snapshot — pane ที่ยังทำงานอยู่:",
            "",
        ]
        for role, pane in alive_teammates:
            state = pane.state
            last_ts = self._compute_last_progress_ts(role, project_ns, pane)
            if last_ts > 0:
                age_s = now - last_ts
                if age_s < 60:
                    age_str = f"{int(age_s)}s ago"
                elif age_s < 3600:
                    age_str = f"{int(age_s // 60)}m ago"
                else:
                    age_str = f"{int(age_s // 3600)}h ago"
                ts_abs = datetime.fromtimestamp(last_ts).strftime("%H:%M:%S")
            else:
                age_str = "unknown"
                ts_abs = "unknown"

            lines.append(f"### {role} ({state}) — last progress: {age_str} ({ts_abs})")
            lines.append("")

            transcript_path = getattr(pane, "_transcript_path", None)
            if transcript_path:
                try:
                    raw = pathlib.Path(transcript_path).read_bytes()
                    raw_lines = raw.decode("utf-8", errors="replace").splitlines()
                    tail = [ln for ln in raw_lines if ln.strip()][-5:]
                    if tail:
                        lines.append("```")
                        lines.extend(tail)
                        lines.append("```")
                except OSError:
                    pass
            lines.append("")

        brief = "\n".join(lines)
        if len(brief) > 2000:
            brief = brief[:2000] + "\n…(truncated)\n"
        return brief

    # ──────────────────────────────────────────────────────────────
    # `<vault>/hot.md` — periodic snapshot of cockpit live state
    # ──────────────────────────────────────────────────────────────
    # ──────────────────────────────────────────────────────────────
    # session snapshot — restore teammate panes across cockpit restarts
    # ──────────────────────────────────────────────────────────────
    def snapshot_state(self) -> dict:
        """Return a JSON-serialisable picture of every live teammate pane
        across every project. Lead panes are excluded because the tab
        restore in main_window (driven by `open_tabs` in projects.json)
        already brings Lead back. We only capture panes that are actively
        running and in a state worth resuming (active/working) — empty,
        exited, or error panes are intentionally skipped so a crashed
        run doesn't get re-spawned into the same crash.
        """
        projects: dict[str, list[dict]] = {}
        for project, panes in self._panes_by_project.items():
            entries: list[dict] = []
            for role, pane in panes.items():
                if role == LEAD.name:
                    continue
                if pane.session is None or not pane.session.is_alive:
                    continue
                if pane.state not in ("active", "working"):
                    continue
                # #9: persist last_task + session_uuid so restore_teammates
                # can re-paste the task and (optionally) resume the session.
                ps_snap = getattr(self, "_pane_state", {}).get(_exit_key(project, role))
                # #410: also persist the isolated-worktree / shared-tree
                # assign-time baseline. Without this, a cockpit restart
                # between assign() and done() strands a pane's merge-
                # proposal identity in memory only — done() afterwards sees
                # a blank PaneState and reports "ตรวจไม่ได้ (snapshot ตอน
                # assign ไม่ครบ)" with no merge proposal, even though the
                # branch really does carry commits.
                assign_dirty_snapshot = ps_snap.assign_dirty_snapshot if ps_snap else None
                entries.append(
                    {
                        "role": role,
                        "cwd": pane._session_cwd or "",
                        "state": pane.state,
                        "last_task": ((ps_snap.last_assigned_task if ps_snap else None) or ""),
                        "session_uuid": ((ps_snap.session_uuid if ps_snap else None) or ""),
                        "worktree": (ps_snap.worktree if ps_snap else None),
                        "assign_base_sha": (ps_snap.assign_base_sha if ps_snap else None),
                        "assign_git_root": (ps_snap.assign_git_root if ps_snap else None),
                        "assign_dirty_snapshot": (
                            {k: list(v) for k, v in assign_dirty_snapshot.items()}
                            if assign_dirty_snapshot is not None
                            else None
                        ),
                    }
                )
            if entries:
                projects[project] = entries
        return {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "projects": projects,
        }

    def write_session_snapshot(self) -> None:
        """Persist the current snapshot to disk. Best-effort: any error
        is swallowed so a disk hiccup never bubbles out of closeEvent or
        the periodic save timer."""
        try:
            ensure_runtime()
            _write_json_atomic(_LAST_SESSION_FILE, self.snapshot_state())
        except OSError:
            pass

    def restore_teammates(self) -> int:
        """Read the snapshot and re-spawn the recorded teammate panes.
        Returns the number of panes scheduled to spawn (caller can show
        a status-bar hint). Skips silently when the snapshot is missing,
        unparseable, or older than `_LAST_SESSION_MAX_AGE_SEC`.

        The ``exit_ts`` field is stamped for crash-recovery bookkeeping,
        but since ``session_uuid`` has no value for these roles yet, each
        spawn here generates a fresh ``--session-id`` (no bleed from a prior
        cockpit run's sessions).
        """
        if not _LAST_SESSION_FILE.is_file():
            return 0
        try:
            snap = json.loads(_LAST_SESSION_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        saved_at = snap.get("saved_at") or ""
        try:
            age = (datetime.now() - datetime.fromisoformat(saved_at)).total_seconds()
        except ValueError:
            return 0
        if age > _LAST_SESSION_MAX_AGE_SEC:
            return 0
        # #232: attribute WHY this boot is restoring teammates at all — a
        # bare "[cockpit restart]" notice didn't distinguish an auto-applied
        # npm update from a user hitting the restart button, so the reason
        # was invisible to Lead unless someone went digging in boot.log.
        restart_reason_suffix = _restart_reason_suffix(_read_and_clear_restart_reason())
        scheduled = 0
        for project, entries in (snap.get("projects") or {}).items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                role = (entry or {}).get("role")
                cwd = (entry or {}).get("cwd") or None
                last_task = (entry or {}).get("last_task") or ""
                if not role:
                    continue
                # Stamp recent-exit for crash-recovery bookkeeping.
                # session_uuid has no value yet for these roles, so
                # spawn() will issue --session-id (fresh session, no bleed).
                self._recent_exits[_exit_key(project, role)] = {"cwd": cwd, "ts": time.time()}
                ok, _ = self.spawn(role, cwd=cwd, project=project)
                if ok:
                    scheduled += 1
                    # #410: restore the isolated-worktree / shared-tree
                    # assign-time baseline saved by snapshot_state() — spawn()
                    # never touches these fields (its fresh-spawn-clear block
                    # only resets delivery/degrade bookkeeping), so it's safe
                    # to set them right after. Without this, a pane resumed
                    # after a restart looks exactly like a brand-new pane with
                    # no assignment on record to done(), which then can't
                    # produce a merge proposal or a "files touched" fact at
                    # all for whatever it finishes here.
                    _wt_restore = (entry or {}).get("worktree")
                    _base_sha_restore = (entry or {}).get("assign_base_sha")
                    _git_root_restore = (entry or {}).get("assign_git_root")
                    _dirty_snap_restore = (entry or {}).get("assign_dirty_snapshot")
                    if _wt_restore or _base_sha_restore or _git_root_restore:
                        _ps_restore = self._ps(_exit_key(project, role))
                        if _wt_restore:
                            _ps_restore.worktree = _wt_restore
                        if _base_sha_restore:
                            _ps_restore.assign_base_sha = _base_sha_restore
                        if _git_root_restore:
                            _ps_restore.assign_git_root = _git_root_restore
                        if isinstance(_dirty_snap_restore, dict):
                            _ps_restore.assign_dirty_snapshot = {
                                k: tuple(v) for k, v in _dirty_snap_restore.items()
                            }
                    # #9: re-paste the last task so the pane continues working;
                    # queue a Lead notice (delivered when Lead spawns) either
                    # way so the operator knows the pane was re-spawned.
                    #
                    # #230: a pane can legitimately finish (`done()` pops the
                    # ledger's `open[role]` row synchronously) in the window
                    # between this snapshot being written and an ABRUPT restart
                    # (crash, forced kill, or any restart path that doesn't run
                    # through the two graceful write_session_snapshot() call
                    # sites) — the on-disk snapshot then still shows the
                    # already-finished task, and re-sending it silently re-runs
                    # completed work (best case a no-op re-`done`, worst case a
                    # repeated migration/push). The task ledger's `open` map is
                    # the one durable, cross-process signal for "is this task
                    # still actually outstanding" — an absent entry (already
                    # resolved, or never tracked) means "safe to skip", which
                    # also fails closed for ledger-write hiccups: burning one
                    # skipped resend beats duplicating side-effecting work.
                    task_still_open = False
                    if last_task:
                        try:
                            from .task_ledger import load_state as _load_ledger_state

                            task_still_open = role in (
                                _load_ledger_state(project).get("open") or {}
                            )
                        except Exception:
                            task_still_open = True
                    if last_task and task_still_open:
                        self._send_when_ready(role, last_task, project=project)
                        notice_body = (
                            f"[cockpit restart{restart_reason_suffix}] {role} pane restored "
                            f"from last session and last task re-sent automatically."
                        )
                    elif last_task:
                        notice_body = (
                            f"⚠️ [cockpit restart{restart_reason_suffix}] {role} pane restored "
                            f"from last session — its last task has no open row in the task "
                            f"ledger (already completed, or was never tracked), so it was "
                            f"NOT re-sent automatically to avoid duplicate/side-effect work. "
                            f"Re-assign manually if it still needs to run."
                        )
                        _log_event("teammate_restore_resend_skipped", role=role, project=project)
                    elif role == "shell":
                        # A plain PowerShell pane never carries an assigned task —
                        # restoring it fresh is its normal state, so no ⚠️/re-assign
                        # noise (field report: scary warning on every restart).
                        notice_body = (
                            f"[cockpit restart{restart_reason_suffix}] shell pane restored "
                            f"from last session."
                        )
                    else:
                        notice_body = (
                            f"⚠️ [cockpit restart{restart_reason_suffix}] {role} pane restored "
                            f"from last session but last task was not saved — pane started "
                            f"fresh. Re-assign if needed."
                        )
                    self._pending_done_notices.setdefault(project, []).append(
                        {"role": role, "note": "restore", "body": notice_body}
                    )
                    self._save_pending_done_notices(project)
                    _log_event(
                        "teammate_restored",
                        role=role,
                        project=project,
                        has_task=bool(last_task),
                    )
        return scheduled

    def write_resume_briefs(self) -> int:
        """For every project currently open in cockpit, write a
        Markdown "resume brief" capturing the last ~20 conversation
        exchanges to `<vault>/07-AI-Command-Center/briefs/<project>-
        <YYYY-MM-DD>T<HHMMSS>.md`. Called from MainWindow.closeEvent
        so the next launch's Lead can read the brief and recover
        context without scrolling the pane history.

        Returns the number of briefs written. 0 when no vault is
        configured or no open project had conversation records to
        summarise.

        Issue #194: `_restart_cockpit()` calls this explicitly right before
        `QCoreApplication.quit()`, and that same quit() triggers
        `MainWindow.closeEvent`, which calls it AGAIN moments later — two
        full chatlog-scan passes back to back on the Qt main thread for
        data the first call just wrote. Throttled so the second call is a
        no-op instead of a second multi-second stall during shutdown.
        """
        now = time.time()
        if now - getattr(self, "_last_resume_brief_ts", 0.0) < _RESUME_BRIEF_MIN_INTERVAL_S:
            return 0
        self._last_resume_brief_ts = now
        vault = _resolve_vault_dir()
        if vault is None:
            return 0
        try:
            from .chatlog_scanner import build_resume_brief
        except Exception:
            return 0
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%dT%H%M%S")
        briefs_dir = vault / "99-Logs" / "briefs"
        # Cap the scan window so a long-dormant project doesn't drag
        # months of jsonls into the brief — last 24 h is plenty for
        # "where did we leave off."
        from datetime import timedelta

        since = now - timedelta(hours=24)
        written = 0
        for project in self._panes_by_project.keys():
            try:
                safe_project = validate_name(project, "project")
            except ValueError:
                import logging

                logging.getLogger(__name__).warning(
                    "write_resume_briefs: rejected unsafe project name %r", project
                )
                continue
            body = build_resume_brief(project_filter=safe_project, since=since)
            if not body:
                continue
            try:
                briefs_dir.mkdir(parents=True, exist_ok=True)
                (briefs_dir / f"{safe_project}-{stamp}.md").write_text(body, encoding="utf-8")
                written += 1
            except OSError:
                continue
        # Prune stale log files and ensure graph filter is set — best-effort.
        try:
            prune_vault_logs(vault)
        except Exception:
            pass
        try:
            write_obsidian_graph_filter(vault)
        except Exception:
            pass
        return written

    def write_daily_digest(self, project: str) -> bool:
        """Append a Finish-Job digest for `project` to today's daily
        note in the configured Obsidian vault.

        Daily note path is `<vault>/05-Daily/<YYYY-MM-DD>.md`. If the
        file already exists (another project's Finish Job earlier the
        same day, or hand-written entries), the digest is appended at
        the end. Otherwise a fresh file is created with a top-level
        title.

        Returns True on success, False when no vault is configured or
        an IO error swallows the write. Caller can surface a status
        bar message based on the return value.
        """
        vault = _resolve_vault_dir()
        if vault is None:
            return False
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        sessions_dir = RUNTIME_DIR / "sessions" / today / project
        sessions: list[tuple[str, str, str]] = []
        if sessions_dir.is_dir():
            for path in sorted(sessions_dir.glob("*.md"), reverse=True):
                stem = path.stem  # "<role>-<HHMMSS>"
                if "-" not in stem:
                    continue
                role, stamp = stem.rsplit("-", 1)
                try:
                    body = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                # `_render_decision_note` writes "## Note\n\n<text>" — pull
                # the first non-empty line after the header.
                note = ""
                marker = "## Note"
                idx = body.find(marker)
                if idx >= 0:
                    tail = body[idx + len(marker) :].strip()
                    note = tail.splitlines()[0] if tail else ""
                sessions.append((stamp, role, note))
        # Decisions today — assistant H2-headed messages from this
        # project's claude session jsonls. Best-effort: any scan
        # error degrades to no decisions section.
        try:
            from .chatlog_scanner import extract_decisions

            start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            decisions = extract_decisions(project_filter=project, since=start_of_today, limit=10)
        except Exception:
            decisions = []
        section = _render_daily_digest(project, now, sessions, decisions=decisions)

        daily_dir = vault / "05-Daily"
        try:
            daily_dir.mkdir(parents=True, exist_ok=True)
            daily_path = daily_dir / f"{today}.md"
            if daily_path.is_file():
                existing = daily_path.read_text(encoding="utf-8")
                if not existing.endswith("\n"):
                    existing += "\n"
                daily_path.write_text(existing + "\n" + section, encoding="utf-8")
            else:
                header = f"# Daily — {today}\n\n"
                daily_path.write_text(header + section, encoding="utf-8")
        except OSError:
            return False
        return True

    def _write_hot_md(self) -> None:
        """Rewrite `<vault>/hot.md` from the current pane registry plus
        the in-memory ring of recent `takkub done` events. Skipped
        silently when no vault is configured. Best-effort: swallow
        OSError so a vault permission glitch never bubbles out of a
        QTimer tick and kills the orchestrator."""
        vault = _resolve_vault_dir()
        if vault is None:
            return
        # Snapshot live state on the main thread (cheap) — the heavy session-file
        # scan + render + write run off-thread so a large chatlog or slow vault
        # never blocks the Qt event loop (was a proven main_thread_stall source:
        # _write_hot_md → scan_hot_md_metrics → stat over every session file,
        # fired on EVERY `done` event + the 60 s timer).
        snapshot = {
            project: {role: pane.state for role, pane in panes.items()}
            for project, panes in self._panes_by_project.items()
        }
        try:
            active_name, _ = active_project()
        except Exception:
            active_name = None
        recent = list(self._recent_done)
        now = datetime.now()
        # Coalesce bursts: if the previous off-thread write is still running,
        # skip this tick — the next one picks up fresh state. Prevents a thread
        # pile-up when `done` events arrive back-to-back.
        if getattr(self, "_hot_md_writing", False):
            return
        self._hot_md_writing = True

        def _hot_md_worker() -> None:
            try:
                # Hook noise meter + friction heatmap — single pass over today's
                # Claude Code session jsonl files. Per-file (mtime, size) cache in
                # scan_hot_md_metrics avoids re-parsing unchanged files.
                try:
                    from .chatlog_scanner import scan_hot_md_metrics

                    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    hook_counts, _corrections, _tool_retries = scan_hot_md_metrics(
                        since=start_of_today
                    )
                    friction = {"corrections": _corrections, "tool_retries": _tool_retries}
                except Exception:
                    hook_counts = {}
                    friction = {}
                body = _render_hot_md(
                    snapshot,
                    active_name,
                    recent,
                    now,
                    hook_counts=hook_counts,
                    friction=friction,
                )
                try:
                    (vault / "hot.md").write_text(body, encoding="utf-8")
                except OSError:
                    pass
            finally:
                self._hot_md_writing = False

        threading.Thread(target=_hot_md_worker, daemon=True, name="hot-md-writer").start()

    # ──────────────────────────────────────────────────────────────
    # idle watchdog — surface teammates that forgot to `takkub done`
    # ──────────────────────────────────────────────────────────────
    def _check_idle_teammates(self) -> None:
        """Surface a `takkub done` reminder for any teammate pane that's been
        at the ready prompt for IDLE_REMIND_AFTER_S while still flagged
        'working'. Normal rounds are cockpit UI signals and do not wake the
        model; one PTY escalation is allowed after the configured number of
        continuous-idle rounds. Lead is exempt — only Lead is allowed to
        orchestrate, and Lead never calls `done` on itself.

        Scans every open project so a teammate in a background tab still
        gets nudged. Idle-state keys are namespaced `<project>::<role>`
        to keep two projects' state from colliding."""
        now = time.time()
        # Stuck-pane detection rides the same 5 s tick so we don't pay
        # for another QTimer. Runs before the idle-reminder logic so a
        # recover (which closes the pane) doesn't fight with reminder
        # injection on the same pane.
        self._check_stuck_panes(now)
        # #308: independent stuck-tool watchdog — see its own docstring for
        # why this can't be folded into `_check_stuck_panes` above (that
        # detector's content-hash clock has the SAME false-idle blind spot
        # the #308 incident exposed, since a provider's idle footer can sit
        # right below the hung tool-call line the whole time). Runs before
        # the per-pane idle-reminder loop below so a fresh escalation this
        # tick is visible to the `tool_stuck_escalated` suppression check in
        # that loop on the very same pass.
        self._check_stuck_tool_panes(now)
        # Spawn FIFO queue escape hatch rides the same tick (#139/#140) — a
        # wedged arbiter is caught even with no new spawn() call arriving to
        # trip over it.
        self._check_spawn_queue_stuck(now)
        # Flush durable done-notices for any project whose Lead is now idle.
        # Handles the case where notices spilled to _pending_done_notices while
        # Lead was busy — delivers them without requiring a Lead restart.
        self._reap_pending_done_notices()
        # #277: re-deliver `takkub send` messages a respawn swallowed. Same
        # tick for the same reason as the line above — one sweep catches every
        # respawn path instead of each one remembering to replay.
        try:
            self._reap_role_messages()
        except Exception:
            _log_event("role_message_reap_error")
        # #255: sweep task deliveries stuck in-flight past their TTL and
        # tell Lead, instead of a stale delivery going quiet until it
        # resurfaces as a duplicate paste.
        self._reap_stale_deliveries()
        # Surface panes whose idle prompt no marker recognises (structural #20
        # staleness detector) — makes an upstream-reword silent break LOUD.
        self._check_stale_markers(now)
        # Proactive idle compaction (issue #161) — independent of the
        # forgot-`done` loop below (which only tracks pane.state=="working"):
        # this targets panes that ARE done (or Lead, which never reports
        # done) and have simply been sitting idle a long time.
        self._check_proactive_compact(now)
        for project_name, project_panes in list(self._panes_by_project.items()):
            for name, pane in list(project_panes.items()):
                try:
                    key = f"{project_name}::{name}"
                    if name == LEAD.name:
                        # Issue #59: malformed-tool-call detection covers Lead too even
                        # though Lead is exempt from the idle-done-reminder loop.
                        if (
                            pane.session
                            and pane.session.is_alive
                            and pane.session.is_at_ready_prompt()
                        ):
                            self._maybe_surface_malformed_xml(key, name, project_name, pane, now)
                        continue
                    if pane.state != "working":
                        self._idle_state.pop(key, None)
                        continue
                    if pane.session is None or not pane.session.is_alive:
                        self._idle_state.pop(key, None)
                        continue

                    # Mid-task graft staleness gap: boot/tab-switch/done are the
                    # only 3 triggers that ever touch a directory's staging
                    # mirror or graph (graft_autobuild.py module docstring), so a
                    # pane querying graft about a file it is CURRENTLY editing —
                    # before its own `done()` — got an answer as of its last
                    # done() (or nothing, on a pane's first task). Riding this
                    # same 5s tick costs nothing new and resync_staging_only()
                    # self-throttles per directory, so this is a cheap no-op most
                    # ticks. Worktree-isolated panes are skipped: graft is never
                    # granted there (M3, shared_dev_tools.py), so resyncing that
                    # throwaway checkout's mirror would be pure waste.
                    _ps_wt = getattr(self, "_pane_state", {}).get(key)
                    if _ps_wt is None or not _ps_wt.worktree:
                        try:
                            from .graft_autobuild import resync_staging_only

                            resync_staging_only(getattr(pane, "_session_cwd", None))
                        except Exception:
                            pass

                    # Stuck-paste reaper — MUST run before every suppression
                    # gate below (rate-limit, blocked-on-lead, tty-block). A
                    # swallowed task submit has to be recovered even when a
                    # gate silences the idle reminder: the 2026-07-02 QA
                    # fan-out incident was exactly this — a false rate-limit
                    # flag (Fable-5 promo text) suppressed the reminder whose
                    # trailing Enter used to rescue stuck pastes by accident,
                    # so panes sat on "[Pasted text +N lines]" for hours.
                    self._maybe_submit_stuck_paste(key, name, project_name, pane, now)

                    # Suppress the reminder while this pane is rate-limited: it's
                    # not idle-because-done, it physically can't work until the
                    # usage limit resets. Detection happens here (every tick) and
                    # schedules a one-shot reset notice the first time it's seen.
                    if self._rate_limit_suppressed(project_name, name, pane, now):
                        self._idle_state.pop(key, None)
                        # Limit-aware auto-resume (🌙): inert unless the user
                        # opted in via the status-bar toggle — see
                        # limit_autoresume.py for the confirm/park/wake flow.
                        self._maybe_auto_resume_park(project_name, name, pane, now)
                        continue

                    # Suppress the reminder while this teammate is waiting on a
                    # reply from Lead — they're not "stuck on `takkub done`",
                    # they're genuinely blocked on clarification. The flag is
                    # set in `send()` when the teammate runs
                    # `takkub send --to lead "..."` and cleared when Lead
                    # sends back. We also expire the suppression after 30
                    # minutes so a Lead that crashed mid-reply doesn't leave
                    # the teammate's watchdog disabled forever.
                    _ps_bl = getattr(self, "_pane_state", {}).get(key)
                    blocked_at = _ps_bl.blocked_on_lead_ts if _ps_bl is not None else None
                    if blocked_at is not None and (now - blocked_at) < 30 * 60:
                        entry = self._idle_state.get(key)
                        if entry:
                            entry["first_idle_ts"] = None
                            entry["last_reminder_ts"] = 0.0
                            entry["notice_rounds"] = 0
                            entry["escalated"] = False
                        continue

                    entry = self._idle_state.setdefault(
                        key,
                        {
                            "first_idle_ts": None,
                            "last_reminder_ts": 0.0,
                            "seen_working": False,
                            "notice_rounds": 0,
                            "escalated": False,
                        },
                    )

                    # Issue #54: suppress the forgot-done reminder while a pane is
                    # blocked on an interactive subprocess prompt (y/N, passphrase,
                    # "press any key"). The idle reminder is the wrong context here
                    # and close→respawn (stuck recover) won't help — the prompt comes
                    # from the subprocess. Surface a separate notice to Lead instead.
                    # #236: also check Claude Code's own numbered tool-permission
                    # menu (no [y/N] bracket, so is_blocked_on_tty_prompt() alone
                    # misses it — a pane stuck there used to silently read as
                    # ordinary busy generation forever).
                    _perm_prompt = pane.session.is_blocked_on_permission_prompt()
                    _tty_prompt = _perm_prompt or pane.session.is_blocked_on_tty_prompt()
                    if _tty_prompt:
                        entry["first_idle_ts"] = None
                        entry["last_reminder_ts"] = 0.0
                        entry["notice_rounds"] = 0
                        entry["escalated"] = False
                        self._maybe_surface_tty_block(
                            key,
                            name,
                            project_name,
                            _tty_prompt,
                            now,
                            kind="permission" if _perm_prompt else "tty",
                        )
                        continue
                    # Clear block state when no longer blocked.
                    _ps_tty = getattr(self, "_pane_state", {}).get(key)
                    if _ps_tty is not None and _ps_tty.tty_blocked_since is not None:
                        _ps_tty.tty_blocked_since = None

                    # #308: suppress the forgot-done reminder while
                    # `_check_stuck_tool_panes` (above, same tick) has this
                    # pane flagged stuck in a shell/tool call — that watchdog
                    # already escalated to Lead and attempted its one-shot Esc
                    # recovery, so nagging "run `takkub done`" on top is noise
                    # a pane genuinely wedged in a tool call cannot act on.
                    # Deliberately checked BEFORE the `is_at_ready_prompt()`
                    # gate below: #308's own evidence is a pane whose idle
                    # footer stayed visible the whole time it was stuck, so
                    # that gate alone would never have caught this case.
                    _ps_ts = getattr(self, "_pane_state", {}).get(key)
                    if _ps_ts is not None and _ps_ts.tool_stuck_escalated:
                        entry["first_idle_ts"] = None
                        entry["last_reminder_ts"] = 0.0
                        entry["notice_rounds"] = 0
                        entry["escalated"] = False
                        continue

                    if not pane.session.is_at_ready_prompt():
                        # claude is processing — reset the idle streak so a long
                        # build doesn't count toward the reminder threshold.
                        # Latch that the pane entered a GENUINE work turn (not a
                        # codex/agy MCP-boot or queued-message phase — those read
                        # busy too but aren't the task running). The forgot-done
                        # reminder only arms once this is set, so a booting/queued
                        # pane no longer collects reminders it can't act on
                        # (2026-07-21 codex boot-window noise). A pane cannot
                        # finish its task without a real work turn, so a genuine
                        # forgot-`takkub done` still reminds.
                        if not pane.session.shows_startup_marker():
                            entry["seen_working"] = True
                        entry["first_idle_ts"] = None
                        entry["last_reminder_ts"] = 0.0
                        entry["notice_rounds"] = 0
                        entry["escalated"] = False
                        continue

                    # Still in a provider startup / message-queue phase (idle
                    # status bar but task not yet consumed) — not a finished
                    # task. Reset the streak and wait for the real turn.
                    if pane.session.shows_startup_marker():
                        entry["first_idle_ts"] = None
                        entry["last_reminder_ts"] = 0.0
                        entry["notice_rounds"] = 0
                        entry["escalated"] = False
                        continue

                    # #391/#394/#395/#398: the pane reads READY (composer
                    # accepts input, correctly per d047ab4) but may still be
                    # babysitting a long background command (docker build,
                    # node build, turbo/vitest, pio run, ...) — that is not
                    # "idle-because-done". Three independent signals, any one
                    # of which means genuinely active: claude's own
                    # background-task footer segment, a provider tool-running
                    # marker (#308, works for codex/gemini too), or the same
                    # content-delta progress clock `takkub status` reports
                    # ("last progress Ns ago") having moved recently — this
                    # last one is what actually caught the reported cases,
                    # where progress showed 5-6s ago while the naive
                    # is_at_ready_prompt()-only watchdog above still thought
                    # the pane had sat idle for 10+ minutes.
                    # #404 review: use the same resolved-provider lookup as
                    # `_check_stuck_tool_panes` (#308) rather than the raw
                    # `pane.model.provider_name` attribute — a role mapped to
                    # codex/gemini/opencode via provider routing needs ITS
                    # OWN `tool_running_marker`, not claude's, or the marker
                    # lookup below silently checks the wrong provider's
                    # pattern and never fires for those roles.
                    from .provider_config import effective_provider_for

                    try:
                        _provider_bg = effective_provider_for(name, project=project_name)
                    except Exception:
                        _provider_bg = getattr(pane.model, "provider_name", None) or "claude"
                    try:
                        _has_bg_work = pane.session.has_background_work()
                    except Exception:
                        _has_bg_work = False
                    # Same isinstance guard as _check_stuck_tool_panes/
                    # _apply_screen_scrape_state (#376): a loosely mocked test
                    # session's un-stubbed attribute call returns a truthy
                    # MagicMock, not a real bool/str/float.
                    if not isinstance(_has_bg_work, bool):
                        _has_bg_work = False
                    try:
                        _tool_marker = pane.session.tool_running_marker(_provider_bg)
                    except Exception:
                        _tool_marker = None
                    if not isinstance(_tool_marker, str) or not _tool_marker:
                        _tool_marker = None
                    _last_progress_ts = self._compute_last_progress_ts(name, project_name, pane)
                    _progress_recent = bool(
                        _last_progress_ts and (now - _last_progress_ts) < STALL_THRESHOLD_SEC
                    )
                    if _has_bg_work or _tool_marker is not None or _progress_recent:
                        entry["seen_working"] = True
                        entry["first_idle_ts"] = None
                        entry["last_reminder_ts"] = 0.0
                        entry["notice_rounds"] = 0
                        entry["escalated"] = False
                        continue

                    # Issue #59: pane is idle — check for malformed tool-call XML
                    # that the harness silently no-op'd (makes pane look hung).
                    # Defense-in-depth: this best-effort nudge sits in front of the
                    # critical forgot-`takkub done` reminder below. A bug in the
                    # detector (e.g. the pyte empty-stub IndexError, now fixed at
                    # source) must never starve the reminder — otherwise a teammate
                    # that forgot to report sits idle until the user closes it,
                    # never reaching Lead. Isolate it so the reminder always runs.
                    try:
                        self._maybe_surface_done_typed_as_text(key, name, project_name, pane, now)
                    except Exception as _dt_err:
                        _log_event(
                            "done_text_check_error",
                            role=name,
                            project=project_name,
                            err=f"{type(_dt_err).__name__}: {_dt_err}",
                        )
                    try:
                        self._maybe_surface_malformed_xml(key, name, project_name, pane, now)
                    except Exception as _mx_err:
                        _log_event(
                            "malformed_xml_check_error",
                            role=name,
                            project=project_name,
                            err=f"{type(_mx_err).__name__}: {_mx_err}",
                        )

                    if entry["first_idle_ts"] is None:
                        entry["first_idle_ts"] = now
                        continue

                    idle_for = now - entry["first_idle_ts"]
                    since_last_reminder = now - entry["last_reminder_ts"]
                    # Armed once we've seen a real work turn, OR — for a task
                    # that began and ended between two ticks so the latch never
                    # flipped — once the pane has been idle well past any
                    # provider boot/queue window (see the constant's comment).
                    armed = entry.get("seen_working") or idle_for >= IDLE_REMIND_UNLATCHED_AFTER_S
                    if (
                        armed
                        and idle_for >= IDLE_REMIND_AFTER_S
                        and since_last_reminder >= IDLE_REMIND_COOLDOWN_S
                    ):
                        notice_round = int(entry.get("notice_rounds") or 0) + 1
                        escalate = bool(
                            IDLE_REMIND_ESCALATE_AFTER_ROUNDS > 0
                            and notice_round > IDLE_REMIND_ESCALATE_AFTER_ROUNDS
                            and not entry.get("escalated")
                        )
                        self._inject_idle_reminder(
                            project_name,
                            name,
                            pane,
                            notice_round,
                            escalate=escalate,
                        )
                        entry["notice_rounds"] = notice_round
                        if escalate:
                            entry["escalated"] = True
                        entry["last_reminder_ts"] = now
                        # Keep first_idle_ts anchored to the start of the
                        # continuous idle episode. Cooldown controls reminder
                        # frequency; retaining the original timestamp also
                        # preserves HARVEST_HINT_SEC semantics.

                    # Harvest hint: if the pane has been idle much longer than
                    # the reminder threshold, suggest `takkub harvest` to Lead.
                    if HARVEST_HINT_SEC > 0 and idle_for >= HARVEST_HINT_SEC:
                        _ps_hh = getattr(self, "_pane_state", {}).get(key)
                        last_hint = _ps_hh.harvest_hint_ts if _ps_hh is not None else 0.0
                        if now - last_hint >= HARVEST_HINT_SEC:
                            lead_pane = project_panes.get(LEAD.name)
                            if lead_pane and lead_pane.session and lead_pane.session.is_alive:
                                hint_min = HARVEST_HINT_SEC // 60
                                # #391/#398: attach the same evidence `takkub
                                # status` would show so Lead can judge on the
                                # spot instead of tabbing over to check —
                                # this notice already survived the
                                # background-work/tool-marker/progress-recency
                                # gate above, so "ready=True" here means
                                # "genuinely idle at the ready prompt", not
                                # just "composer accepts input".
                                try:
                                    _out_age = pane.session.seconds_since_output()
                                except Exception:
                                    _out_age = float("inf")
                                if not isinstance(_out_age, (int, float)):
                                    _out_age = float("inf")
                                _out_age_str = (
                                    "?" if _out_age == float("inf") else str(round(_out_age))
                                )
                                _prog_age = (now - _last_progress_ts) if _last_progress_ts else None
                                _prog_age_str = "?" if _prog_age is None else str(round(_prog_age))
                                hint_msg = (
                                    f"[cockpit] {name} ไม่ active >{hint_min}m. "
                                    f"ลอง: takkub harvest --role {name} "
                                    f"(last output {_out_age_str}s ago, last progress "
                                    f"{_prog_age_str}s ago, ready=True)"
                                )
                                self._notify_lead(project_name, hint_msg, kind="harvest-hint")
                                _log_event("harvest_hint", role=name, project=project_name)
                                self._ps(key).harvest_hint_ts = now
                except Exception as e:
                    # This block runs every 5s per pane; a persistent fault used
                    # to re-log a bare role/project with NO exception detail on
                    # every tick (3279 blind entries in one events.log — zero
                    # diagnostic value). Capture the exception type+message and
                    # rate-limit: log only when the error changes or after a
                    # 5-min cooldown per pane, so the real cause surfaces once
                    # instead of flooding the log.
                    err = f"{type(e).__name__}: {e}"
                    last = self._idle_err_last.get(key)
                    if last is None or last[0] != err or (now - last[1]) >= 300:
                        _log_event(
                            "idle_watchdog_pane_error",
                            role=name,
                            project=project_name,
                            err=err,
                        )
                        self._idle_err_last[key] = (err, now)

    def _team_idle_since_for(
        self, project_name: str, project_panes: dict[str, AgentPane], now: float
    ) -> float:
        """(#412) Wall-clock time since every non-Lead pane in *project_name*
        stopped being `working`, or 0.0 while at least one still is (or none
        exist at all).

        No teammates in the project is deliberately NOT the same as "every
        role done" — a Lead with nobody ever assigned yet has no team-done
        event behind it, so it gets no exemption (matches the pre-existing
        #343 escalation tests, which use a lone "lead"-only pane map on
        purpose to prove Lead itself can still be paged when genuinely
        wedged). The exemption only kicks in once at least one teammate has
        actually existed and none is currently working.

        Deliberately reads `pane.state` directly rather than any ready-marker
        screen scrape — the whole point is a signal that works identically
        for every provider, calibrated or not, since it never looks at PTY
        text. Recomputed fresh each call instead of updated by `done()`/
        `assign()` so a pane that closes/exits mid-task (never calling
        `done()`) still correctly counts as "not working" the very next tick,
        with no separate teardown path to keep in sync.
        """
        teammates = [p for role, p in project_panes.items() if role != LEAD.name]
        if not teammates:
            self._team_idle_since.pop(project_name, None)
            return 0.0
        if any(getattr(p, "state", None) == "working" for p in teammates):
            self._team_idle_since.pop(project_name, None)
            return 0.0
        return self._team_idle_since.setdefault(project_name, now)

    def _check_stale_markers(self, now: float) -> None:
        """Surface a pane whose idle prompt no state marker recognises — the
        silent-break signature of #20 (an upstream CLI reworded its prompt so
        is_at_ready_prompt no longer matches).

        Structural gate: only consider a pane that has been output-QUIET for
        STALE_MARKER_QUIET_S. A generating CLI streams continuously, so a long
        silence means the pane is genuinely settled, not mid-generation — a
        signal independent of the (fragile) text markers. If a settled pane is
        recognised by NO marker (not ready, not a known tty/trust/splash
        prompt), detection has gone blind; log it (rate-limited per pane) with
        the bottom screen text so the operator sees the real footer and can
        rescue it via TAKKUB_EXTRA_READY_MARKERS — a loud diagnostic instead of
        a silent idle-watchdog stall.

        (#343) A pane that stays continuously unrecognised across multiple
        cooldown windows in a row gets an auto-recovery probe past this
        routine 🟡 line — see _STALE_MARKER_ESCALATE_EVERY,
        _nudge_stale_marker, and _resolve_stale_marker_nudge. This does NOT
        touch the marker table itself: guessing at a marker fix here would
        risk misclassifying a real provider crash as "ready", masking the
        one signal that currently surfaces it at all.

        A key with a probe in flight (`self._stale_marker_nudged`) is
        resolved FIRST, before any of the ordinary quiet/marker checks below
        — see _resolve_stale_marker_nudge's docstring for why phase 2 must
        run unconditionally on the very next sweep tick after the nudge, not
        whenever this pane next happens to pass the quiet/cooldown gate.

        (#412) Lead is NOT skipped by the loop below (unlike the
        forgot-`done` reminder in `_check_idle_teammates`) — a Lead whose
        provider has no ready-prompt marker covering every genuinely-idle
        shape would otherwise get escalated as "provider ค้าง/ล่ม" while
        simply waiting for the human's next instruction. `_team_idle_since_for`
        grants Lead a LEAD_DONE_IDLE_GRACE_S exemption while every teammate in
        its project is non-working, checked right after the alive check so it
        also cancels any nudge already in flight.
        """
        for project_name, project_panes in list(self._panes_by_project.items()):
            for name, pane in list(project_panes.items()):
                key = f"{project_name}::{name}"
                try:
                    sess = pane.session
                    if sess is None or not sess.is_alive:
                        self._stale_marker_streak.pop(key, None)
                        self._stale_marker_nudged.pop(key, None)
                        continue
                    if name == LEAD.name:
                        idle_since = self._team_idle_since_for(project_name, project_panes, now)
                        if idle_since and (now - idle_since) < LEAD_DONE_IDLE_GRACE_S:
                            self._stale_marker_streak.pop(key, None)
                            self._stale_marker_nudged.pop(key, None)
                            continue
                    if key in self._stale_marker_nudged:
                        self._resolve_stale_marker_nudge(project_name, name, pane, sess, key, now)
                        continue
                    if sess.seconds_since_output() < STALE_MARKER_QUIET_S:
                        continue  # still streaming → genuinely busy, not blind
                    if sess.is_at_ready_prompt():
                        self._stale_marker_streak.pop(key, None)
                        continue  # recognised idle → markers working
                    if (
                        sess.is_blocked_on_tty_prompt()
                        or sess.is_at_trust_prompt()
                        or sess.is_blocked_on_permission_prompt()
                    ):
                        self._stale_marker_streak.pop(key, None)
                        continue  # recognised shell/trust/permission prompt
                    if sess.is_at_update_splash():
                        self._stale_marker_streak.pop(key, None)
                        continue  # recognised codex splash (handled elsewhere)
                    # Alive + settled + unrecognised → markers likely stale.
                    last = self._stale_marker_last.get(key)
                    if last is not None and (now - last) < STALE_MARKER_COOLDOWN_S:
                        continue
                    tail = _stale_marker_footer(sess)
                    quiet_s = round(sess.seconds_since_output())
                    _log_event(
                        "ready_marker_possibly_stale",
                        role=name,
                        project=project_name,
                        quiet_s=quiet_s,
                        footer=tail,
                    )
                    self._stale_marker_last[key] = now
                    streak = self._stale_marker_streak.get(key, 0) + 1
                    self._stale_marker_streak[key] = streak
                    if streak % _STALE_MARKER_ESCALATE_EVERY == 0:
                        self._nudge_stale_marker(project_name, name, sess, tail, quiet_s, streak)
                except Exception:
                    continue

    def _nudge_stale_marker(
        self,
        project_name: str,
        name: str,
        sess: PtySession,
        tail: str,
        quiet_s: int,
        streak: int,
    ) -> None:
        """(#343 follow-up) Phase 1 of the two-phase auto-recovery probe: fire
        a resize bump-and-restore (never a keystroke — see PtySession.resize()'s
        note; a keystroke can change a CLI's state, a resize can't, and this
        must stay safe across every provider, not just claude) to force a
        real redraw, record what the screen looked like BEFORE that redraw,
        and stop — deliberately WITHOUT reading the screen again yet.

        Why not read it here too: resize() applies pyte's synchronous
        screen.resize() and returns immediately — it does not, and cannot,
        wait for the child process to actually notice SIGWINCH/the ConPTY
        size event and repaint in response. Reading the screen in the same
        call, right after resize() returns, would just observe the SAME
        pre-redraw frame again (or, worse, a frame mid-repaint), which is
        not evidence of anything. The real post-redraw frame only exists
        after the event loop has run long enough for the child to react —
        i.e. on a LATER sweep tick. `self._stale_marker_nudged[key]` records
        the pre-redraw state so `_resolve_stale_marker_nudge` (phase 2, next
        tick) can compare against it once a real redraw has had a chance to
        land, and `_check_stale_markers` resolves that key unconditionally
        first thing on its very next pass.

        Best-effort: a failed resize/read here must never take the sweep
        down with it (matches this file's surrounding except-Exception-
        continue style) — phase 2 still runs next tick and, seeing no
        genuine recovery, falls through to the loud escalation.
        """
        provider = "unknown"
        try:
            from .provider_config import effective_provider_for

            provider = effective_provider_for(name, project=project_name)
        except Exception:
            pass

        # Compared against the literal rather than importing provider_config.CLAUDE
        # here too — that import already failing above is exactly the case where
        # `provider` stays "unknown", which correctly keeps this False either way.
        is_claude = provider == "claude"
        structural_before = False
        try:
            structural_before = is_claude and bool(sess.is_at_claude_empty_composer())
            orig_cols, orig_rows = sess.cols, sess.rows
            sess.resize(orig_cols + 1, orig_rows)
            sess.resize(orig_cols, orig_rows)
        except Exception:
            pass
        key = f"{project_name}::{name}"
        self._stale_marker_nudged[key] = {
            "provider": provider,
            "footer_before": tail,
            "structural_before": structural_before,
            "quiet_s": quiet_s,
            "streak": streak,
        }
        _log_event(
            "ready_marker_nudge",
            role=name,
            project=project_name,
            quiet_s=quiet_s,
            streak=streak,
            provider=provider,
            footer_before=tail,
            structural_before=structural_before,
        )

    def _resolve_stale_marker_nudge(
        self,
        project_name: str,
        name: str,
        pane: AgentPane,
        sess: PtySession,
        key: str,
        now: float,
    ) -> None:
        """(#343 follow-up) Phase 2 of the two-phase auto-recovery probe, run
        unconditionally on the sweep tick AFTER _nudge_stale_marker fired the
        resize — by now the event loop has had a full tick to let the child
        actually react to the resize and repaint, so reading the screen here
        reflects a genuine post-redraw frame instead of a same-tick echo of
        the pre-redraw one (see _nudge_stale_marker's docstring for why phase
        1 can't do this check itself).

        Two ways this counts as recovered, and either clears the streak
        without paging anyone:
          * `is_at_ready_prompt()` now matches — a marker just caught up
            (the pane may simply have finished the redraw mid-recognition).
          * claude-only structural fallback: the screen showed the SAME
            "empty bordered composer" shape (PtySession.is_at_claude_empty_composer())
            both immediately before the redraw (phase 1) AND now, after it —
            two independent glimpses of the exact same idle shape bracketing
            a real repaint is what rules out "caught mid-repaint" (a single
            glimpse could coincidentally show a bare composer for one frame).

        Anything else — non-claude, shape doesn't match, or doesn't survive
        the redraw — escalates loudly in THIS same round via
        _escalate_stale_marker, not after another _STALE_MARKER_ESCALATE_EVERY
        streak: a failed nudge must never make evidence of a genuinely wedged
        pane wait multiple cooldown windows longer to surface.

        Best-effort: a failed screen read here is treated as "not recovered"
        (falls through to the loud path) rather than raising, matching this
        file's surrounding except-Exception-continue style.
        """
        nudge = self._stale_marker_nudged.pop(key, None)
        if nudge is None:
            return
        footer_after = str(nudge["footer_before"])
        recognised_after = False
        structural_after = False
        try:
            recognised_after = bool(sess.is_at_ready_prompt())
            structural_after = nudge["provider"] == "claude" and bool(
                sess.is_at_claude_empty_composer()
            )
            footer_after = _stale_marker_footer(sess)
        except Exception:
            pass
        structural_recovered = bool(nudge["structural_before"]) and structural_after
        quiet_s = int(nudge["quiet_s"])
        streak = int(nudge["streak"])
        provider = str(nudge["provider"])
        if recognised_after or structural_recovered:
            _log_event(
                "ready_marker_nudge_recovered",
                role=name,
                project=project_name,
                quiet_s=quiet_s,
                streak=streak,
                provider=provider,
                reason="recognised" if recognised_after else "structural",
                footer_before=nudge["footer_before"],
                footer_after=footer_after,
            )
            self._stale_marker_streak.pop(key, None)
            return
        _log_event(
            "ready_marker_nudge_result",
            role=name,
            project=project_name,
            quiet_s=quiet_s,
            streak=streak,
            provider=provider,
            footer_before=nudge["footer_before"],
            footer_after=footer_after,
            recognised_after=recognised_after,
            structural_recovered=structural_recovered,
        )
        self._escalate_stale_marker(
            project_name, name, pane, sess, footer_after, quiet_s, streak, now
        )

    def _escalate_stale_marker(
        self,
        project_name: str,
        name: str,
        pane: AgentPane,
        sess: PtySession,
        tail: str,
        quiet_s: int,
        streak: int,
        now: float,
    ) -> None:
        """(#343) The loud path: a pane that stayed unrecognised-and-quiet
        across _STALE_MARKER_ESCALATE_EVERY occurrences AND did not recover
        across the phase-1/phase-2 resize-nudge probe (see
        _nudge_stale_marker / _resolve_stale_marker_nudge). Dumps full
        diagnostic state and makes sure a human actually finds out:

          * logs under a distinct event name maintenance.py classifies 🔴
            (`_SEVERE_EVENTS`), not 🟡 — the routine event stayed 🟡-only for
            the entire ~9h #343 episode, which is the gap this closes
          * pings Lead directly via _notify_lead — including when *this* pane
            IS Lead: that call still lands in the durable pending-notice
            fallback (see _notify_lead's docstring) when live delivery can't
            reach a genuinely wedged pane, so the diagnostic is waiting
            whenever that pane is next recovered/respawned rather than lost

        Best-effort end to end — a failed diagnostic dump must never take the
        sweep down with it (matches this file's surrounding
        except-Exception-continue style).
        """
        provider = "unknown"
        model: str | None = None
        try:
            from .provider_config import effective_provider_for

            provider = effective_provider_for(name, project=project_name)
            model = sess.current_model_label(provider)
        except Exception:
            pass

        last_progress_ts = 0.0
        try:
            last_progress_ts = self._compute_last_progress_ts(name, project_name, pane)
        except Exception:
            last_progress_ts = 0.0
        _log_event(
            "ready_marker_stale_prolonged",
            role=name,
            project=project_name,
            quiet_s=quiet_s,
            streak=streak,
            footer=tail,
            markers_checked=list(_STALE_MARKER_CHECKS_TRIED),
            provider=provider,
            model=model,
            last_progress_ts=last_progress_ts,
            last_progress_age_s=(round(now - last_progress_ts) if last_progress_ts else None),
        )
        try:
            self._notify_lead(
                project_name,
                f"[watchdog] {name} เงียบต่อเนื่อง {quiet_s}s (ครั้งที่ {streak} ที่ marker "
                f"จับไม่ได้เลยติดกัน) — อาจเป็น provider ค้าง/ล่มเงียบ, ไม่ใช่แค่ idle ปกติ "
                f"(#343). provider={provider} footer: {tail[:150] or '(ว่าง)'}",
                kind="ready-marker-stale",
            )
        except Exception:
            pass

    def _lead_orchestrate_busy_reason(
        self, project_ns: str, project_panes: dict[str, AgentPane]
    ) -> str | None:
        """#465: why Lead must not be proactively compacted right now, or
        `None` if the whole project is genuinely idle and Lead is free to.

        Lead sitting at its own ready prompt while a specialist is still
        working, a `takkub wait` registration is still open, or an inbox
        digest hasn't reached Lead's pane yet all look identical to genuine
        idle to the PTY-scrape check in `_check_proactive_compact` — but
        Lead is actively orchestrating in every one of those states, and
        compacting away the context it needs the moment that work resolves
        (the spec it assigned, what it reviewed, the merge plan) was the
        exact reported failure. Checked in this order only for a stable,
        single-cause `reason` string — all three can be true at once."""
        for other_role, other_pane in project_panes.items():
            if other_role == LEAD.name:
                continue
            if other_pane.state == "working":
                return "lead-specialist-working"
        if getattr(self, "_active_waits", {}).get(project_ns):
            return "lead-wait-pending"
        if (
            getattr(self, "_lead_digest_queue", {}).get(project_ns)
            or getattr(self, "_lead_notify_queue", {}).get(project_ns)
            or getattr(self, "_pending_done_notices", {}).get(project_ns)
        ):
            return "lead-inbox-unread"
        return None

    def _pane_waiting_for_lead(self, ps: PaneState | None, now: float) -> bool:
        """#465 item 2 / #463: True when `ps` is in the "waiting-lead" tier
        (see `_derive_display_state`'s docstring) — the pane's turn
        genuinely ended while it was blocked on Lead's reply, so it is not
        idle, it's blocked on a decision only Lead can make. Both a recent
        `blocked_on_lead_ts` stamp AND a `last_turn_end_ts` at least as new
        are required — `progress()` alone stamps `blocked_on_lead_ts`
        mid-turn too, before the turn has actually ended."""
        return bool(
            ps is not None
            and ps.blocked_on_lead_ts is not None
            and (now - ps.blocked_on_lead_ts) < 30 * 60
            and ps.last_turn_end_ts is not None
            and ps.last_turn_end_ts >= ps.blocked_on_lead_ts
        )

    def _check_proactive_compact(self, now: float) -> None:
        """Inject `/compact` into a Claude pane that's been continuously idle
        at its ready prompt for PROACTIVE_COMPACT_IDLE_AFTER_S — see that
        constant's comment for the prompt-cache-TTL cost rationale.

        Runs over every live pane (including Lead) regardless of
        `pane.state` — unlike the forgot-`takkub done` loop this rides
        alongside, a pane that already reported done (or Lead, which never
        calls done on itself) is exactly the case this targets, not one it
        skips. Never fires on a pane that's busy, booting, TTY-blocked, or
        currently rate-limited (rate-limited panes can't run `/compact`
        either — it would just join the same stuck queue).

        Claude-only: gated on `effective_provider_for(...) == CLAUDE`. Other
        providers are skipped without an alternative action — `/compact` has
        no confirmed equivalent slash command on codex/gemini/opencode/kimi/
        cursor (tracked as a known gap under #103, not silently assumed to
        work). A pane whose provider changes mid-episode (rare — provider is
        fixed at spawn) is simply re-evaluated fresh next tick.

        One `/compact` per idle episode: `proactive_compact_sent_ts` is
        compared against `proactive_compact_idle_since`, so a pane that stays
        idle for hours only gets nudged once, not every tick past the
        threshold. Going busy again for REAL work (new task, user typing,
        etc.) resets `proactive_compact_idle_since` to None, which starts a
        fresh episode the next time the pane settles.

        Issue #190: the pane going busy running the `/compact` THIS WATCHDOG
        just injected must NOT count as "going busy again" for that reset —
        `proactive_compact_pending` (set the instant we send `/compact`,
        cleared the next time the pane is observed back at its ready prompt)
        makes that one not-ready stretch transparent to
        `proactive_compact_idle_since`, so the same idle episode is still
        recognised as already-compacted once the pane settles. Without this,
        the compact's own busy→ready cycle looked identical to new work
        starting, so `proactive_compact_idle_since` got reset to "now" right
        after compacting, sent_ts (older) no longer gated it, and the same
        idle stretch fired `/compact` again ~one threshold later — forever
        (confirmed in runtime/events.log: repeat cycles at
        threshold + compact-run-time, e.g. +27min against a 25min threshold).

        Follow-up gap: `proactive_compact_pending` alone can't tell "still
        running our /compact" apart from "went not-ready again for real work
        before we ever observed it back at ready" — e.g. a task lands on the
        pane in the same window the compact was still finishing. Left
        unbounded, that would pin `idle_since` at its stale pre-compact value
        for as long as the pane keeps being busy, silently swallowing the
        NEXT idle episode's `/compact` once it finally settles. See
        PROACTIVE_COMPACT_PENDING_CEILING_S: once a not-ready stretch outlives
        it, `pending` is treated as stale and the not-ready branch falls back
        to the ordinary new-work path (clear pending, reset idle_since).

        Issue #465: a pane sitting at its own ready prompt is not always
        actually idle. Two extra gates run once the pane clears every check
        above (ready, not booting/TTY-blocked/rate-limited, Claude-only) but
        before the idle clock is allowed to accumulate — both reset
        `idle_since` to None (the same "not idle, don't count it" treatment
        the not-ready branch above already gives busy/booting panes) rather
        than merely skipping the fire, so the full threshold must elapse
        again fresh once the project genuinely settles:

        - **Lead** (`_lead_orchestrate_busy_reason`): a specialist in the
          same project still `pane.state == "working"`, an open `takkub
          wait` registration, or an inbox digest still queued (not yet
          written into Lead's pane) all mean Lead is actively orchestrating
          even though its own screen looks idle. Confirmed live
          (runtime/events.log 2026-09-01 09:39:26 / 10:36:31): Lead got
          `/compact`ed mid-orchestration while backend was still working
          #463/#464, summarizing away the spec/review/merge-plan context
          Lead needed the moment backend's `done()` landed 35s later.
        - **Specialist** (`_pane_waiting_for_lead`): the #463 "waiting-lead"
          tier — turn genuinely ended while blocked on Lead's reply, not
          done with the task, so it isn't idle either.

        Overage-aware threshold: `PROACTIVE_COMPACT_IDLE_AFTER_S` (base) vs
        `PROACTIVE_COMPACT_OVERAGE_IDLE_AFTER_S` (short, used once the
        project's cached usage state reports `limit_status.is_in_overage`) —
        see those constants' comments. Read once per project per tick
        (cheap best-effort local file read, never a network fetch) and
        reused for every pane in that project this tick.
        """
        if PROACTIVE_COMPACT_IDLE_AFTER_S <= 0 and PROACTIVE_COMPACT_OVERAGE_IDLE_AFTER_S <= 0:
            return
        from .limit_status import is_in_overage, load_shared_state
        from .provider_config import CLAUDE, effective_provider_for
        from .user_profile import config_dir_for

        for project_name, project_panes in list(self._panes_by_project.items()):
            _overage_loaded = False
            _project_in_overage = False
            for role, pane in list(project_panes.items()):
                try:
                    sess = pane.session
                    if sess is None or not sess.is_alive:
                        continue
                    key = f"{project_name}::{role}"
                    ps = self._ps(key)
                    if ps.proactive_compact_baseline_bytes < 0:
                        # #450: seed the baseline the very first tick this
                        # watchdog observes the pane, not only after its
                        # first `/compact` settles. Without this, a pane that
                        # spawns and then just sits idle (no task ever sent)
                        # falls through to the "nothing new since baseline"
                        # gate below with baseline still -1, which reads as
                        # "never compacted" and bypasses that gate entirely
                        # -> fires `/compact` into an empty conversation the
                        # moment PROACTIVE_COMPACT_IDLE_AFTER_S elapses.
                        # Seeded here, before the ready/not-ready branch
                        # below, so a pane that gets a task immediately after
                        # spawn still anchors near spawn-time output rather
                        # than after the task's own output already landed.
                        _seed_total = getattr(sess, "output_bytes_total", None)
                        if isinstance(_seed_total, int):
                            ps.proactive_compact_baseline_bytes = _seed_total
                    try:
                        _has_bg_work = sess.has_background_work()
                    except Exception:
                        _has_bg_work = False
                    if not isinstance(_has_bg_work, bool):
                        _has_bg_work = False
                    if (
                        not sess.is_at_ready_prompt()
                        or sess.shows_startup_marker()
                        or sess.is_blocked_on_tty_prompt()
                        or sess.is_blocked_on_permission_prompt()
                        or ps.rate_limited_until > now
                        # #392 part 2: `/compact` while a background
                        # shell/agent is still running (footer shows "esc to
                        # interrupt · ← for agents") would kill that
                        # background task the same way any other
                        # composer-busy interrupt does — never inject it into
                        # that window, only once the pane is genuinely idle.
                        or _has_bg_work
                    ):
                        # #190: don't null out idle_since for the busy stretch
                        # caused by the /compact we ourselves just injected —
                        # only a genuinely new not-ready (pending already
                        # cleared) means real work started.
                        if ps.proactive_compact_pending and (
                            now - ps.proactive_compact_sent_ts
                            <= PROACTIVE_COMPACT_PENDING_CEILING_S
                        ):
                            continue
                        # Either pending was never set (ordinary new-work
                        # not-ready), or it's been pending longer than a
                        # /compact could plausibly still be running — see
                        # PROACTIVE_COMPACT_PENDING_CEILING_S's comment.
                        # Either way this is real work now: clear any stale
                        # pending flag and reset idle_since so it starts a
                        # fresh episode once the pane settles.
                        ps.proactive_compact_pending = False
                        ps.proactive_compact_idle_since = None
                        ps.proactive_compact_skip_logged_bytes = -1
                        continue
                    if ps.proactive_compact_pending:
                        # Pane is back at its ready prompt for the first time
                        # since we sent /compact — the compact episode is
                        # over. Clear the flag but deliberately leave
                        # idle_since untouched so this idle episode still
                        # reads as already-compacted.
                        ps.proactive_compact_pending = False
                        # Baseline for the "nothing new since" gate: taken
                        # AFTER the compact's own output so the summary it
                        # printed never counts as new conversation.
                        total = getattr(sess, "output_bytes_total", None)
                        if isinstance(total, int):
                            ps.proactive_compact_baseline_bytes = total
                        # Baseline moved — any prior "nothing new" dedupe
                        # value is stale.
                        ps.proactive_compact_skip_logged_bytes = -1
                    if effective_provider_for(role, project=project_name) != CLAUDE:
                        continue

                    # #465: Lead/specialist "not really idle" gates — see the
                    # docstring above. Reset (not just skip) idle_since so a
                    # fresh full threshold is required once genuinely settled.
                    if role == LEAD.name:
                        _busy_reason = self._lead_orchestrate_busy_reason(
                            project_name, project_panes
                        )
                    else:
                        _busy_reason = (
                            "waiting-lead" if self._pane_waiting_for_lead(ps, now) else None
                        )
                    if _busy_reason is not None:
                        ps.proactive_compact_idle_since = None
                        if ps.proactive_compact_busy_logged != _busy_reason:
                            ps.proactive_compact_busy_logged = _busy_reason
                            _log_event(
                                "proactive_idle_compact_skipped",
                                role=role,
                                project=project_name,
                                reason=_busy_reason,
                            )
                        continue
                    ps.proactive_compact_busy_logged = ""

                    if ps.proactive_compact_idle_since is None:
                        ps.proactive_compact_idle_since = now
                        continue
                    idle_for = now - ps.proactive_compact_idle_since
                    if not _overage_loaded:
                        try:
                            _project_in_overage = is_in_overage(
                                load_shared_state(config_dir_for(project_name))["data"]
                            )
                        except Exception:
                            _project_in_overage = False
                        _overage_loaded = True
                    threshold = (
                        PROACTIVE_COMPACT_OVERAGE_IDLE_AFTER_S
                        if _project_in_overage
                        else PROACTIVE_COMPACT_IDLE_AFTER_S
                    )
                    if threshold <= 0 or idle_for < threshold:
                        continue
                    if ps.proactive_compact_sent_ts >= ps.proactive_compact_idle_since:
                        continue  # already compacted this idle episode
                    total = getattr(sess, "output_bytes_total", None)
                    if (
                        PROACTIVE_COMPACT_MIN_NEW_OUTPUT_BYTES > 0
                        and isinstance(total, int)
                        and ps.proactive_compact_baseline_bytes >= 0
                        and total - ps.proactive_compact_baseline_bytes
                        < PROACTIVE_COMPACT_MIN_NEW_OUTPUT_BYTES
                    ):
                        # Nothing new to compact since the last one settled.
                        # Dedupe logging to once per distinct byte count
                        # (not every 5s tick) WITHOUT stamping sent_ts — see
                        # this gate's #465 follow-up comment above for why.
                        if ps.proactive_compact_skip_logged_bytes != total:
                            ps.proactive_compact_skip_logged_bytes = total
                            _log_event(
                                "proactive_idle_compact_skipped",
                                role=role,
                                project=project_name,
                                reason="nothing-new",
                                idle_for=round(idle_for),
                                new_bytes=total - ps.proactive_compact_baseline_bytes,
                            )
                        continue
                    sess.write("/compact")
                    _delayed_enter(pane, sess, 150)
                    ps.proactive_compact_sent_ts = now
                    ps.proactive_compact_pending = True
                    ps.proactive_compact_skip_logged_bytes = -1
                    _log_event(
                        "proactive_idle_compact",
                        role=role,
                        project=project_name,
                        idle_for=round(idle_for),
                        overage=_project_in_overage,
                    )
                except Exception as e:
                    _log_event(
                        "proactive_compact_check_error",
                        role=role,
                        project=project_name,
                        err=f"{type(e).__name__}: {e}",
                    )

    def _check_shell_open_dialog(
        self, project_name: str, role: str, pane: AgentPane, key: str, now: float
    ) -> None:
        """Issue #104 tripwire: nudge Lead once if `pane`'s transcript tail
        shows the Windows Open-With dialog marker — a shell one-liner
        ShellExecute'd a bare file path instead of the pane using Read/Grep.

        Issue #194: throttled to once every _SHELL_DIALOG_SCAN_INTERVAL_S per
        pane (was every 5s watchdog tick), gated on the pane having been
        silent for _SHELL_DIALOG_IDLE_GATE_S (an actively-working pane skips
        the file open entirely), and the actual open()+read() runs on a
        background thread, so a slow disk/AV-scanned file open never blocks
        the Qt main thread. `_notify_lead` still runs on the GUI thread —
        it's marshalled back via QTimer.singleShot(0, ...) once the scan
        result is in.

        Issue #199: raw substring matching false-positived on the
        cockpit's OWN notify message below (a pane editing/Read-ing this
        exact source line saw the marker echoed back in its own
        transcript) with zero corroborating evidence that an actual OS
        dialog existed. Two independent guards now gate the notify:
        (1) `_looks_like_source_reference` discards matching lines that
        read like source/diff context; (2) on Windows, an OpenWith/
        AppPicker/OpenAs_RunDLL process must actually be running. This
        dialog type is Windows-only, so on any other platform the scan
        still runs (cheap, still useful for #194's idle-gate metrics) but
        never notifies — the "other branch" for non-Windows is simply
        "this can't happen here."

        Degrades silently on any I/O hiccup; never raises into the
        watchdog tick (mirrors `_scan_done_evidence`'s degrade-silently
        contract)."""
        ps = self._ps(key)
        if ps.shell_open_dialog_notified or ps.dialog_scan_in_flight:
            return
        transcript_path = getattr(pane, "_transcript_path", None)
        if not transcript_path:
            return
        last_out = getattr(pane, "_last_output_ts", 0.0)
        if not isinstance(last_out, (int, float)) or now - last_out < _SHELL_DIALOG_IDLE_GATE_S:
            return  # pane has recent progress — can't be blocked on a modal dialog (#199)
        if now - ps.last_dialog_scan_ts < _SHELL_DIALOG_SCAN_INTERVAL_S:
            return
        ps.last_dialog_scan_ts = now
        ps.dialog_scan_in_flight = True

        def _scan_worker() -> None:
            found = False
            try:
                raw = _read_tail_bytes(pathlib.Path(transcript_path), _TRANSCRIPT_TAIL_BYTES)
                tail = raw.decode("utf-8", errors="replace")
                candidate = any(
                    _SHELL_OPEN_DIALOG_MARKER in line and not _looks_like_source_reference(line)
                    for line in tail.splitlines()
                )
                if candidate and sys.platform == "win32" and _open_with_dialog_process_present():
                    found = True
            except OSError:
                pass

            def _finish() -> None:
                ps.dialog_scan_in_flight = False
                if not found or ps.shell_open_dialog_notified:
                    return
                ps.shell_open_dialog_notified = True
                self._notify_lead(
                    project_name,
                    f"[cockpit] {role} pane อาจติด Windows 'How do you want to open this file?' "
                    "dialog (ShellExecute path แทน Read tool, #104) — เช็ค/ปิด dialog บนเครื่อง "
                    "แล้วเตือน pane ให้ใช้ Read/Grep tool อ่านไฟล์แทน shell one-liner",
                    from_role=role,
                    note="",
                    kind="shell-open-dialog",
                )

            QTimer.singleShot(0, _finish)

        threading.Thread(target=_scan_worker, daemon=True, name="shell-dialog-scan").start()

    def _check_stuck_panes(self, now: float) -> None:
        """Walk every teammate pane and auto-recover any that's been
        sitting in `working` state with no PTY output for longer than
        STUCK_THRESHOLD_S. A recovered pane runs close→spawn and gets
        --resume <uuid> via the session-uuid + recent-exits machinery, so
        claude rejoins the conversation rather than restarting blank.

        Lead is exempt: Lead's "stuck" usually means waiting on user
        input, not a hang, and a forced restart would lose Lead's
        conversation with the operator. Teammates are the safe target."""
        for project_name, project_panes in list(self._panes_by_project.items()):
            for role, pane in list(project_panes.items()):
                try:
                    if role == LEAD.name:
                        continue
                    if pane.state != "working":
                        continue
                    if pane.session is None or not pane.session.is_alive:
                        continue
                    key = f"{project_name}::{role}"
                    # Issue #104: independent of stuck/idle detection below —
                    # a ShellExecute Open-With dialog doesn't necessarily stop
                    # PTY output (the offending command may return immediately),
                    # so this must run whether or not the pane looks stuck.
                    self._check_shell_open_dialog(project_name, role, pane, key, now)
                    # A rate-limited pane is silent on purpose — never force-respawn
                    # it (the fresh session would just hit the same limit). The idle
                    # walker owns detection; here we only read the recorded state.
                    if (self._pane_state.get(key) or PaneState()).rate_limited_until > now:
                        continue
                    last_out = getattr(pane, "_last_output_ts", 0.0)
                    if not isinstance(last_out, (int, float)) or last_out <= 0:
                        # Pane hasn't seen output yet — still in bootstrap,
                        # or the attribute was never initialised (legacy
                        # AgentPane subclass / test fixture). Skip; the next
                        # tick will pick it up once a real timestamp lands.
                        continue
                    # Bug-2 fix: measure screen-content delta excluding the spinner
                    # region ('esc to interrupt').  Raw byte timestamps are bumped on
                    # every PTY byte including the animated spinner, so a claude
                    # wedged on a slow MCP call never trips STUCK_THRESHOLD_S with
                    # the old byte-only check.  Content-delta is immune to spinners.
                    ps_ck = self._ps(key)
                    try:
                        disp = pane.session.display_lines()
                        # Fix 3: filter spinner/status lines more broadly.
                        # Exclude lines matching any known interrupt phrase OR volatile
                        # counter patterns (elapsed seconds, token counters) so a
                        # counter-only spinner line doesn't keep resetting the hash.
                        _filtered_lines = "\n".join(
                            ln
                            for ln in disp
                            if not any(p in ln.lower() for p in _spinner_interrupt_phrases())
                            and not _SPINNER_VOLATILE_RE.search(ln)
                        )
                        non_spinner_hash = hashlib.blake2b(
                            _filtered_lines.encode("utf-8", errors="replace"),
                            digest_size=8,
                        ).hexdigest()
                        prev_hash = ps_ck.last_content_hash
                        if prev_hash != non_spinner_hash:
                            ps_ck.last_content_hash = non_spinner_hash
                            if prev_hash is not None:
                                # Genuine content change (not first observation) →
                                # reset the change clock so the pane isn't recovered.
                                ps_ck.last_content_change_ts = now
                            elif ps_ck.last_content_change_ts is None:
                                # First time we see this pane: initialise from
                                # last_out so an already-stale pane is detected on
                                # the very first tick rather than getting a free
                                # STUCK_THRESHOLD_S grace period.
                                ps_ck.last_content_change_ts = last_out
                    except Exception:
                        # display_lines() failed (session torn down mid-tick); fall
                        # back to initialising the ts from last raw byte time.
                        if ps_ck.last_content_change_ts is None:
                            ps_ck.last_content_change_ts = last_out
                    last_content_ts = ps_ck.last_content_change_ts
                    if last_content_ts is None:
                        last_content_ts = last_out
                    # Throughput watchdog (issue #35): detect runaway output loops
                    # that flood the Qt main thread. The existing stuck detector
                    # only catches *silent* or *content-stable* panes; a pane in a
                    # runaway loop has ever-changing content and is never "stuck" by
                    # the existing metric. Here we measure byte rate and warn Lead.
                    _tp_total = getattr(pane, "_tp_total_bytes", 0)
                    if ps_ck.tp_last_ts > 0:
                        _tp_elapsed = now - ps_ck.tp_last_ts
                        if _tp_elapsed > 0:
                            _tp_delta = _tp_total - ps_ck.tp_last_total
                            _tp_rate = _tp_delta / _tp_elapsed
                            if _tp_rate > RUNAWAY_BYTES_S:
                                if ps_ck.tp_runaway_since is None:
                                    ps_ck.tp_runaway_since = now
                                elif (now - ps_ck.tp_runaway_since) >= RUNAWAY_DURATION_S:
                                    if (now - ps_ck.tp_warn_ts) >= RUNAWAY_WARN_COOLDOWN_S:
                                        self._warn_lead_runaway_pane(role, project_name, _tp_rate)
                                        ps_ck.tp_warn_ts = now
                            else:
                                ps_ck.tp_runaway_since = None
                    ps_ck.tp_last_total = _tp_total
                    ps_ck.tp_last_ts = now
                    if (now - last_content_ts) < STUCK_THRESHOLD_S:
                        continue
                    if ps_ck.stuck_recover_gave_up:
                        # Already hit STUCK_RECOVER_MAX for this pane and handed it
                        # to the operator (#41). Stop recovering — re-recovering a
                        # deterministically-wedged pane just loops + burns tokens.
                        continue
                    last_recover = ps_ck.last_stuck_recover
                    if (now - last_recover) < STUCK_RECOVER_COOLDOWN_S:
                        # Already tried to recover this pane recently;
                        # leave it alone so we don't loop close→spawn.
                        continue
                    if ps_ck.stuck_recover_attempts >= STUCK_RECOVER_MAX:
                        # Recovered MAX times and it wedged again — giving up beats
                        # an infinite close→respawn loop that stalls the pipeline (#41).
                        self._give_up_stuck(role, project_name, pane, now)
                        continue
                    # Issue #54: if the pane is blocked on a TTY prompt, close→respawn
                    # won't help (the prompt comes from a subprocess). Defer recovery
                    # and surface to Lead instead.
                    # Note: we're already past STUCK_THRESHOLD_S here, so we skip the
                    # TTY_BLOCK_SURFACE_AFTER_S grace period and surface immediately
                    # (only the repeat-spam cooldown applies).
                    try:
                        _perm_stuck = pane.session.is_blocked_on_permission_prompt()
                        _tty_stuck = _perm_stuck or pane.session.is_blocked_on_tty_prompt()
                    except Exception:
                        _perm_stuck = None
                        _tty_stuck = None
                    if _tty_stuck:
                        _ps_tty = self._ps(key)
                        if _ps_tty.tty_blocked_since is None:
                            _ps_tty.tty_blocked_since = now
                        if (
                            now - _ps_tty.last_tty_block_surface_ts
                        ) >= TTY_BLOCK_SURFACE_COOLDOWN_S:
                            self._surface_tty_block_notice(
                                role,
                                project_name,
                                _tty_stuck,
                                kind="permission" if _perm_stuck else "tty",
                            )
                            _ps_tty.last_tty_block_surface_ts = now
                        continue
                    # Issue #62: codex 'update available!' splash blocks ready-prompt.
                    # Send Enter once to dismiss; if it doesn't clear within
                    # SPLASH_DISMISS_COOLDOWN_S fall back to close→respawn.
                    try:
                        _at_splash = pane.session.is_at_update_splash()
                    except Exception:
                        _at_splash = False
                    if _at_splash:
                        _ps_sp = self._ps(key)
                        if _ps_sp.splash_dismiss_ts == 0.0:
                            pane.session.write(b"\r")
                            _log_event(
                                "pane_recovered_update_splash",
                                role=role,
                                project=project_name,
                            )
                            _ps_sp.splash_dismiss_ts = now
                            continue
                        if (now - _ps_sp.splash_dismiss_ts) < SPLASH_DISMISS_COOLDOWN_S:
                            continue
                        # Dismiss didn't clear the splash — fall through to close→respawn
                    # #288: LAST gate before the kill. A static screen is not
                    # proof of a wedge — a QA pane that writes a Playwright
                    # script and runs `node <script>.js` prints nothing for
                    # minutes while node + chrome-headless-shell do the work.
                    # The 2026-08-17 field case killed exactly that pane and
                    # logged `close_kills_live_children count=8` in the same
                    # event as `content_static_s=600`: the process list proving
                    # the pane was busy was collected on the way out, one step
                    # too late to change the decision. Re-using it here is
                    # cheap because nothing reaches this line until a pane has
                    # already been content-static for STUCK_THRESHOLD_S.
                    #
                    # Respawning a live QA pane is worse than leaving it alone:
                    # the respawn is `resumed: false`, so it restarts the task
                    # from step 1 and re-clicks buttons it has already clicked
                    # in whatever real system it is driving.
                    if self._defer_stuck_recover_for_live_children(
                        role, project_name, pane, ps_ck, now
                    ):
                        continue
                    self._auto_recover_stuck(role, project_name, pane, now)
                except Exception:
                    _log_event("stuck_watchdog_pane_error", role=role, project=project_name)

    def _defer_stuck_recover_for_live_children(
        self, role: str, project: str, pane: AgentPane, ps_ck: PaneState, now: float
    ) -> bool:
        """True when the stuck watchdog must NOT recover *pane* because real
        work is still running under it (#288).

        The grace is bounded: a child that is itself hung would otherwise pin
        the pane forever, so once the deferral has run past
        ``STUCK_LIVE_CHILD_GRACE_S`` the watchdog takes the pane back and
        recovers it, with the reason recorded. Lead is told once per pane per
        deferral episode — the point is that a silent 10-minute pane is
        expected for script-running work, not that every tick needs a notice.
        """
        try:
            names = self._live_non_scaffolding_children(project, role, pane.session)
        except Exception:
            names = []
        if not names:
            ps_ck.live_child_defer_since = 0.0
            return False
        if ps_ck.live_child_defer_since == 0.0:
            ps_ck.live_child_defer_since = now
        deferred_for = now - ps_ck.live_child_defer_since
        if deferred_for >= STUCK_LIVE_CHILD_GRACE_S:
            _log_event(
                "stuck_recover_live_children_grace_expired",
                role=role,
                project=project,
                deferred_for_s=int(deferred_for),
                children=names[:10],
            )
            ps_ck.live_child_defer_since = 0.0
            return False
        if (now - ps_ck.last_live_child_defer_log_ts) >= STUCK_LIVE_CHILD_NOTICE_COOLDOWN_S:
            ps_ck.last_live_child_defer_log_ts = now
            _log_event(
                "stuck_recover_deferred_live_children",
                role=role,
                project=project,
                deferred_for_s=int(deferred_for),
                count=len(names),
                children=names[:10],
            )
            self._notify_lead(
                project,
                f"⏳ [{role}] จอไม่ขยับเกิน {int(STUCK_THRESHOLD_S / 60)} นาที แต่ยังมี "
                f"{len(names)} process ทำงานอยู่ใต้ pane ({', '.join(names[:5])}) — "
                "watchdog เลื่อนการ respawn ออกไปแทนที่จะฆ่าทิ้ง งานยังเดินอยู่",
                from_role=role,
                note="stuck_recover_deferred",
                kind="stuck-recover-deferred",
            )
        # The pane is demonstrably alive, so give the content clock a fresh
        # window instead of letting it keep accumulating — otherwise the very
        # next tick after the children exit would recover a pane whose only
        # crime was doing long quiet work.
        ps_ck.last_content_change_ts = now
        return True

    def _check_stuck_tool_panes(self, now: float) -> None:
        """Escalate + attempt one-shot recovery for a pane wedged inside a
        shell/tool call the underlying CLI never returns from (#308).

        Deliberately a SEPARATE watchdog from `_check_stuck_panes` above,
        not a branch inside it: that one's content-hash detector (and the
        idle-reminder loop's `is_at_ready_prompt()` gate) both read the
        pane's screen as normal the whole time #308's own incident ran,
        because the provider's idle footer ("? for shortcuts") stayed
        visible on screen below the "Running command..." status line the
        entire 13 minutes. This checks `ProviderSpec.tool_running_markers`
        directly, independent of what the ready-marker classifier says,
        paired with `PtySession.seconds_since_output()` (spinner-normalized)
        for "has this been sitting here with no real change underneath it".

        Recovery is ONE automatic step only, never a loop: send Esc once (the
        same key every provider's own screen already advertises as "esc to
        interrupt"/"esc to cancel" while busy), wait
        `TOOL_STUCK_ESC_GRACE_S`, then either tell Lead it cleared or tell
        Lead once that it didn't and a manual `takkub close` + respawn is the
        only way out (#308's own confirmed workaround) — this never closes
        or respawns the pane itself; that stays Lead's call.
        """
        from .provider_config import effective_provider_for

        for project_name, project_panes in list(self._panes_by_project.items()):
            for role, pane in list(project_panes.items()):
                try:
                    if role == LEAD.name:
                        continue
                    if pane.state != "working":
                        continue
                    session = pane.session
                    if session is None or not session.is_alive:
                        continue
                    key = f"{project_name}::{role}"
                    provider = effective_provider_for(role, project=project_name)
                    try:
                        marker = session.tool_running_marker(provider)
                        stale_for = session.seconds_since_output()
                    except Exception:
                        marker = None
                        stale_for = 0.0
                    # Guard against a loosely-mocked session in tests: an
                    # un-stubbed MagicMock attribute call returns a truthy
                    # MagicMock, not None/str — same isinstance guard
                    # `_check_stuck_panes` already uses for `last_out` above.
                    if not isinstance(marker, str) or not marker:
                        marker = None
                    if not isinstance(stale_for, (int, float)):
                        stale_for = 0.0
                    ps = self._ps(key)
                    stuck = marker is not None and stale_for >= TOOL_STUCK_TIMEOUT_SEC
                    if not stuck:
                        if ps.tool_stuck_escalated:
                            _log_event("tool_stuck_recovered", role=role, project=project_name)
                            nudge = (
                                "🔧 [auto-recovery] pane นี้ค้างอยู่ในเครื่องมือ shell เกิน "
                                f"{int(TOOL_STUCK_TIMEOUT_SEC // 60)} นาที ระบบส่ง Esc ให้ไปแล้ว "
                                "และตอนนี้กลับมาที่ composer แล้ว — คำสั่งก่อนหน้าค้าง ข้ามแล้ว "
                                "ทำงานต่อได้เลย (ถ้าคำสั่งนั้นยังไม่เสร็จจริง ให้สั่งใหม่)"
                            )
                            try:
                                session.write(nudge)
                                _delayed_enter(pane, session, 150)
                            except Exception:
                                pass
                            self._notify_lead(
                                project_name,
                                f"✅ [system] {role} หลุดจาก shell tool ที่ค้างแล้ว กลับมาทำงานต่อเอง",
                                from_role=role,
                                note="tool_stuck_recovered",
                                kind="tool-stuck-recovered",
                            )
                        ps.tool_stuck_escalated = False
                        ps.tool_stuck_esc_sent_ts = 0.0
                        ps.tool_stuck_close_recommended = False
                        continue

                    if not ps.tool_stuck_escalated:
                        _log_event(
                            "tool_stuck",
                            role=role,
                            project=project_name,
                            marker=marker,
                            stuck_for_s=int(stale_for),
                        )
                        from .provider_spec import tool_stuck_auto_esc_for

                        auto_esc = tool_stuck_auto_esc_for(provider)
                        tail = (
                            "— ส่ง Esc ให้ 1 ครั้งแล้ว กำลังรอดูว่าหลุดไหม"
                            if auto_esc
                            else "— ไม่ส่ง Esc อัตโนมัติ (provider นี้ tool เงียบนานอาจเป็นงานจริง) "
                            f"ถ้าค้างจริงสั่ง `takkub close --role {role}` แล้ว assign ใหม่"
                        )
                        self._notify_lead(
                            project_name,
                            f"⚠ [system] {role} ค้างอยู่ใน shell tool เกิน "
                            f'{int(TOOL_STUCK_TIMEOUT_SEC // 60)} นาที (จอโชว์: "{marker}") ' + tail,
                            from_role=role,
                            note="tool_stuck",
                            kind="tool-stuck",
                        )
                        ps.tool_stuck_escalated = True
                        if auto_esc:
                            try:
                                session.write("")
                            except Exception:
                                pass
                            ps.tool_stuck_esc_sent_ts = now
                        else:
                            # No keystroke sent → nothing to wait for; the close
                            # recommendation is already in the notice above (#308).
                            ps.tool_stuck_close_recommended = True
                        continue

                    if (
                        not ps.tool_stuck_close_recommended
                        and ps.tool_stuck_esc_sent_ts
                        and (now - ps.tool_stuck_esc_sent_ts) >= TOOL_STUCK_ESC_GRACE_S
                    ):
                        self._notify_lead(
                            project_name,
                            f"⛔ [system] {role} ยังค้างใน shell tool อยู่ แม้ส่ง Esc ไปแล้ว "
                            "— auto-recovery ทำต่อให้ไม่ได้ (root cause อยู่ใน provider CLI เอง) "
                            f"แนะนำ `takkub close --role {role}` แล้ว assign งานใหม่",
                            from_role=role,
                            note="tool_stuck_esc_failed",
                            kind="tool-stuck-esc-failed",
                        )
                        ps.tool_stuck_close_recommended = True
                except Exception:
                    _log_event("tool_stuck_watchdog_error", role=role, project=project_name)

    def _session_uuid_for(self, key: str) -> str | None:
        """Best-known transcript uuid for a `project::role` slot (#422 item 3):
        the live PaneState first, else the last `session_report` this slot
        ever made (survives the PaneState pop done()/close() perform before
        they log)."""
        ps = getattr(self, "_pane_state", {}).get(key)
        live = getattr(ps, "session_uuid", None)
        if live:
            return live
        return getattr(self, "_last_session_uuid", {}).get(key)

    def _auto_recover_stuck(self, role: str, project: str, pane: AgentPane, now: float) -> None:
        """Close the wedged pane and respawn it with --resume <uuid>. The
        spawn uses the pane's last-known cwd so claude rejoins the same
        project directory.

        Bug-1 fix: close() pops session UUID, last-task, auto-chain flag and
        requires-commit gate — without a snapshot/restore the respawned session
        starts blank (no --resume despite the docstring), drops the verify hop
        from auto-chain, and silently loses the commit gate.  We snapshot those
        four fields before teardown and restore them in the respawn callback so
        spawn() can_resume logic finds the UUID and the task/flags survive."""
        cwd = pane._session_cwd
        key = f"{project}::{role}"

        # Snapshot fields that close() will pop so _do_respawn can restore them.
        _ps_snap = self._pane_state.get(key)
        snap_uuid = _ps_snap.session_uuid if _ps_snap is not None else None
        snap_uuid_cwd = _ps_snap.session_uuid_cwd if _ps_snap is not None else ""
        snap_task = _ps_snap.last_assigned_task if _ps_snap is not None else None
        snap_auto_chain = _ps_snap.auto_chain if _ps_snap is not None else False
        snap_requires_commit = _ps_snap.requires_commit_on_done if _ps_snap is not None else False
        snap_shard_total = _ps_snap.shard_total if _ps_snap is not None else 0
        snap_pipeline_run_id = _ps_snap.pipeline_run_id if _ps_snap is not None else None
        # #245: same task resumes, so the digest baseline must survive too —
        # a stuck-recover respawn isn't a new assign() dispatch (which is the
        # only place that takes a FRESH snapshot), so without this restore
        # the resumed pane would lose its baseline and done()'s fact table
        # would report "ตรวจไม่ได้" for a pane that actually had one.
        snap_assign_base_sha = _ps_snap.assign_base_sha if _ps_snap is not None else None
        snap_assign_git_root = _ps_snap.assign_git_root if _ps_snap is not None else None
        snap_assign_dirty_snapshot = (
            _ps_snap.assign_dirty_snapshot if _ps_snap is not None else None
        )
        # #41: carry the stuck-recover attempt count across the close→respawn so
        # the watchdog can enforce STUCK_RECOVER_MAX (close() pops the PaneState).
        snap_recover_attempts = _ps_snap.stuck_recover_attempts if _ps_snap is not None else 0

        self._ps(key).last_stuck_recover = now
        # silent_for_s = raw-byte silence. It is frequently 0 even on a genuine
        # recover because the animated spinner ("esc to interrupt") keeps
        # emitting bytes — so on its own it does NOT explain why the watchdog
        # fired and reads as a false alarm. The actual trigger is
        # content_static_s: how long the spinner-filtered screen content stayed
        # byte-for-byte identical (>= STUCK_THRESHOLD_S is what trips recovery).
        # Log both so the recover reason is unambiguous in events.log.
        silent_for_s = int(now - getattr(pane, "_last_output_ts", now))
        _content_ts = _ps_snap.last_content_change_ts if _ps_snap is not None else None
        content_static_s = int(now - _content_ts) if _content_ts is not None else -1
        # Reset the output timestamp so the next tick doesn't re-trigger
        # before claude has had a chance to print anything from the new
        # session.
        # #422: classify WHY (closed enum) + attach a bounded screen/process
        # snapshot + a recovery_id that the respawn/re-deliver events below
        # share, so one recovery reads as one chain in events.log instead of
        # three timestamps to eyeball (#418 post-mortem).
        recovery_id = _uuid.uuid4().hex[:12]
        _idle_rounds = int((self._idle_state.get(key) or {}).get("notice_rounds") or 0)
        reason = classify_stuck_reason(
            idle_rounds=_idle_rounds,
            live_child_defer_since=(
                _ps_snap.live_child_defer_since if _ps_snap is not None else 0.0
            ),
        )
        try:
            _children = self._live_non_scaffolding_children(project, role, pane.session)
        except Exception:
            _children = []
        snapshot = recovery_snapshot(
            pane.session,
            now=now,
            last_output_ts=getattr(pane, "_last_output_ts", None),
            last_content_ts=_content_ts,
            assign_ts=_ps_snap.assign_ts if _ps_snap is not None else None,
            children=_children,
            spinner_phrases=_spinner_interrupt_phrases(),
        )
        pane._last_output_ts = now
        _log_event(
            "stuck_pane_recover",
            role=role,
            project=project,
            cwd=cwd or "",
            silent_for_s=silent_for_s,
            content_static_s=content_static_s,
            reason=reason,
            idle_rounds=_idle_rounds,
            recovery_id=recovery_id,
            session_uuid=snap_uuid,
            snapshot=snapshot,
        )
        # suppress_pipeline + suppress_auto_chain: this close is the first half of a
        # close→respawn recovery, not a real pane death.  Neither the pipeline hop
        # nor the auto-chain handoff should advance here — the same role respawns
        # 2 s later with its auto_chain flag restored by _do_respawn.
        self.close(role, project=project, suppress_pipeline=True, suppress_auto_chain=True)

        def _do_respawn() -> None:
            # Restore snapshotted state before spawn() runs so:
            #   - session_uuid lets can_resume pick --resume <uuid>
            #   - last_assigned_task survives for replay (gated by Bug-5 fix)
            #   - auto_chain keeps the verify-hop tag alive
            #   - requires_commit_on_done preserves the commit gate
            # Cooldown stamp: close() pops the whole PaneState so last_stuck_recover
            # reverts to 0.0 — restore it here so the watchdog can't re-trigger
            # within STUCK_RECOVER_COOLDOWN_S of the recovery attempt.
            self._ps(key).last_stuck_recover = now
            # #41: persist the incremented stuck-recover count across the
            # close()-pop so the watchdog can enforce STUCK_RECOVER_MAX (a
            # wedged-but-alive pane never crashes, so auto_respawn_attempts —
            # which only counts crashes — never caps it).
            self._ps(key).stuck_recover_attempts = snap_recover_attempts + 1
            if snap_uuid is not None:
                _ps_r = self._ps(key)
                _ps_r.session_uuid = snap_uuid
                _ps_r.session_uuid_cwd = snap_uuid_cwd
            if snap_task is not None:
                self._ps(key).last_assigned_task = snap_task
            if snap_auto_chain:
                self._ps(key).auto_chain = snap_auto_chain
            if snap_requires_commit:
                self._ps(key).requires_commit_on_done = snap_requires_commit
            if snap_shard_total:
                self._ps(key).shard_total = snap_shard_total
            if snap_pipeline_run_id is not None:
                self._ps(key).pipeline_run_id = snap_pipeline_run_id
            if snap_assign_base_sha is not None:
                self._ps(key).assign_base_sha = snap_assign_base_sha
            if snap_assign_git_root is not None:
                self._ps(key).assign_git_root = snap_assign_git_root
            if snap_assign_dirty_snapshot is not None:
                self._ps(key).assign_dirty_snapshot = snap_assign_dirty_snapshot
            # m3 fix: if PTY teardown hasn't fired _on_session_exit yet (takes
            # longer than the 2s singleShot on a slow machine), _recent_exits
            # has no entry and spawn()'s can_resume returns False → blank session.
            # Synthesise the entry from snap_uuid so we never depend on timing.
            if snap_uuid is not None and key not in self._recent_exits:
                self._recent_exits[key] = {
                    "cwd": snap_uuid_cwd or cwd or "",
                    "ts": time.time(),
                }
            ok, msg = self.spawn(
                role,
                cwd=cwd,
                project=project,
                _from_auto_respawn=True,
                _shard_total=snap_shard_total,
            )
            _log_event(
                "stuck_recover_respawn",
                role=role,
                project=project,
                ok=ok,
                msg=msg[:160],
                reason=reason,
                recovery_id=recovery_id,
                session_uuid=snap_uuid,
            )
            if not ok:
                # Spawn failed — pop the whole PaneState (pane is dead, return
                # to post-close empty state) rather than resetting fields one by
                # one.  Matches the "popped atomically by close()/done()" contract
                # and avoids leaving an empty PaneState entry in _pane_state.
                self._pane_state.pop(key, None)
                # Recovery truly failed: the recovery-close suppressed the
                # pipeline fail/advance assuming the role would come back. It
                # won't — so now mark it failed and advance the hop, else the
                # pipeline stalls forever waiting on a pane that's gone.
                if snap_pipeline_run_id is not None:
                    pl_key = f"{project}::{snap_pipeline_run_id}"
                    pl_run = self._pipeline_runs.get(pl_key)
                    if pl_run is not None and not pl_run.closed:
                        pl_run.hop_pending.discard(role)
                        pl_run.hop_failed.add(role)
                        if not pl_run.hop_pending:
                            self._advance_pipeline(project, pl_key, pl_run)
                return
            # Drive the recovered pane so it actually continues the task:
            #   - blank respawn (no --resume): re-paste the original task verbatim.
            #   - resumed respawn (--resume): claude reloads the conversation but
            #     sits idle at the ready prompt — it does NOT auto-continue the
            #     interrupted turn, so the pane would silently stall ("ไม่ทำต่อ").
            #     Send a short continue-nudge instead of the full task (Bug-5
            #     gate: never re-paste the whole task into restored history — that
            #     would double the work).
            # Fix 1: read structured flag set by spawn() instead of parsing msg.
            _ps_after = self._pane_state.get(key)
            spawn_resumed = _ps_after.last_spawn_resumed if _ps_after is not None else False
            if snap_task:
                if not spawn_resumed:
                    self._send_when_ready(role, snap_task, project=project)
                else:
                    self._send_when_ready(role, _STUCK_RESUME_NUDGE, project=project)

        # 2 s pause so the close has time to terminate the PTY and tear
        # down the WebEngine view before the respawn binds a new one
        # to the same role slot.
        QTimer.singleShot(2_000, _do_respawn)

    def _give_up_stuck(self, role: str, project: str, pane: AgentPane, now: float) -> None:
        """STUCK_RECOVER_MAX hit (#41): stop auto-recovering a wedged-but-alive
        pane. Recovering it again just loops — it re-wedges deterministically —
        and, if it belongs to a pipeline hop, stalls that pipeline forever waiting
        on a done event that never comes. So we give up exactly ONCE: flag the
        pane so the watchdog leaves it alone, drop any auto-chain tag (siblings
        would otherwise wait forever), warn Lead, and — if it's a pipeline-hop
        role — mark it failed + advance the hop (mirrors the crash-cap branch in
        _do_respawn / _schedule_auto_respawn). The pane is left ALIVE so the
        operator can inspect it and reassign; nothing keeps recovering it."""
        key = f"{project}::{role}"
        ps = self._ps(key)
        if ps.stuck_recover_gave_up:
            return  # one-shot — never warn / advance more than once per pane
        ps.stuck_recover_gave_up = True
        ps.last_stuck_recover = now
        _log_event(
            "stuck_recover_capped",
            role=role,
            project=project,
            attempts=ps.stuck_recover_attempts,
        )
        # An auto-chain verify-hop sibling would wait forever for this pane's
        # done event; drop the tag so a capped pane can't keep a hop open. If it
        # was the last blocker, release the chain so the hop doesn't deadlock
        # (bug-1 orch).
        _had_ac_stuck = ps.auto_chain
        ps.auto_chain = False
        self._maybe_fire_auto_chain_handoff(project, _had_ac_stuck)
        # Pipeline hop: fail + advance so the run doesn't stall on a pane that
        # will never report done (same bookkeeping as the respawn-fail path).
        pl_run_id = ps.pipeline_run_id
        if pl_run_id is not None:
            pl_key = f"{project}::{pl_run_id}"
            pl_run = self._pipeline_runs.get(pl_key)
            if pl_run is not None and not pl_run.closed:
                pl_run.hop_pending.discard(role)
                pl_run.hop_failed.add(role)
                if not pl_run.hop_pending:
                    self._advance_pipeline(project, pl_key, pl_run)
            # Unlink the (still-alive) pane from the run so a later operator
            # close() can't re-enter the pipeline-fail branch and spuriously
            # advance a DIFFERENT (already-advanced) hop. The crash-cap path gets
            # this for free by popping the PaneState; we keep the pane alive for
            # inspection, so clear the linkage explicitly.
            ps.pipeline_run_id = None
        lead = self._project_panes(project).get(LEAD.name)
        if lead and lead.session and lead.session.is_alive:
            msg = (
                f"⚠️ [stuck-capped] {role} ({project}) wedged แต่ยังไม่ตาย — "
                f"auto-recover ครบ {STUCK_RECOVER_MAX} ครั้งแล้วยังค้าง เลิก recover "
                f"อัตโนมัติ (กัน loop + pipeline stall) — เช็ค `takkub list` แล้ว "
                f"close + assign ใหม่ถ้าต้องการให้ทำต่อ"
            )
            self._notify_lead(project, msg, kind="stuck-capped")

    def _warn_lead_runaway_pane(self, role: str, project: str, rate_bps: float) -> None:
        """Inject a one-line warning into Lead's input when a teammate pane has
        sustained unusually high PTY throughput (issue #35 throughput watchdog).

        Does *not* auto-recover: runaway output is not necessarily an agent bug
        (e.g. a build streaming logs). We surface it so Lead can decide whether
        to close the pane or let it continue."""
        lead = self._project_panes(project).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        rate_kb = rate_bps / 1024
        msg = (
            f"⚠️ [runaway-output] {role} pane พ่น output ≈ {rate_kb:.0f} KB/s "
            f"ต่อเนื่อง > {int(RUNAWAY_DURATION_S)}s — อาจติดลูป. "
            f"ตรวจสอบ pane /{role} หรือ `takkub close --role {role}` ถ้าต้องการหยุด"
        )
        self._notify_lead(project, msg, kind="runaway-output")
        _log_event("runaway_pane_warn", role=role, project=project, rate_kb=int(rate_kb))

    def _warn_lead_over_cap(self, role: str, project: str) -> None:
        """Best-effort advisory: when a fresh teammate spawn pushes the TOTAL
        live pane count (across *all* projects) over what the machine can
        comfortably run (``exec_mode.machine_total_pane_cap()``), drop a one-line
        notice into the spawning project's Lead. **Non-blocking** — the spawn
        always proceeds; this only flags oversubscription so the Lead can split
        the batch into waves or close idle panes.

        machine_fanout_cap() is *per role* and ceilinged at MAX_FANOUT, so it
        can't catch the case where every role is within its per-role cap yet the
        aggregate (e.g. frontend#1..#3 + backend#1..#3 = 6 panes) overwhelms a
        small box. This total guard does (see docs/reviews/2026-06-30-queue-gap.md).

        Rate-limited machine-wide (OVERCAP_WARN_COOLDOWN_S) so a burst of fan-out
        assigns doesn't spam. Wrapped so a warning can never break spawning.
        """
        try:
            if role == LEAD.name:
                return  # spawning a Lead pane is never an over-capacity event
            from . import exec_mode  # local import: matches the toggle handler's pattern

            cap = exec_mode.machine_total_pane_cap()
            # The pane being spawned isn't alive yet, so `active` is the count
            # BEFORE it — active >= cap means this fresh spawn is the (cap+1)-th.
            active = _count_active_teammates(self._panes_by_project)
            if active < cap:
                return
            now = time.time()
            last = getattr(self, "_last_overcap_warn_ts", 0.0)
            if (now - last) < OVERCAP_WARN_COOLDOWN_S:
                return
            self._last_overcap_warn_ts = now
            lead = self._project_panes(project).get(LEAD.name)
            if not (lead and lead.session and lead.session.is_alive):
                return
            msg = (
                f"⚠️ [over-capacity] กำลังเปิด pane teammate ตัวที่ {active + 1} — "
                f"เกินที่เครื่องนี้รับไหวสบายๆ (~{cap} panes) · เสี่ยงช้า/ค้าง/RAM พุ่ง. "
                f"พิจารณาแบ่งงานเป็น waves หรือ `takkub close` pane ที่ไม่ใช้แล้ว"
            )
            self._notify_lead(project, msg, kind="over-capacity")
            _log_event("over_capacity_warn", role=role, project=project, active=active, cap=cap)
        except Exception:
            # A capacity advisory must never prevent a pane from spawning.
            pass

    # ── Fan-out queue (flag-gated, default OFF — TAKKUB_QUEUE_FANOUT) ─────────
    # The over-capacity advisory above only *warns*. With the flag on, this queue
    # actually *defers* a fresh teammate spawn that would exceed the machine's
    # total-pane budget and spawns it later when a pane frees a slot — turning the
    # Lead's self-limited fan-out into a machine-enforced wave executor. Default
    # off so the cockpit's spawn behaviour is 100% unchanged until the operator
    # opts in. See docs/reviews/2026-06-30-queue-gap.md.

    def _should_queue_assign(self, role_name: str, project: str | None) -> bool:
        """True iff this assign should be deferred to the fan-out queue rather
        than spawned now: the flag is on, it's a *new* teammate pane (not a
        re-assign to a live one, never Lead), and the machine is already at/over
        its total-pane budget."""
        if not _fanout_queue_enabled():
            return False
        base_role, _ = _split_shard(role_name)
        if base_role == LEAD.name or role_name == LEAD.name:
            return False
        project_ns = self._resolve_project(project)
        existing = self._project_panes(project_ns).get(role_name)
        if (
            existing is not None
            and getattr(existing, "session", None) is not None
            and getattr(existing.session, "is_alive", False)
        ):
            # Re-assigning a new task to an already-running pane spawns nothing,
            # so it must never be queued (queuing it would strand the task).
            return False
        from . import exec_mode

        cap = exec_mode.machine_total_pane_cap()
        return _count_active_teammates(self._panes_by_project) >= cap

    def _enqueue_assign(
        self,
        role_name: str,
        cwd: str | None,
        task: str,
        requires_commit: bool,
        auto_chain: bool,
        shard_total: int,
        plan: bool,
        isolation: str,
        project: str | None,
        feature: str = "",
        model: str | None = None,
        provider: str | None = None,
        effort: str | None = None,
    ) -> tuple[bool, str]:
        """Park an over-cap assign on the per-project queue and tell the Lead.
        Replayed verbatim by `_drain_fanout_queue` once a slot frees, so every
        flag (commit gate, auto-chain, shards, plan, isolation, feature,
        per-assign model/provider/effort) survives unchanged."""
        project_ns = self._resolve_project(project)
        q = getattr(self, "_fanout_queue", None)
        if q is None:
            self._fanout_queue = q = {}
        q.setdefault(project_ns, collections.deque()).append(
            {
                "role": role_name,
                "cwd": cwd,
                "task": task,
                "requires_commit": requires_commit,
                "auto_chain": auto_chain,
                "shard_total": shard_total,
                "plan": plan,
                "isolation": isolation,
                "project": project,
                "feature": feature,
                "model": model,
                "provider": provider,
                "effort": effort,
            }
        )
        depth = len(q[project_ns])
        self._save_fanout_queue(project_ns)  # survive a restart with work still queued
        _log_event("assign_queued", role=role_name, project=project_ns, queue_depth=depth)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if lead and lead.session and lead.session.is_alive:
            try:
                from . import exec_mode

                cap = exec_mode.machine_total_pane_cap()
            except Exception:
                cap = 0
            msg = (
                f"⏳ [queued] {role_name} เข้าคิวรอ slot ว่าง (เครื่องเต็ม ~{cap} panes) — "
                f"คิวตอนนี้ {depth} งาน · จะ spawn อัตโนมัติเมื่อมี pane done/close"
            )
            _q_sess = lead.session
            _q_sess.write(msg)
            _delayed_enter(lead, _q_sess, 150)
            self.leadInjected.emit(msg)
        return True, f"{role_name} queued (machine at capacity; {depth} in queue)"

    def _drain_fanout_queue(self, project: str | None) -> None:
        """Pop ONE pending assign for `project` and run it, if the flag is on,
        the queue is non-empty, and a slot is now free. Scheduled (deferred via
        singleShot) after a genuine teammate close so it never re-enters the
        close/emit stack. One slot freed → one dequeue; the next close drains the
        next. Best-effort: a failure here must not break close()."""
        try:
            if not _fanout_queue_enabled():
                return
            project_ns = self._resolve_project(project)
            q = getattr(self, "_fanout_queue", None)
            queue = q.get(project_ns) if q else None
            if not queue:
                return
            from . import exec_mode

            cap = exec_mode.machine_total_pane_cap()
            if _count_active_teammates(self._panes_by_project) >= cap:
                return  # still full — leave the item queued for the next close
            item = queue.popleft()
            self._save_fanout_queue(project_ns)  # persist the shrunk queue
            _log_event(
                "assign_dequeued", role=item["role"], project=project_ns, remaining=len(queue)
            )
            # Replay through the normal assign() path. Its own gate re-checks
            # capacity (now below cap) and proceeds to spawn rather than re-queue.
            self.assign(
                item["role"],
                item["cwd"],
                item["task"],
                requires_commit=item["requires_commit"],
                auto_chain=item["auto_chain"],
                shard_total=item["shard_total"],
                plan=item["plan"],
                isolation=item.get("isolation", "shared"),
                project=item["project"],
                feature=item.get("feature", ""),
                model=item.get("model"),
                provider=item.get("provider"),
                effort=item.get("effort"),
            )
            # The queue itself was an auto-chain blocker. Re-evaluate after
            # dequeue: a successful replay now has pane state to block on; a
            # failed replay (or another queued item) is handled correctly too.
            if item.get("auto_chain"):
                getattr(self, "_maybe_fire_auto_chain_handoff", lambda *_: None)(project_ns, True)
        except Exception:
            pass

    # ── Fan-out queue durability (mirrors _save/_load_pending_done_notices) ──

    def _fanout_queue_path(self, project_ns: str) -> pathlib.Path:
        return RUNTIME_DIR / f"fanout-queue-{project_ns}.json"

    def _save_fanout_queue(self, project_ns: str) -> None:
        """Persist one project's pending queue so a still-queued assign survives
        a cockpit restart. Empty queue → remove the file. Best-effort."""
        try:
            q = getattr(self, "_fanout_queue", None)
            items = list(q.get(project_ns, [])) if q else []
            path = self._fanout_queue_path(project_ns)
            if items:
                ensure_runtime()
                _write_json_atomic(path, items)
            elif path.exists():
                path.unlink()
        except Exception:
            pass

    def _load_fanout_queue(self) -> None:
        """Reload persisted per-project queues on startup — only when the flag is
        on, so a stale file from a prior flag-on session is ignored once the
        feature is turned off. Best-effort; a corrupt file is skipped."""
        try:
            if not _fanout_queue_enabled():
                return
            runtime = RUNTIME_DIR
            if not runtime.exists():
                return
            prefix = "fanout-queue-"
            for p in runtime.glob(f"{prefix}*.json"):
                try:
                    proj = p.stem[len(prefix) :]
                    items = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(items, list) and items:
                        if getattr(self, "_fanout_queue", None) is None:
                            self._fanout_queue = {}
                        self._fanout_queue[proj] = collections.deque(items)
                except Exception:
                    continue
        except Exception:
            pass

    def _maybe_submit_stuck_paste(
        self, key: str, role: str, project: str, pane: AgentPane, now: float
    ) -> None:
        """Submit a task paste whose Enter was swallowed and never recovered.

        Fires when a "working" pane sits at its ready prompt with a
        "[Pasted text +N lines]" placeholder in the input box (structural
        signal: ``shows_pending_input()``) for STUCK_PASTE_SUBMIT_AFTER_S —
        the state a swallowed submit leaves behind once
        ``_delayed_enter_verified`` has exhausted its resends (~3 s window,
        too short under parallel-spawn CPU load). Sends a bare CR, which is
        harmless if the submit is actually mid-flight, and retries on a
        cooldown up to STUCK_PASTE_SUBMIT_MAX times so a pane a CR cannot fix
        (e.g. wedged upstream TUI) is escalated to the log instead of poked
        forever. State lives in PaneState and resets the moment the pending
        input clears (submit landed → pane goes busy)."""
        ps = self._ps(key)
        try:
            stuck = pane.session.is_at_ready_prompt() and pane.session.shows_pending_input()
        except Exception:
            stuck = False
        if not stuck:
            ps.pending_input_since = None
            ps.pending_submit_attempts = 0
            return
        if ps.pending_input_since is None:
            ps.pending_input_since = now
            return
        if (now - ps.pending_input_since) < STUCK_PASTE_SUBMIT_AFTER_S:
            return
        if (now - ps.last_pending_submit_ts) < STUCK_PASTE_SUBMIT_COOLDOWN_S:
            return
        if ps.pending_submit_attempts >= STUCK_PASTE_SUBMIT_MAX:
            return
        pane.session.write(b"\r")
        ps.last_pending_submit_ts = now
        ps.pending_submit_attempts += 1
        _log_event(
            "stuck_paste_submit",
            role=role,
            project=project,
            attempt=ps.pending_submit_attempts,
            stuck_for_s=int(now - ps.pending_input_since),
        )
        if ps.pending_submit_attempts >= STUCK_PASTE_SUBMIT_MAX:
            # CRs aren't landing — leave a loud breadcrumb for the operator
            # instead of silently giving up (no-silent-caps rule).
            _log_event("stuck_paste_gave_up", role=role, project=project)

    def _rate_limit_suppressed(self, project: str, role: str, pane: AgentPane, now: float) -> bool:
        """Return True if `pane` is rate-limited and the watchdog should leave
        it alone until the limit resets.

        On first detection it records the reset epoch and schedules a one-shot
        notice to the Lead (option A: notify only, no auto-resume). Once the
        reset time passes the state is cleared and the watchdog resumes."""
        key = f"{project}::{role}"
        _ps_rl = getattr(self, "_pane_state", {}).get(key)
        existing = _ps_rl.rate_limited_until if _ps_rl is not None else 0.0
        if existing > 0.0:
            if now < existing:
                return True
            # Reset time reached — clear and let the watchdog behave normally.
            # The notice fires from its own QTimer scheduled at detection time.
            if _ps_rl is not None:
                _ps_rl.rate_limited_until = 0.0
            return False

        if pane.session is None or not pane.session.is_alive:
            return False
        provider = getattr(pane.model, "provider_name", None) or "claude"
        reset_at = pane.session.rate_limit_reset_at(provider)
        if reset_at is None:
            return False

        marker = ""
        try:
            marker = pane.session.quota_stall_marker(provider) or ""
        except Exception:
            marker = ""
        ps = self._ps(key)
        ps.rate_limited_until = reset_at
        ps.quota_marker = marker
        ps.quota_provider = provider
        self._schedule_rate_limit_notice(project, role, reset_at)
        _log_event(
            "rate_limit_detected",
            role=role,
            project=project,
            provider=provider,
            marker=marker,
            resets_in_s=int(max(0, reset_at - now)),
        )
        # #301: tell Lead the instant a pane hits quota — the reset-time
        # notice above only fires hours later when the window lifts, so
        # without this Lead has no signal at all that a "working" pane is
        # actually frozen until it either notices `takkub status` staying
        # stale or the user reports it by hand (real incident, issue #301:
        # a gemini/agy pane silently downgraded Pro→Flash and kept going
        # while Lead's own status read "working / progress 2s ago").
        # Best-effort (try/except): must never let a Lead-delivery failure
        # take down the gate itself, and keeps this call harmless against
        # minimal test fixtures that don't wire the full notify plumbing.
        try:
            self._notify_quota_hit(project, role, provider, reset_at, marker, pane)
        except Exception:
            pass
        # v2.1.198 pairs the banner with an interactive chooser whose
        # preselected option 1 is "Stop and wait for limit to reset" — confirm
        # it once so the pane waits out the window and auto-resumes, instead of
        # blocking on the modal until a human notices. Best-effort: fires only
        # on first detection (this branch), so no repeat-Enter spam.
        try:
            if pane.session.is_at_limit_choice_modal():
                pane.session.write(b"\r")
                _log_event("rate_limit_modal_confirmed", role=role, project=project)
        except Exception:
            pass
        return True

    def _notify_quota_hit(
        self,
        project: str,
        role: str,
        provider: str,
        reset_at: float,
        marker: str,
        pane: AgentPane,
    ) -> None:
        """Inject an immediate `[system]` notice into Lead the moment a pane
        hits its quota/usage limit (#301) — same channel `toggle_provider`
        uses for `[system] <provider> ENABLED/DISABLED`, so it reads as
        cockpit-authored chrome rather than the teammate's own words."""
        human = _human_duration(max(0, reset_at - time.time()))
        model = None
        try:
            model = pane.session.current_model_label(provider)
        except Exception:
            model = None
        model_note = f" · model now: {model}" if model else ""
        marker_note = f' ("{marker}")' if marker else ""
        msg = f"[system] {role} ({provider}) hit quota{marker_note} — resets in {human}{model_note}"
        self._notify_lead(project, msg, kind="quota-hit")

    def _schedule_rate_limit_notice(self, project: str, role: str, reset_at: float) -> None:
        """Fire a single reset notice when the usage limit lifts."""
        delay_ms = max(0, int((reset_at - time.time()) * 1000))
        QTimer.singleShot(delay_ms, lambda: self._emit_rate_limit_reset(project, role))

    def _emit_rate_limit_reset(self, project: str, role: str) -> None:
        """Tell the Lead a rate-limited pane's window has reset (notify-only)."""
        key = f"{project}::{role}"
        _ps_rr = self._pane_state.get(key)

        # De-dupe guard: if rate_limited_until is already 0, a previous timer
        # for the same episode already handled the reset — skip silently.
        if _ps_rr is None or _ps_rr.rate_limited_until == 0.0:
            _log_event(
                "rate_limit_reset_skipped",
                role=role,
                project=project,
                reason="already_handled",
            )
            return

        # Auto-resume owns parked episodes: its wake timer resumes the teammate
        # and emits the single Lead-facing reset notice.
        if _ps_rr.limit_parked:
            _log_event(
                "rate_limit_reset_skipped",
                role=role,
                project=project,
                reason="auto_resume_parked",
            )
            return

        # Pane-alive guard: if the pane closed while the timer was pending,
        # there is nobody to assign work to — clear state but skip the notice.
        panes = self._project_panes(project)
        target_pane = panes.get(role)
        if target_pane is None or target_pane.session is None or not target_pane.session.is_alive:
            _ps_rr.rate_limited_until = 0.0
            _log_event(
                "rate_limit_reset_skipped",
                role=role,
                project=project,
                reason="pane_gone",
            )
            return

        # Pane is alive — clear state and reset the stuck-watchdog timestamp so
        # the very next tick doesn't see content_static_s >> STUCK_THRESHOLD_S
        # and trigger a spurious close→respawn (#53 fix, must stay here).
        _ps_rr.rate_limited_until = 0.0
        _ps_rr.last_content_change_ts = time.time()

        msg = (
            f"⏰ [rate-limit] {role} ({project}) — usage limit reset แล้ว "
            f"pane พร้อมทำงานต่อ (nudge/มอบงานต่อได้เลย)"
        )
        self._notify_lead(project, msg, kind="rate-limit-reset")
        _log_event("rate_limit_reset", role=role, project=project)
        self.statusChanged.emit()

    def _maybe_surface_tty_block(
        self,
        key: str,
        role: str,
        project: str,
        prompt_line: str,
        now: float,
        *,
        kind: str = "tty",
    ) -> None:
        """Record the TTY block start time and call _surface_tty_block_notice
        once the block has lasted TTY_BLOCK_SURFACE_AFTER_S, then re-surface
        at most every TTY_BLOCK_SURFACE_COOLDOWN_S while still blocked.

        ``kind="permission"`` (#236) is Claude Code's own numbered tool-
        approval dialog rather than a subprocess y/N prompt — same timing
        machinery, different wording so Lead reads the right remedy."""
        ps = self._ps(key)
        if ps.tty_blocked_since is None:
            ps.tty_blocked_since = now
        if (
            now - ps.tty_blocked_since >= TTY_BLOCK_SURFACE_AFTER_S
            and now - ps.last_tty_block_surface_ts >= TTY_BLOCK_SURFACE_COOLDOWN_S
        ):
            self._surface_tty_block_notice(role, project, prompt_line, kind=kind)
            ps.last_tty_block_surface_ts = now

    def _surface_tty_block_notice(
        self, role: str, project: str, prompt_line: str, *, kind: str = "tty"
    ) -> None:
        """Inject a notice into Lead's input when a teammate pane is blocked
        on an interactive prompt (issue #54; #236 for the permission-dialog
        variant).

        Does NOT auto-close or respawn — surface + nudge only. Lead (or the
        operator) decides whether to send a non-interactive flag or manually
        unblock the pane."""
        lead = self._project_panes(project).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        if kind == "permission":
            msg = (
                f"⛔ [{role}] ค้างที่ permission-prompt: '{prompt_line}' — "
                f"tool-permission dialog ของ CLI เอง (ไม่ใช่ subprocess) รอกด 1/2/3 "
                f"หรือ Esc, ถึงแม้เปิด bypass-permissions ไว้ก็ยังหลุดมาเจอได้. "
                f"แก้: เข้าไปดู pane ตรงๆ หรือ "
                f'`takkub send --to {role} "<คำแนะนำ>"` เพื่อปลด block (issue #236)'
            )
        else:
            msg = (
                f"⚠️ [{role}] ค้างรอ input: '{prompt_line}' — "
                f"subprocess รอคำตอบ interactive (y/N, passphrase, 'press any key'). "
                f"แก้: รัน subprocess แบบ non-interactive "
                f"(เช่น `-y`, `--no-input`, `DEBIAN_FRONTEND=noninteractive`) "
                f'หรือ `takkub send --to {role} "<คำแนะนำ>"` เพื่อปลด block'
            )
        self._notify_lead(project, msg, kind=f"tty-block-{kind}")
        _log_event("tty_block_surface", role=role, project=project, prompt=prompt_line, kind=kind)

    def _inject_idle_reminder(
        self,
        project_name: str,
        role_name: str,
        pane: AgentPane,
        notice_round: int,
        *,
        escalate: bool = False,
    ) -> None:
        """Surface an idle reminder without waking a model turn.

        The cockpit-side signal is the primary, cross-platform/provider path
        (#103). Only the explicitly requested one-shot escalation retains the
        historical PTY write+Enter behaviour so a genuinely finished pane can
        still be pushed to report and remain harvestable.
        """
        if pane.session is None or not pane.session.is_alive:
            return
        self.idleReminderNotice.emit(project_name, role_name, notice_round, escalate)
        _log_event(
            "idle_reminder",
            role=role_name,
            project=project_name,
            channel="ui",
            round=notice_round,
            escalated=escalate,
        )
        if not escalate:
            return
        idle_sess = pane.session
        idle_sess.write(IDLE_REMINDER_TEXT)
        _delayed_enter(pane, idle_sess, 150)
        _log_event(
            "idle_reminder_escalation",
            role=role_name,
            project=project_name,
            round=notice_round,
        )

    def _maybe_surface_malformed_xml(
        self, key: str, role: str, project: str, pane: AgentPane, now: float
    ) -> None:
        """Inject a nudge into `pane` if literal tool-call XML is visible on
        screen, indicating the harness silently no-op'd a malformed tool call
        (missing ``antml:`` prefix). Fires at most once per
        MALFORMED_XML_NOTICE_COOLDOWN_S (issue #59)."""
        ps = self._ps(key)
        if now - ps.malformed_xml_notice_ts < MALFORMED_XML_NOTICE_COOLDOWN_S:
            return
        if pane.session is None or not pane.session.is_alive:
            return
        matched = pane.session.has_unparsed_tool_call()
        if matched is None:
            return
        msg = (
            "⚠️ [cockpit] ตรวจพบ tool-call XML ที่ harness parse ไม่ได้ "
            "(หล่น `antml:` prefix) — คำสั่งไม่ได้รันจริงและไม่ถือว่า hang "
            "ลองพิมพ์ tool call ใหม่ให้ใช้ antml:invoke / antml:parameter ให้ครบ"
        )
        _xml_sess = pane.session
        _xml_sess.write(msg)
        _delayed_enter(pane, _xml_sess, 150)
        ps.malformed_xml_notice_ts = now
        _log_event("malformed_tool_call_detected", role=role, project=project)

    def _maybe_surface_done_typed_as_text(
        self, key: str, role: str, project: str, pane: AgentPane, now: float
    ) -> None:
        """#435: an idle teammate whose screen shows `takkub done` as plain
        text (narrated, never executed — gemini qa pane, 2026-08-29: report +
        43 screenshots written, ledger stuck "working", Lead saw nothing)
        gets a one-line nudge to run it for real. Provider-neutral: reads the
        rendered screen, not a transcript. Cooldown `DONE_TEXT_NOTICE_COOLDOWN_S`."""
        ps = self._ps(key)
        if now - ps.done_text_notice_ts < DONE_TEXT_NOTICE_COOLDOWN_S:
            return
        if pane.session is None or not pane.session.is_alive:
            return
        probe = getattr(pane.session, "has_typed_done_text", None)
        matched = probe() if callable(probe) else None
        if not isinstance(matched, str) or not matched:
            return
        msg = (
            "⚠️ [cockpit] เห็น `takkub done` บนจอเป็นข้อความ แต่ orchestrator ไม่ได้รับ report — "
            'ต้องรันเป็นคำสั่ง shell จริงตอนนี้: takkub done "<สรุปงาน 1 บรรทัด + path ไฟล์/ภาพ>" '
            "(ห้ามพิมพ์เป็น text/markdown) Lead ยังไม่เห็นงานของคุณจนกว่าคำสั่งจะรันสำเร็จ"
        )
        _dt_sess = pane.session
        _dt_sess.write(msg)
        _delayed_enter(pane, _dt_sess, 150)
        ps.done_text_notice_ts = now
        _log_event("done_typed_as_text", role=role, project=project, line=matched[:120])

    def close_all_teammates(self, project: str | None = None) -> tuple[bool, str]:
        """Close every non-Lead pane in `project` (defaults to active).
        Used by Lead to reset the board and by the cockpit when a tab is
        closed."""
        project_ns = self._resolve_project(project)
        # #249 item 5: a board reset should sweep away any `takkub wait`
        # registration too — the roles it was watching are about to be
        # closed out from under it, so leaving it behind just makes the
        # next `takkub wait` stumble over a stale registration.
        cancel_wait = getattr(self, "cancel_wait", None)
        if cancel_wait is not None:
            cancel_wait(project_ns)
        names = [n for n in list(self._project_panes(project_ns).keys()) if n != LEAD.name]
        if not names:
            return True, "no teammates to close"
        for n in names:
            self.close(n, project=project_ns)
        return True, f"closed {len(names)} teammate(s): {', '.join(names)}"

    # ──────────────────────────────────────────────────────────────
    # internal: handlers wired from AgentPane signals
    # ──────────────────────────────────────────────────────────────
    def _on_pane_spawn_clicked(self, role_name: str) -> None:
        self.spawn(role_name)

    def _on_pane_close_clicked(self, role_name: str) -> None:
        # Pane-header × (AgentPane's own close button) — a USER click, so it
        # gates on confirm_manual_pane_close(). Every automated close path
        # (CLI `takkub close`, done-report auto-close, close_all_teammates,
        # app shutdown) calls close()/close_all_teammates() directly and
        # never routes through this signal, so automation never blocks on a
        # human click.
        pane = self.sender()
        if not isinstance(pane, AgentPane | HeadlessPane):
            pane = self.panes.get(role_name)
        project_ns = self._project_ns_for_pane(pane) if pane is not None else None
        if project_ns is None:
            project_ns = self._resolve_project(None)
        if not self.confirm_manual_pane_close(pane, role_name, project_ns):
            return
        self.close(role_name)

    def confirm_manual_pane_close(
        self, pane: AgentPaneLike | None, role_name: str, project: str | None
    ) -> bool:
        """Confirm dialog gate for a USER-initiated pane close (pane-header
        × or tab-bar ×) — issue "กันกดปิด pane ผิด". Returns True to proceed
        with the close, False if the user cancelled.

        Must NEVER be called from an automated close path: CLI `takkub
        close`, the done-report auto-close 2.5s after a teammate reports
        done, `close_all_teammates` (board reset / tab close), or app
        shutdown. Those call `close()`/`close_all_teammates()` directly —
        wiring this gate into `close()` itself would hang unattended
        automation waiting for a click that never comes.
        """
        if role_name == LEAD.name:
            # close() already no-ops Lead unless force=True (tab teardown) —
            # a dialog here would just be a confusing extra click on what is
            # already a guaranteed no-op.
            return True

        from PyQt6.QtWidgets import QMessageBox

        from . import cockpit_theme as theme

        project_ns = self._resolve_project(project)
        working = bool(pane is not None and getattr(pane, "state", None) == "working")
        role_obj = getattr(pane, "role", None) if pane is not None else None
        label = getattr(role_obj, "label", None) or role_name

        if working:
            lines = [f"'{label}' กำลังทำงานอยู่ — ปิดตอนนี้จะตัดงานที่กำลังรันทิ้งทันที"]
        else:
            lines = [f"ปิด pane '{label}'?"]

        key = f"{project_ns}::{role_name}"
        ps = getattr(self, "_pane_state", {}).get(key)
        if ps is not None and ps.worktree:
            try:
                from .worktree_manager import WorktreeInfo, WorktreeManager

                info = WorktreeInfo.from_dict(ps.worktree)
                mgr = WorktreeManager()
                if mgr.is_dirty(info):
                    n = mgr.uncommitted_count(info)
                    lines.append(
                        f"⚠ worktree ({info.branch}) มี {n} ไฟล์ที่ยังไม่ commit — จะหายถ้าปิดตอนนี้"
                    )
                commits = mgr.commit_count(info)
                if commits > 0:
                    lines.append(f"⚠ branch {info.branch} มี {commits} commit ที่ยังไม่ได้ merge กลับ")
            except Exception:
                _log_event("confirm_close_worktree_check_error", role=role_name, project=project_ns)

        parent = pane.window() if isinstance(pane, AgentPane) else None
        box = theme.themed_message_box(parent)
        box.setWindowTitle("ปิด pane")
        box.setText("\n".join(lines))
        box.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Ok

    # (#357) A chunk that xterm.js's `onData` forwards can be a REAL keystroke
    # OR a terminal-generated auto-reply to an escape-sequence query the PTY's
    # own OUTPUT just triggered (cursor-position/device-attributes/focus
    # reports) — xterm.js's public `onData` API does not distinguish the two,
    # so both arrive at `_on_pane_input` through the exact same path. Measured
    # live: pasting a Lead Inbox digest/banner/notice makes the target CLI
    # redraw, which queries the terminal (e.g. cursor position) and xterm.js
    # answers automatically — that reply used to be indistinguishable from the
    # owner typing, falsely stamping `_lead_last_user_input_ts` and cutting a
    # `takkub wait` short with "interrupted by user input" seconds after the
    # digest landed, nothing typed. A human cannot type these byte sequences
    # via a keyboard, so a chunk matching ONLY one or more of them — no
    # printable text, no CR/LF — is filtered out here. A chunk with even one
    # real character mixed in still counts as user input (conservative: never
    # suppress a genuine keystroke to catch an auto-reply).
    #   ESC[<row>;<col>R   CPR   — cursor position report
    #   ESC[?...c          DA1   — primary device attributes
    #   ESC[>...c          DA2   — secondary device attributes
    #   ESC[<n>n           DSR   — device status report reply (e.g. ESC[0n)
    #   ESC]...BEL / ESC]...ST   OSC reply (e.g. color-query answers)
    #   ESC[I / ESC[O      focus-in / focus-out reporting
    #   ESC[200~ / ESC[201~      bracketed-paste markers (no content between)
    #   ESC[?<m>;<v>$y     DECRPM mode report (sync-output query ESC[?2026$p
    #                      → ESC[?2026;2$y)                              (#420)
    #   ESC[?<flags>u      kitty keyboard-protocol flags reply          (#420)
    #   ESC[<n>;<r>;<c>t   XTWINOPS size/position report                (#420)
    #   ESC P ... ESC \    DCS reply — XTVERSION (ESC[>q) / DECRQSS      (#420)
    #   ESC[<b;x;yM/m      SGR mouse report (wheel/motion/click) — seen live
    #                      as ESC[<65;51;24M cutting waits short           (#420)
    #   ESC[M<3 bytes>     X10 mouse report                                 (#420)
    # Terminal-structural, not provider- or platform-specific — applies to
    # every pane on every OS this widget runs on.
    #
    # (CodeQL py/redos #31) This used to be one `(?:alt1|...|alt7)+$` regex
    # fullmatched against the whole chunk — a multi-alternative group with
    # its own quantified subpatterns (`\d*`, `[\d;]*`, `[^\x07\x1b]*`)
    # wrapped in an outer `+` is exactly the nested-quantifier shape that
    # can make a backtracking engine explore superlinear-to-exponential
    # splits on a chunk that ALMOST matches but never quite completes.
    # Rewritten as a token scanner instead: `_is_terminal_auto_reply_chunk`
    # below walks the chunk with `re.match` at a fixed, forward-only cursor
    # — each step consumes exactly one token or the whole chunk is
    # rejected, so there is no repetition-boundary ambiguity left for the
    # engine to backtrack over, and the per-token patterns bound every
    # variable-length run (`{0,N}` instead of bare `*`) so even a single
    # token can't itself run away on a very long non-terminated chunk.
    _TERMINAL_AUTO_REPLY_TOKEN_RE = re.compile(
        rb"\x1b\[\d{0,8}(?:;\d{1,8}){0,8}R"
        rb"|\x1b\[\?[\d;]{0,32}c"
        rb"|\x1b\[>[\d;]{0,32}c"
        rb"|\x1b\[\d{0,8}n"
        rb"|\x1b\][^\x07\x1b]{0,4096}(?:\x07|\x1b\\)"
        rb"|\x1b\[[IO]"
        rb"|\x1b\[20[01]~"
        # #420 — replies xterm.js also emits with nobody typing, seen cutting
        # `takkub wait` short 3× in a row against an empty inbox:
        rb"|\x1b\[\??\d{1,8};\d\$y"  # DECRPM mode report (ESC[?2026;2$y)
        rb"|\x1b\[\?\d{0,8}u"  # kitty keyboard-protocol flags reply
        rb"|\x1b\[\d{1,3}(?:;\d{1,8}){0,2}t"  # XTWINOPS report (ESC[8;24;80t)
        rb"|\x1bP[^\x1b]{0,1024}\x1b\\"  # DCS reply (XTVERSION / DECRQSS)
        # #420 (caught live by `lead_user_input_stamp`): SGR mouse reports —
        # wheel/motion/click over the Lead pane while the TUI has mouse
        # tracking on. Scrolling is not a command; it must never end a wait.
        rb"|\x1b\[<\d{1,3};\d{1,5};\d{1,5}[mM]"
        rb"|\x1b\[M[\x20-\xff]{3}"  # X10/normal mouse report (3 payload bytes)
    )

    # A real terminal auto-reply is at most a few dozen bytes (a CPR is
    # ~10, DA1/DA2 responses a bit more) — a chunk this long was never
    # going to be one, so bail before the scanner ever looks at it.
    _TERMINAL_AUTO_REPLY_MAX_LEN = 512

    # #449 forensics: a GENERIC (not just the known auto-reply tokens above)
    # ANSI escape-sequence matcher, used only to build a redacted repr for
    # `lead_input_stamped` below — never to decide filtering. Wide on purpose
    # (any CSI/OSC/DCS shape, or a bare two-byte ESC sequence like arrow-key
    # SS3 codes) so the redaction below can strip every recognizable escape
    # span and leave behind only what a human could actually have typed.
    _ANSI_SEQ_RE = re.compile(
        rb"\x1b\[[0-9;:<>=?]*[A-Za-z~^`]"
        rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
        rb"|\x1bP[^\x1b]*\x1b\\"
        rb"|\x1b[A-Za-z0-9]"
    )

    @classmethod
    def _escape_only_repr(cls, data: bytes, limit: int = 64) -> str:
        """`repr()` of *only* the recognizable escape-sequence bytes in
        *data*, with every other byte dropped — used so a forensic log line
        can show the STRUCTURE of a stamped chunk (which escape sequences it
        contained) without ever risking a leak of whatever the owner was
        actually typing (a password mid-entry, a sensitive path, etc.)."""
        spans = [m.group(0) for m in cls._ANSI_SEQ_RE.finditer(data)]
        return repr(b"".join(spans)[:limit])

    @classmethod
    def _chunk_has_non_escape_content(cls, data: bytes) -> bool:
        """True if *data* still has visible content once every recognizable
        escape sequence is stripped out — i.e. this chunk cannot be fully
        explained as terminal chrome and likely carries real typed/pasted
        text. Used only for the `printable` field on `lead_input_stamped`."""
        remainder = cls._ANSI_SEQ_RE.sub(b"", data)
        return any(b >= 0x20 for b in remainder)

    # (#428/#431) The auto-reply token list above is a denylist — every new
    # TUI feature that queries the terminal (#420 added five in one go)
    # slips through until someone catches it in `lead_user_input_stamp`.
    # Complement it with a structural rule: an ESC-led chunk that arrives
    # within this many seconds of an ENGINE write into the Lead PTY (digest
    # / worktree notice / task paste — the exact moment the TUI redraws and
    # queries the terminal) and AFTER the owner's last real keystroke is
    # treated as a terminal reply, not typing. Printable text and CR/LF
    # still count immediately — a human pressing an arrow key inside this
    # window loses nothing but a wait-interrupt, and the next keystroke
    # re-stamps normally.
    _LEAD_INJECT_GRACE_S = 5.0

    def _is_post_inject_terminal_reply(self, project_ns: str, session, data: bytes) -> bool:
        if data[:1] != b"\x1b" or len(data) > self._TERMINAL_AUTO_REPLY_MAX_LEN:
            return False
        if b"\r" in data or b"\n" in data:
            return False
        last_write = float(getattr(session, "last_write_ts", 0.0) or 0.0)
        last_user = self._lead_last_user_write_ts.get(project_ns, 0.0)
        if last_write <= last_user:
            return False  # nothing engine-written since the owner last typed
        return (time.time() - last_write) <= self._LEAD_INJECT_GRACE_S

    @classmethod
    def _is_terminal_auto_reply_chunk(cls, data: bytes) -> bool:
        """True iff `data` is nothing but one-or-more terminal auto-reply
        tokens back to back — same semantics the old `_TERMINAL_AUTO_REPLY_RE.
        fullmatch(data)` gave, computed by scanning instead of backtracking
        (see the comment above `_TERMINAL_AUTO_REPLY_TOKEN_RE`)."""
        if not data or len(data) > cls._TERMINAL_AUTO_REPLY_MAX_LEN:
            return False
        pos = 0
        end = len(data)
        while pos < end:
            match = cls._TERMINAL_AUTO_REPLY_TOKEN_RE.match(data, pos)
            if match is None:
                return False
            pos = match.end()
        return True

    def _on_pane_input(self, role_name: str, data: bytes) -> None:
        # Route the keystrokes to the pane that ACTUALLY emitted them, not to
        # `self.panes[role_name]`. `self.panes` resolves to the *active project*
        # only — a single-project-era assumption that predates multi-tab support.
        # With several project tabs open, every project's same-role pane (e.g.
        # both Leads) is wired to this one slot, so role-name lookup sent input
        # into whichever project happened to be active — misdelivering keystrokes
        # (incl. Shift+Tab, which cycles claude's permission mode) into the wrong
        # pane. Qt's sender() is the emitting AgentPane, so it is project-correct
        # by construction. Fall back to the role lookup for direct/test calls
        # where there is no signal sender.
        pane = self.sender()
        if not isinstance(pane, AgentPane | HeadlessPane):
            pane = self.panes.get(role_name)
        if pane is None or pane.session is None:
            return
        # Feed the Lead draft-typing guard (#3) with every keystroke the
        # Lead pane's terminal actually emits — engine-originated writes
        # (done notices, CC flushes, …) go straight to session.write() and
        # never pass through here, so they can't feed the tracker.
        # (#357) ...except a chunk that is ONLY a terminal auto-reply (no
        # real keystroke in it at all) — see `_TERMINAL_AUTO_REPLY_TOKEN_RE`'s
        # comment above. Still forwarded to the PTY exactly as before either
        # way; only the draft/user-input tracking is skipped for it.
        user_project_ns: str | None = None
        if pane.role.name == LEAD.name and not self._is_terminal_auto_reply_chunk(data):
            project_ns = self._project_ns_for_pane(pane)
            if project_ns is not None and self._is_post_inject_terminal_reply(
                project_ns, pane.session, data
            ):
                _log_event(
                    "lead_user_input_suppressed_post_inject",
                    project=project_ns,
                    size=len(data),
                    sample=repr(data[:64]),
                )
                pane.session.write(data)
                return
            if project_ns is not None:
                user_project_ns = project_ns
                self._track_lead_draft_input(project_ns, data)
                # #265: any byte here is proven to be the owner actually
                # typing (see this method's comment above and
                # `_lead_last_user_input_ts`'s own docstring) — stamp it so
                # `LeadWaitMixin.poll_wait` can wake early instead of
                # leaving the owner queued behind up to --timeout of
                # teammate polling.
                self._lead_last_user_input_ts[project_ns] = time.time()
                self._lead_last_user_input_printable[project_ns] = (
                    self._chunk_has_non_escape_content(data)
                )
                # #449 forensics: logged on EVERY stamp, not just an ESC-led
                # one (the old `lead_user_input_stamp` below #420 only fired
                # for those) — a `takkub wait` cut short with "interrupted by
                # user input" while the owner typed nothing was otherwise
                # undiagnosable: nothing on record said WHAT stamped the
                # timestamp. `escape_repr` never carries plain typed text
                # (see `_escape_only_repr`), so this is safe to log
                # unconditionally.
                _log_event(
                    "lead_input_stamped",
                    project=project_ns,
                    chunk_len=len(data),
                    printable=self._chunk_has_non_escape_content(data),
                    escape_repr=self._escape_only_repr(data),
                )
        pane.session.write(data)
        if user_project_ns is not None:
            # Stamped AFTER the write so the owner's own keystroke never
            # reads as "an engine write newer than the last user write" in
            # `_is_post_inject_terminal_reply`.
            self._lead_last_user_write_ts[user_project_ns] = time.time()
