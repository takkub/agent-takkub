"""Lead-inbox queue cluster — ready-prompt-aware serialised delivery to Lead.

Extracted from orchestrator.py (lead_notify_pump cluster, refactor round 5).
Provides ``LeadInboxMixin`` which ``Orchestrator`` inherits so all queue
ownership stays in one module.

Also houses the pane submit helpers (_delayed_enter / _delayed_enter_verified)
that the cluster methods depend on and that were previously module-level in
orchestrator.py.

Also houses the Lead draft-typing guard (issue #3, 2026-07-09 core-upgrade
plan): `_track_lead_draft_input` / `_lead_can_accept_injection` /
`_lead_draft_hold_expired`, backed by the pure state machine in
`lead_draft_state.py`. `Orchestrator._on_pane_input` feeds every Lead-pane
keystroke into the tracker; `_pump_lead_notify` and `_flush_pending_lead_cc`
gate through the same `_lead_can_accept_injection()` so a done-notice/CC
paste never lands on top of the user's unsubmitted draft.

Layer rule (enforced by import-linter "lead-inbox-layer" contract):
  lead_inbox MUST NOT import orchestrator / main_window / app / cli.

State ownership rule: _lead_notify_queue, _lead_digest_queue, _digest_timer,
_pending_lead_cc, _pending_done_notices, _lead_notify_pumping,
_lead_notify_retry, _lead_draft_state MUST stay in Orchestrator.__init__.  This mixin only
defines methods — never the initial dict assignments — so queue ownership
stays centralised and divergence bugs cannot creep back in.
"""

from __future__ import annotations

import collections
import inspect
import json
import os
import pathlib
import re
import sys as _sys
import time
from collections.abc import Callable
from datetime import datetime

from PyQt6.QtCore import QProcess, QTimer

from .agent_pane import AgentPane
from .config import RUNTIME_DIR as _RUNTIME_DIR_DEFAULT
from .config import ensure_runtime as _ensure_runtime_default
from .digest_facts import DigestFacts, format_digest_fact_line
from .lead_draft_state import (
    LeadDraftState,
    advance_draft_state,
    draft_hold_expired,
    draft_state_allows_injection,
)
from .orchestrator_text import (
    _enter_delay_ms,
    _exit_key,
    _log_event,
    _notice_fingerprint,
    _paste_payload,
    _sanitize_pane_text,
)
from .pipeline_executor import _split_shard
from .pty_session import PtySession, WritePriority
from .roles import LEAD
from .task_delivery import DeliveryManager


def _orch_attr(name: str, default):
    """Read an attribute from the orchestrator module facade at call time.

    Tests patch agent_takkub.orchestrator.<name>; using _orch_attr lets those
    patches propagate into lead_inbox methods without a top-level import cycle.
    Falls back to *default* when orchestrator is not yet in sys.modules.
    """
    m = _sys.modules.get("agent_takkub.orchestrator")
    return getattr(m, name, default) if m is not None else default


# ── Submit helpers (moved from orchestrator.py module scope) ─────────────────

# Self-healing submit constants (see _delayed_enter_verified docstring).
_SUBMIT_VERIFY_GRACE_MS = 600
_SUBMIT_MAX_RESENDS = 3
# Separate, much larger budget for the "pane not-ready + paste still visibly
# pending" case — a legitimately busy/booting pane (codex/agy cold-booting its
# MCP servers), NOT a swallowed CR. The small _SUBMIT_MAX_RESENDS budget
# (~1.8 s) is meant for the swallow/repaste path where being aggressive risks
# duplicate pastes; sharing it with the boot case meant that when ≥3 codex
# panes were spawned together, the later ones were still MCP-booting when the
# 3-resend budget ran out, so their task CR was abandoned and the pointer +
# auto-reminders piled up unsubmitted in the composer (observed 2026-07-13).
# 150 × 600 ms ≈ 90 s — matches the codex/agy ready-wait window (_ready_wait_ms)
# so a boot that finishes anytime within that window still gets its CR. Nudging
# stops the instant the paste leaves the composer (submit landed), so a normal
# boot resends only a handful of times; the cap only bounds a wedged pane.
_SUBMIT_BUSY_MAX_RESENDS = 150

# Render-settle guard for the repaste branch (duplicate-paste spam fix).
# Under parallel-spawn CPU load claude renders its `[Pasted text +N lines]`
# placeholder slower than _SUBMIT_VERIFY_GRACE_MS, so the input box momentarily
# reads EMPTY and the self-heal mistakes a still-painting paste for a swallowed
# one — repasting up to _SUBMIT_MAX_RESENDS times (the visible "4× [Pasted
# text]" stack). A pane still rendering is producing output, so before repasting
# we re-poll while output stayed recent within _RENDER_ACTIVE_S, bounded by
# _RENDER_WAIT_MAX cycles. A truly swallowed paste leaves the pane idle (no
# recent output) → skips the wait and repastes at once, preserving the #26 fix.
_RENDER_ACTIVE_S = 1.0
_RENDER_WAIT_MAX = 6

# Stall-aware deferral (#133). A fan-out of concurrent pane spawns backlogs
# the Qt event loop for ~1s at a time (proven via events.log: main_thread_stall
# episodes of 938-1141ms landing right before bursts of duplicate repaste
# events). Every already-scheduled verify() timer queued during that backlog
# fires in a burst the instant it clears, collapsing what was meant to be
# several REAL seconds of render-settle grace into a few milliseconds of
# wall-clock time — so a paste that was simply still painting under CPU
# contention gets misread as swallowed. `_main_thread_heartbeat_age()` (see
# orchestrator.set_main_thread_heartbeat_probe) reads live Qt heartbeat
# staleness: if verify() is running at all the main thread is free RIGHT NOW,
# but an elevated age proves a backlog just cleared — the exact moment a
# burst-fired verify must not trust its own timing. Deferring once more here
# costs nothing when nothing was actually swallowed and gives production the
# real elapsed time it was supposed to have. Bounded (not indefinite) so a
# permanently-stale probe (a bug in the probe itself) can't wedge the chain.
_STALL_DEFER_AGE_S = 0.5
_STALL_DEFER_MAX = 4

# Ready-prompt poll cadence for _send_when_ready (task delivery). Lowered from
# 1000/500 → 300/150 so a task lands almost as soon as the pane hits idle — the
# old 1 s lead-in + 500 ms poll added up to ~1.5 s of avoidable lag even on an
# already-idle pane. elapsed[] still accumulates by the poll interval so the 45 s
# hard timeout stays wall-clock accurate.
_READY_POLL_FIRST_MS = 300
_READY_POLL_INTERVAL_MS = 150
# #186: bounded grace _deliver() keeps waiting instead of blind-pasting a
# task into an active trust/onboarding modal or shell prompt once its normal
# hard timeout fires. A blind paste into a modal lands as keystrokes on the
# modal, not the composer — a strictly worse loss than the ordinary
# best-effort blind paste this branch exists for — so give a still-running
# _auto_trust responder (or a human) a further short window to clear it
# first. Short relative to STALL_THRESHOLD_SEC (300s default) so it barely
# adds wait time in the common case; this only even engages once the pane is
# ALREADY past its delivery timeout and STILL on a recognised prompt.
_PROMPT_BLOCK_DEFER_CEILING_MS = 30_000

# #248/#247 round 2 follow-up: an auth-failure marker must be seen on this
# many CONSECUTIVE polls before `_check` blind-delivers — a single poll that
# happens to land while the footer region is mid-redraw (or still showing
# stale text from a screen transition) must not be enough to convict. Small
# relative to `_READY_POLL_INTERVAL_MS` (150ms) — 5 polls is ~750ms, trivial
# next to the ceiling this whole fast-fail path exists to beat
# (BUSY_WAIT_CEILING_SEC, 1800s) — but enough to filter a one-frame render
# artifact. A pane genuinely stuck on auth keeps showing the marker on every
# subsequent poll, so this costs it nothing.
_AUTH_FAILURE_CONFIRM_POLLS = 5


def _prompt_block_reason(session) -> str | None:
    """Return "trust" / "permission" / "tty" if *session* is currently
    sitting on an interactive prompt that needs a keypress before it can
    accept a task paste, else None.

    This is a state `_send_when_ready` must treat as distinct from "busy
    generating" — a modal that periodically redraws (a spinner, agy's
    boot-phase "verifying your account" text) still advances
    `seconds_since_output()`, so before this it silently fell into the
    ordinary #144 busy-wait extension and the Lead heard nothing concrete
    for up to BUSY_WAIT_CEILING_SEC (issue #186). "permission" (#236) is
    checked before the generic "tty" catch-all so Claude Code's own
    numbered tool-approval menu — which has no [y/N] bracket for the tty
    regex to match — gets its own accurate wording instead of being
    mislabelled a shell prompt."""
    try:
        if session.is_at_trust_prompt():
            return "trust"
        if session.is_blocked_on_permission_prompt():
            return "permission"
        if session.is_blocked_on_tty_prompt():
            return "tty"
    except Exception:
        pass
    return None


# Claude roles that load MCP servers need the same cold-boot allowance as the
# slower provider CLIs.  Claude's base provider spec intentionally stays at
# 45 s because roles without MCPs normally render their prompt well inside
# that window; the role-aware extension is applied in _ready_wait_ms.
_MCP_READY_WAIT_MS = _SUBMIT_BUSY_MAX_RESENDS * _SUBMIT_VERIFY_GRACE_MS

# After this many consecutive 400-ms busy-retries (~30 s) the pump gives up
# and spills remaining items to the durable _pending_done_notices queue.
# Prevents unbounded memory growth and ensures delivery survives a crash that
# occurs while Lead is alive-but-wedged.
LEAD_NOTIFY_BUSY_CAP = 75

# Clean done notices and peer CCs are deliberately held for a short window so a
# parallel burst wakes Lead once instead of once per teammate.  Read the env at
# enqueue time (rather than only at import) so cockpit launches and tests can
# configure the policy without rebuilding this module.
#
# #264: was 60_000. A prod user described the cockpit as feeling like it
# "always hangs on something" — traced to this constant applying in full
# even to a solo done() with nothing else in flight to combine with (see
# `_INBOX_DIGEST_ADAPTIVE_SETTLE_MS` below for that half of the fix).
# Lowered to the top of the issue's own 10-15s suggested range rather than
# the bottom: a genuine parallel fan-out (the case this window exists to
# serve) can have real completion spread of several seconds under load, and
# 15s still cuts the worst-case wait 4x versus the old value while keeping
# more margin for that spread than 10s would.
_INBOX_DIGEST_WINDOW_MS = 15_000

# #264: used INSTEAD of the full window above once a notice's own enqueue
# finds no OTHER role in the project still in a non-terminal state (see
# `_DIGEST_TERMINAL_PANE_STATES`) — i.e. nothing else is plausibly about to
# join this burst, so there is nothing left to wait for. Not 0: a burst can
# still be "in flight" for a few seconds even after every role's pane looks
# terminal (e.g. two shards' done() calls landing a beat apart under load),
# so a short settle still catches that without paying the full window's
# tax on the common solo-completion case.
_INBOX_DIGEST_ADAPTIVE_SETTLE_MS = 3_000

# States from which a role can never produce a NEW report without a fresh
# assign — mirrors `lead_wait._WAIT_TERMINAL_PANE_STATES`. Duplicated
# rather than imported to avoid coupling this module to LeadWaitMixin's
# layer (see both modules' "Layer rule" header comments); both sets are
# small, stable, and mean the same thing here as there.
_DIGEST_TERMINAL_PANE_STATES = frozenset({"empty", "done", "exited", "error"})

# Staleness escalation for the durable reaper (#70). When spilled done-notices
# can't be flushed because the Lead reads as not-ready for this long, the
# reaper force-delivers them anyway — guarding against an is_at_ready_prompt()
# false-negative (a blocker marker in the Lead's visible conversation makes an
# idle Lead read as busy → notices stranded forever, the #70 multi-project
# stall). 60 s ≫ a real Lead turn, so a genuinely-busy Lead is rarely hit; if it
# is, claude buffers the pasted input and processes it next.
_DONE_NOTICE_STALE_S = 60.0


def _inbox_digest_window_ms() -> int:
    """Return the configured Lead-inbox digest window.

    Invalid values fall back to the production default; negative values behave
    like ``0`` (legacy immediate delivery).
    """
    raw = os.environ.get("TAKKUB_INBOX_DIGEST_MS")
    if raw is None:
        return _INBOX_DIGEST_WINDOW_MS
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _INBOX_DIGEST_WINDOW_MS


_DONE_NOTICE_RE = re.compile(r"^\[([^\]\r\n]+?)\s+done\](?:\s+(.*))?$", re.DOTALL)
_CC_NOTICE_RE = re.compile(
    r"^\[CC\]\s*\[([^\]\r\n]+?)\s*→\s*([^\]\r\n]+?)\](?:\s+(.*))?$",
    re.DOTALL,
)
_FAILED_NOTICE_RE = re.compile(r"\[[^\]\r\n]*\bFAILED\b[^\]\r\n]*\]", re.IGNORECASE)
# delivery-unconfirmed reviewed for #130 (was firing on busy-not-stuck panes,
# see _check's seconds_since_output gate above _warn_lead_delivery_unconfirmed)
# and kept here: now that it only fires for a pane genuinely silent past
# STALL_THRESHOLD_SEC, it's a real "task may not have landed" signal that
# deserves to jump the digest queue, same as spawn-failed.
# spawn-stuck (#140): an assign that fell into the spawn FIFO queue used to
# produce no notice at all — only an outright spawn *failure* warned via
# spawn-failed. A queued assign stuck past SPAWN_QUEUE_STUCK_SEC
# (spawn_engine._check_spawn_queue_stuck) is the same "task may not have
# landed" class of signal and jumps the queue for the same reason.
# delivery-boot-stall (#254): same "task may not have landed" class as the
# three above — fires while the target pane is still stuck loading a
# provider boot-phase marker (MCP server), well before the ordinary
# delivery timeout would even notice, so it deserves the same jump-the-
# digest-queue urgency rather than waiting behind other mail.
_BLOCKING_NOTICE_MARKERS = (
    "[spawn-failed]",
    "[delivery-unconfirmed]",
    "[spawn-stuck]",
    "[delivery-boot-stall]",
)

# #259: these 4 markers are authored by the orchestrator itself
# (`_warn_lead_*`), not by a teammate's own `done()`/`failed()` call, so
# they carry their target role as plain text right after the marker
# (``⚠️ [delivery-unconfirmed] backend pane ...``) instead of the
# `[role done]`/`[role FAILED]` shape `_notice_role_tag` parses.
# `_notice_role_tag` returns None for these bodies — `inbox_report` then
# falls back to `"system"` for them, which is why `_pending_notice_outside`
# used to skip every one of these notices for every role (watched or not):
# its `role == "system": continue` guard swallowed them before the
# blocking-marker check ever ran. Deliberately a separate helper rather
# than widening `_notice_role_tag` itself: that function's contract feeds
# `_provenance_stale`'s pane_token verification (#228) for genuine
# `done()`-authored bodies, and these system notices are never queued with
# a pane_token to verify — widening it would add an unused, untested code
# path there for no benefit.
#
# #266: also matches `[delivery-busy-wait]` (#144) — same "orchestrator-
# authored, role name in plain text" shape, and one of the notice classes
# #266's own evidence named as arriving after the pane it describes had
# already closed. Not in `_BLOCKING_NOTICE_MARKERS` (it's informational,
# not urgent) but the SAME re-checkable "is this role's pane still alive"
# claim as the 4 blocking markers, so it belongs in this set even though it
# doesn't belong in that one.
_SYSTEM_MARKER_ROLE_RE = re.compile(
    r"\[(?:spawn-failed|delivery-unconfirmed|spawn-stuck|delivery-boot-stall|delivery-busy-wait)\]"
    r"\s+(\S+)",
    re.IGNORECASE,
)


def _system_marker_role(body: str) -> str | None:
    """Extract the target role from a delivery-health system notice, or
    None if *body* doesn't carry one of the 4 markers above. See the
    comment on `_SYSTEM_MARKER_ROLE_RE` for why this is separate from
    `_notice_role_tag`."""
    m = _SYSTEM_MARKER_ROLE_RE.search(body)
    return m.group(1).strip() if m else None


def _is_digestible_lead_notice(body: str) -> bool:
    """True only for the high-volume, non-blocking done/peer-CC paths."""
    stripped = body.strip()
    return bool(_DONE_NOTICE_RE.match(stripped) or _CC_NOTICE_RE.match(stripped))


def _is_immediate_lead_notice(body: str) -> bool:
    """True for notices that require Lead action or sequencing immediately."""
    return _is_blocking_lead_notice(body) or "[auto-chain handoff]" in body.lower()


def _is_blocking_lead_notice(body: str) -> bool:
    """True for failure notices that must jump ahead of digestible mail."""
    lowered = body.lower()
    return bool(
        _FAILED_NOTICE_RE.search(body)
        or any(marker in lowered for marker in _BLOCKING_NOTICE_MARKERS)
    )


_FAILED_TAG_RE = re.compile(r"^\[([^\]\r\n]+?)\s+FAILED\]", re.IGNORECASE)


def _notice_role_tag(body: str) -> str | None:
    """Extract the role name from a `[role done]`/`[role FAILED]`-tagged
    notice body, or None if the body carries no per-pane origin claim.

    Both tags are built in exactly one place each (`Orchestrator.done()` /
    `_build_verify_fail_handoff`), so a match here always corresponds to a
    real `from_role` a caller can attach a `pane_token` to (#228)."""
    stripped = body.strip()
    m = _DONE_NOTICE_RE.match(stripped)
    if m:
        return m.group(1).strip()
    fm = _FAILED_TAG_RE.match(stripped)
    if fm:
        return fm.group(1).strip()
    return None


def _unwrap_notice_item(item) -> tuple[str, str | None, float | None]:
    """Normalise one `_lead_digest_queue`/`_lead_notify_queue` entry to
    ``(body, pane_token, queued_ts)``.

    `_lead_digest_queue` entries (#241) are `(body, pane_token, queued_ts)`
    3-tuples — `queued_ts` is `time.time()` at enqueue, used to show digest
    items' age instead of leaving Lead to guess whether a line is fresh or
    stale. `_enqueue_live_lead_notice` entries are `(body, pane_token)`
    2-tuples (`queued_ts` reads back `None` — the live queue delivers
    promptly enough that staleness isn't a concern there). A bare string
    (legacy call sites, hand-built test fixtures) means no origin or
    timestamp was recorded."""
    if isinstance(item, tuple):
        if len(item) >= 3:
            return item[0], item[1], item[2]
        if len(item) == 2:
            return item[0], item[1], None
        return (item[0] if item else "", None, None)
    return item, None, None


def _unwrap_notice_facts(item) -> DigestFacts | None:
    """Pull the optional 4th ``DigestFacts`` element off a
    ``_lead_digest_queue`` entry (#245). Every OTHER queue/consumer keeps
    working unchanged against the 3-tuple `_unwrap_notice_item` already
    returns — this is additive, read only where the digest actually renders
    a bullet. Absent (2-/3-tuple entries, or a bare string) means the caller
    falls back to parsing `body` as prose, same as before #245."""
    if isinstance(item, tuple) and len(item) >= 4:
        return item[3]
    return None


_STALE_ORIGIN_BANNER = (
    "⚠️ [unverified origin — {role} pane was respawned since this report was "
    "queued; confirm current status with the live pane before acting on it]"
)


def _format_notice_age(now_ts: float, queued_ts: float | None) -> str:
    """Render ' · Xm ago' for a digest item's queue age, or '' when no
    timestamp was recorded (#241 — Lead had no way to tell a fresh digest
    line from one that had been sitting in the debounce window a while,
    short of cross-referencing the report's session-file path by hand)."""
    if queued_ts is None:
        return ""
    age = max(0.0, now_ts - queued_ts)
    if age < 60:
        return f" · {int(age)}s ago"
    if age < 3600:
        return f" · {int(age // 60)}m ago"
    return f" · {int(age // 3600)}h{int((age % 3600) // 60)}m ago"


def _occurred_stamp(queued_ts: float | None, now_ts: float | None = None) -> str:
    """Render a "[HH:MM:SS · age]" prefix (trailing space included) for the
    wall-clock a notice's underlying event actually occurred, or "" when
    unknown. Originally #241's digest-only treatment
    (`_format_digest_item`); #266 extends it to live/durable notices too —
    every notice class had this available in principle (`queued_ts` is
    captured at enqueue time everywhere now), but only the digest queue
    ever rendered it, so a notice that sat through the busy-retry/draft-hold
    chain (#264) before finally landing looked exactly as fresh as one
    delivered instantly."""
    if queued_ts is None:
        return ""
    clock = datetime.fromtimestamp(queued_ts).strftime("%H:%M:%S")
    age = _format_notice_age(now_ts if now_ts is not None else time.time(), queued_ts)
    return f"[{clock}{age}] "


def _format_digest_item(
    body: str,
    queued_ts: float | None = None,
    now_ts: float | None = None,
    facts: DigestFacts | None = None,
) -> str:
    """Render one queued notice in the compact Lead Inbox Digest format.

    Prefixed with a "[HH:MM:SS · age]" stamp whenever `queued_ts` is known
    (#241) so Lead can judge freshness inline instead of guessing whether a
    digest line already went stale while it sat in the debounce window.

    When *facts* is supplied (#245 — `done()` computes it for every clean
    done notice), the bullet is a fact table `format_digest_fact_line`
    renders straight from cockpit-measured values, never from parsing
    `body`. *facts* is None for every other digestible body (peer-CC relays,
    or a caller that predates #245) — those fall back to the original
    regex-on-prose rendering below, unchanged.
    """
    stripped = body.strip()
    stamp = _occurred_stamp(queued_ts, now_ts)

    if facts is not None:
        return format_digest_fact_line(facts, stamp=stamp)

    done_match = _DONE_NOTICE_RE.match(stripped)
    if done_match:
        role, detail = done_match.groups()
        suffix = f": {detail.strip()}" if detail and detail.strip() else ""
        return f"• {stamp}[{role.strip()}] done{suffix}"

    cc_match = _CC_NOTICE_RE.match(stripped)
    if cc_match:
        from_role, to_role, detail = cc_match.groups()
        suffix = f": {detail.strip()}" if detail and detail.strip() else ""
        return f"• {stamp}[CC from {from_role.strip()} -> {to_role.strip()}]{suffix}"

    # Defensive fallback: only digestible bodies should reach this helper, but
    # preserve a notice rather than drop it if a caller changes its format.
    return f"• {stamp}{stripped}"


def _delayed_enter(pane: AgentPane, session: PtySession, delay_ms: int) -> None:
    """Schedule CR into pane after delay_ms, no-op if session has changed.

    Captures the session object at call time so the lambda cannot reach a
    replacement session if the pane is closed and respawned before the timer fires.
    """
    QTimer.singleShot(
        delay_ms,
        lambda: pane.session is session and _safe_session_write(pane.session, b"\r"),
    )


def _safe_session_write(session, data, **metadata) -> bool:
    """Use metadata with real PtySession objects while preserving simple fakes.

    Several compatibility tests and third-party integrations expose only the
    historical ``write(data)`` shape. Production PtySession accepts the safety
    metadata; a TypeError fallback keeps those narrow adapters working.
    """
    has_delivery_guard = any(
        metadata.get(key) is not None for key in ("delivery_id", "session_generation", "expires_at")
    )
    safety_metadata = (
        {key: value for key, value in metadata.items() if value is not None}
        if has_delivery_guard
        else {}
    )
    try:
        result = session.write(data, **safety_metadata) if safety_metadata else session.write(data)
    except TypeError:
        result = session.write(data)
    return result is not False


def _log_verify_decision(
    decision: str,
    *,
    session: PtySession,
    payload: str | None,
    is_ready: bool,
    shows_pending: bool | None = None,
    produced_output: bool | None = None,
    render_waits: int | None = None,
) -> None:
    """#134 diagnostics: record every recovery decision (resend/repaste) with
    the exact signals that drove it. Deliberately fired only at the same 4
    sites that already call ``on_resend``/``on_repaste`` (never per-poll), so
    volume stays proportional to actual recovery events, not verify() ticks."""
    try:
        seconds_since_output = session.seconds_since_output()
    except Exception:
        seconds_since_output = None
    _log_event(
        "task_deliver_verify_decision",
        decision=decision,
        is_ready=is_ready,
        shows_pending=shows_pending,
        produced_output=produced_output,
        seconds_since_output=(
            round(seconds_since_output, 2)
            if isinstance(seconds_since_output, (int, float))
            else None
        ),
        render_waits=render_waits,
        payload_len=len(payload) if payload else 0,
        single_line=("\n" not in payload) if payload else None,
    )


def _delayed_enter_verified(
    pane: AgentPane,
    session: PtySession,
    delay_ms: int,
    *,
    max_resends: int = _SUBMIT_MAX_RESENDS,
    busy_max_resends: int = _SUBMIT_BUSY_MAX_RESENDS,
    on_resend=None,
    payload: str | None = None,
    content_fragment: str = "",
    on_repaste=None,
    on_settled=None,
    delivery_id: str | None = None,
    session_generation: int | None = None,
    expires_at: float | None = None,
    validator: Callable[[], bool] | None = None,
) -> None:
    """Like `_delayed_enter`, but recovers a submit that was swallowed.

    Sends the submitting CR after ``delay_ms`` (same as `_delayed_enter`), then
    ``_SUBMIT_VERIFY_GRACE_MS`` later checks `is_at_ready_prompt()`. If the pane
    is STILL at its ready prompt the submit did not land (a real submit would
    have flipped it to busy), so it recovers — bounded by ``max_resends``.

    Two failure modes are distinguished when ``payload`` is supplied (#79):
      • Enter swallowed mid-paste-render — the input box still holds the pasted
        content (``shows_pending_input`` True). Re-send the CR only. (#22)
      • Paste swallowed — the input box is empty (``shows_pending_input`` False),
        so a CR resend has nothing to submit and the pane sits idle forever
        ("pane stays empty" — the #26 symptom). Re-paste the payload, then submit
        after the render settles.
    When ``payload`` is None the old behaviour (CR resend only) is preserved.

    A not-ready verdict is cross-checked against the input box too (#99):
    a busy marker unrelated to this submit (e.g. codex booting its own MCP
    servers) can read as not-ready while the paste is still visibly stuck in
    the composer. When that happens the CR is resent instead of the verify
    chain concluding "submitted" and stopping for good.

    Before EVERY verify decision, a live Qt main-thread heartbeat probe is
    checked (#133): a fan-out of concurrent pane spawns can backlog the event
    loop for ~1s at a time, and every verify() timer queued during that
    backlog then fires in a burst the instant it clears — collapsing the
    intended real-time render-settle grace into milliseconds and making a
    paste that was simply still painting under CPU contention look swallowed.
    If the probe reports the heartbeat went stale recently, the decision is
    DEFERRED (one more grace wait, budget unconsumed) instead of concluded —
    bounded so a stuck probe can't wedge the chain forever.

    ``on_resend`` / ``on_repaste`` (optional) are invoked with the
    remaining-attempt count each time the respective recovery fires, so the
    caller can log/observe it. ``on_settled`` (optional) fires exactly once,
    with no args, when the chain stops trying — submitted, gave up, or the
    pane was torn down — so a caller can serialise further writes to the same
    session until this one is no longer in flight (#133).

    ``validator`` (#258): forwarded to every `_safe_session_write` call this
    function makes — the CR resend below AND the payload repaste — same as
    the caller's OWN first write of this delivery. Before this fix only that
    first write carried a validator; the QTimer chain here (queued when the
    first write's own verify grace period began) kept resending/repasting
    with no way to notice the delivery was cancelled out from under it in
    the meantime (`takkub send`/`takkub task cancel`, #255) — a cancel that
    landed after the chain was already queued still pasted the stale
    payload straight into the composer. `_WriterThread.run` (pty_session.py)
    checks
    `message.validator()` immediately before every native write regardless
    of which call site attached it, so passing the SAME validator through
    here closes that gap without the writer needing to know anything about
    delivery IDs itself.
    """

    def _settled() -> None:
        if on_settled is not None:
            on_settled()

    # Output-timestamp baseline captured the instant we begin verifying (the
    # caller has just written the paste). Output that arrives AFTER this proves
    # claude received the paste, which the repaste branch uses to avoid the
    # duplicate-paste false-positive. A list cell so the repaste branch can
    # re-baseline against its own write without `nonlocal`. Best-effort: a fake
    # session without the accessor falls back to 0.0 (repaste path unchanged).
    paste_baseline = [
        session.last_output_monotonic() if hasattr(session, "last_output_monotonic") else 0.0
    ]

    def _send_then_verify(remaining: int, busy_remaining: int) -> None:
        if pane.session is not session:
            _settled()
            return
        _safe_session_write(
            pane.session,
            b"\r",
            priority=WritePriority.CONTROL,
            kind="control",
            delivery_id=delivery_id,
            session_generation=session_generation,
            expires_at=expires_at,
            validator=validator,
        )

        def _verify(
            render_waits: int = _RENDER_WAIT_MAX, stall_grace: int = _STALL_DEFER_MAX
        ) -> None:
            if pane.session is not session:
                _settled()
                return
            # #133: a decision made the instant a Qt backlog clears is exactly
            # what corrupted the composer — defer instead of trusting it. Costs
            # nothing when nothing was swallowed; bounded so a permanently-stale
            # probe can't wedge the chain. Does not touch render_waits/remaining
            # so it never eats into either resend budget.
            if stall_grace > 0 and _orch_attr("_main_thread_heartbeat_age", lambda: 0.0)() > (
                _STALL_DEFER_AGE_S
            ):
                QTimer.singleShot(
                    _SUBMIT_VERIFY_GRACE_MS,
                    lambda: _verify(render_waits, stall_grace - 1),
                )
                return
            # Submit landed → pane is busy → is_at_ready_prompt() is False → stop.
            # But "not ready" is ambiguous: it's ALSO what a busy marker
            # UNRELATED to this submit looks like — e.g. codex auto-booting its
            # configured MCP servers shows the "esc to interrupt" hard blocker
            # on screen while the composer still holds the unsubmitted paste
            # underneath it (#99, observed via direct transcript capture: pane
            # sits on "Booting MCP server: ... esc to interrupt" with the paste
            # placeholder still visible in the input row). Trusting the ready
            # verdict alone there means the false-stop below fires and the
            # submit chain gives up for good — no more resends ever fire.
            # Cross-check the input box itself before trusting it: if the
            # pasted content is still visibly sitting there, the CR hasn't
            # landed yet regardless of why the pane reads not-ready, so keep
            # retrying on the SEPARATE, generous busy budget (busy_remaining) —
            # a slow MCP boot is not a swallow, and sharing the tiny swallow
            # budget stranded later panes' tasks under concurrent multi-spawn
            # (3+ codex panes booting at once outlasted the 3-resend budget).
            if not session.is_at_ready_prompt():
                if (
                    payload is not None
                    and busy_remaining > 0
                    and session.shows_pending_input(content_fragment)
                ):
                    _log_verify_decision(
                        "resend_busy_pending",
                        session=session,
                        payload=payload,
                        is_ready=False,
                        shows_pending=True,
                    )
                    if on_resend is not None:
                        on_resend(busy_remaining)
                    _send_then_verify(remaining, busy_remaining - 1)
                    return
                _settled()
                return
            # Ready-prompt reached. The swallow/repaste recovery below is the
            # aggressive path (risks duplicate pastes), so it stays on the small
            # bounded swallow budget — exhaust it and stop.
            if remaining <= 0:
                _settled()
                return
            # Still ready → submit didn't land. If we have the payload and the
            # input box is empty, the PASTE may have been swallowed (#26) — but
            # "ready prompt + empty box" is ALSO exactly what a paste that was
            # SUBMITTED successfully looks like once claude returns to idle, and
            # what a landed paste whose `[Pasted text]` placeholder scrolled out
            # of the scanned footer rows looks like. Repasting in those cases is
            # the false-positive that stacked duplicate tasks in the input box —
            # near-universal under concurrent multi-project load (the visible
            # "เบิ้ลตามจำนวนโปรเจค"). Decide with a structural signal instead of
            # the ambiguous box state.
            if payload is not None and not session.shows_pending_input(content_fragment):
                # Did claude produce ANY output since we pasted? A paste that
                # landed renders a placeholder / streams a reply (timestamp
                # advances past the baseline); a swallowed paste leaves the pane
                # completely silent (timestamp unchanged). Output-since-paste ⇒
                # the bytes were received, so an empty box now means it was
                # SUBMITTED, not lost. Recover with a bare CR only (harmless
                # no-op if already submitted; submits a swallowed-CR #22 paste) —
                # never a second [Pasted text]. A fake session without the
                # accessor degrades to the prior render-guard behaviour.
                produced_output = (
                    session.last_output_monotonic() > paste_baseline[0]
                    if hasattr(session, "last_output_monotonic")
                    else False
                )
                if produced_output:
                    _log_verify_decision(
                        "resend_ready_output_produced",
                        session=session,
                        payload=payload,
                        is_ready=True,
                        shows_pending=False,
                        produced_output=True,
                    )
                    if on_resend is not None:
                        on_resend(remaining)
                    _send_then_verify(remaining - 1, busy_remaining)
                    return
                # No output yet since the paste. Could be a genuine swallow, or a
                # placeholder that hasn't started painting under load — wait out a
                # bounded grace for output to appear before concluding the bytes
                # were dropped (repasting into a slow-rendering box is what
                # stacked 4× [Pasted text]).
                if render_waits > 0 and session.seconds_since_output() < _RENDER_ACTIVE_S:
                    QTimer.singleShot(
                        _SUBMIT_VERIFY_GRACE_MS,
                        lambda: _verify(render_waits - 1, stall_grace),
                    )
                    return
                # Silent through the grace window → genuine swallow (#26):
                # re-paste, then submit once it renders. Re-baseline so the next
                # verify measures output produced by THIS repaste.
                _log_verify_decision(
                    "repaste",
                    session=session,
                    payload=payload,
                    is_ready=True,
                    shows_pending=False,
                    produced_output=False,
                    render_waits=render_waits,
                )
                if on_repaste is not None:
                    on_repaste(remaining)
                _safe_session_write(
                    session,
                    payload,
                    priority=WritePriority.TASK,
                    kind="task",
                    delivery_id=delivery_id,
                    session_generation=session_generation,
                    expires_at=expires_at,
                    validator=validator,
                )
                paste_baseline[0] = session.last_output_monotonic()
                QTimer.singleShot(
                    _enter_delay_ms(payload),
                    lambda: _send_then_verify(remaining - 1, busy_remaining),
                )
                return
            # Content present, CR swallowed mid-render (#22) — resend the CR.
            _log_verify_decision(
                "resend_content_present",
                session=session,
                payload=payload,
                is_ready=True,
                shows_pending=True,
            )
            if on_resend is not None:
                on_resend(remaining)
            _send_then_verify(remaining - 1, busy_remaining)

        QTimer.singleShot(_SUBMIT_VERIFY_GRACE_MS, _verify)

    QTimer.singleShot(delay_ms, lambda: _send_then_verify(max_resends, busy_max_resends))


# ── Mixin ─────────────────────────────────────────────────────────────────────


class LeadInboxMixin:
    """Lead-inbox queue and delivery methods for Orchestrator.

    All methods resolve through the combined MRO; state dicts
    (_lead_notify_queue, _lead_digest_queue, _digest_timer, _pending_lead_cc,
    _pending_done_notices, _lead_notify_pumping, _lead_notify_retry,
    _lead_draft_state) are
    initialised in Orchestrator.__init__ — never here — so ownership stays
    centralised.

    Dependencies on the host class (Orchestrator):
      _project_panes(project)          — spawn_engine cluster
      _resolve_project(project)        — static helper
      leadInjected.emit(body)          — Qt signal on Orchestrator
      _shard_groups: dict              — state, kept in Orchestrator.__init__
      _pane_state:   dict[str, ...]    — state, kept in Orchestrator.__init__
      _inject_shard_fanout_handoff()   — pipeline_executor cluster
    """

    # ------------------------------------------------------------------
    # Pipeline bridge (thin wrapper so pipeline_executor can call self.)
    # ------------------------------------------------------------------

    def _inject_to_lead(
        self, project_ns: str, message: str, log_event: str = "lead_inject"
    ) -> None:
        """Write *message* to the Lead pane. If Lead is absent, queue it in
        _pending_done_notices so it is delivered when Lead next spawns."""
        self._notify_lead(project_ns, message)
        _log_event(log_event, project=project_ns)

    # ------------------------------------------------------------------
    # Lead draft-typing guard (#3, 2026-07-09 core-upgrade plan)
    #
    # `is_at_ready_prompt()` only tells us the pane is idle — it can't see a
    # user draft sitting unsubmitted in the input line. Without this guard, a
    # done-notice/CC paste lands on top of that draft and the delayed Enter
    # submits both together, silently dragging the user's half-typed text
    # along with it. `_track_lead_draft_input` is fed every byte the Lead
    # pane's terminal emits (wired from `Orchestrator._on_pane_input`);
    # `_lead_can_accept_injection` is the single gate `_pump_lead_notify` and
    # `_flush_pending_lead_cc` share.
    # ------------------------------------------------------------------

    def _track_lead_draft_input(self, project_ns: str, data: bytes) -> None:
        if not hasattr(self, "_lead_draft_state"):
            self._lead_draft_state = {}
        prev = self._lead_draft_state.get(project_ns) or LeadDraftState()
        self._lead_draft_state[project_ns] = advance_draft_state(prev, data, time.time())

    def _lead_can_accept_injection(self, project_ns: str) -> bool:
        """True when the Lead pane's input line reads empty — safe to paste
        an engine-originated message without dragging in an unsubmitted draft."""
        state = getattr(self, "_lead_draft_state", {}).get(project_ns)
        return draft_state_allows_injection(state)

    def _lead_draft_hold_expired(self, project_ns: str) -> bool:
        """True once a held draft has blocked injection long enough that the
        caller should give up waiting and spill instead (see
        lead_draft_state.DRAFT_HOLD_TIMEOUT_S)."""
        state = getattr(self, "_lead_draft_state", {}).get(project_ns)
        return draft_hold_expired(state, time.time())

    # ------------------------------------------------------------------
    # Delivery helpers
    # ------------------------------------------------------------------

    def inject_lead_prompt(self, prompt: str, project: str | None = None) -> bool:
        """Paste a prompt into a project's Lead pane and submit it.

        For status-bar buttons that hand a task to the Lead instead of running
        a native GUI flow (e.g. the ⬆ Claude CLI button asks the Lead to
        check/report the CLI version rather than popping its own dialog).
        Writes immediately if the Lead is alive; otherwise queues via
        `_pending_done_notices` so it lands on the next Lead spawn. Returns
        True when delivered live, False when queued (no live Lead).
        """
        project_ns = self._resolve_project(project)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if lead and lead.session and lead.session.is_alive:
            self._notify_lead(project_ns, prompt)
            _log_event("inject_lead_prompt", project=project_ns)
            return True
        self._pending_done_notices.setdefault(project_ns, []).append(
            {"role": "system", "note": "lead prompt", "body": prompt, "queued_ts": time.time()}
        )
        self._save_pending_done_notices(project_ns)
        _log_event("inject_lead_prompt_queued", project=project_ns)
        return False

    def _ready_wait_ms(self, role_name: str, project: str | None, max_wait_ms: int) -> int:
        """Effective ready-prompt wait window for a pane.

        agy (the gemini role's engine) cold-boots far slower than claude/codex
        — a 146 MB self-contained binary that also scans the workspace on launch
        routinely needs ~45-60s to render its first ready prompt, landing right
        at the default 45s edge and forcing a fragile blind paste (#26). Give
        gemini/agy panes a longer window so first-assign delivery is confirmed,
        not blind. codex gets the same extension: it only counts ready once the
        composer status bar ('fast off'/'fast on') renders, which lands after
        codex finishes cold-booting AND auto-booting its configured MCP servers
        (#99 — the startup banner alone is deliberately not a ready marker), so
        the default 45s can also force a blind paste there. Claude roles whose
        effective pane-tools policy loads MCP servers get the same allowance:
        MCP schema boot routinely keeps qa/critic/designer from reaching the
        ready prompt within 45s (#118). An explicit non-default ``max_wait_ms``
        from the caller always wins (e.g. the short-poll peer-send path).
        """
        if max_wait_ms != 45_000:
            return max_wait_ms
        effective_wait_ms = max_wait_ms
        try:
            from .provider_config import CLAUDE, effective_provider_for
            from .provider_spec import PROVIDER_REGISTRY

            # Registry-driven (#103): each spec owns its cold-boot allowance via
            # `ready_wait_ms`, so a newly registered provider (opencode/kimi/
            # cursor …) gets its own window instead of silently inheriting
            # claude's 45 s and forcing a blind first paste. Was a hardcoded
            # codex/gemini pair.
            provider = effective_provider_for(role_name, project=self._resolve_project(project))
            spec = PROVIDER_REGISTRY.get(provider)
            if spec is not None and spec.ready_wait_ms:
                effective_wait_ms = max(effective_wait_ms, int(spec.ready_wait_ms))

            if provider == CLAUDE:
                from .pane_tools_policy import effective_mcps
                from .shared_dev_tools import default_role_mcp_policy

                base_role = _split_shard(role_name)[0]
                default_mcps = default_role_mcp_policy().get(base_role)
                if effective_mcps(base_role, default_mcps):
                    effective_wait_ms = max(effective_wait_ms, _MCP_READY_WAIT_MS)
        except Exception:
            pass
        return effective_wait_ms

    def _send_when_ready(
        self,
        role_name: str,
        task: str,
        max_wait_ms: int = 45_000,
        project: str | None = None,
        allow_repaste: bool | None = None,
    ) -> None:
        """Poll until claude's main prompt is idle, then paste task + Enter.

        Replaces the old fixed 12s wait so we don't paste into the trust modal
        or while claude is still bootstrapping. Falls back to a hard timeout
        so a hung claude doesn't silently swallow the task.

        ``allow_repaste=False`` (#134) disables the swallow/repaste recovery
        in ``_delayed_enter_verified`` for this delivery — only a bare CR
        resend is ever attempted. For a short, single-line, non-bracketed-
        paste body the "ready + empty box" verify signal is genuinely
        ambiguous between "submitted already" and "still holds the unsent
        text" (no ``[Pasted text]`` placeholder or `is_at_ready_prompt()`
        busy transition to disambiguate on), so trusting it enough to write a
        SECOND copy risks concatenating two copies into the composer with no
        separator (observed: the trigger doubled and submitted as one
        garbled turn). Callers whose body is recoverable another way (e.g.
        spawn_engine's `_CURRENT_TASK_TRIGGER`, backed by the idle watchdog's
        `[auto-reminder]` nudge if genuinely never delivered) should pass
        this. Ordinary task delivery keeps the default — losing a real task
        spec has no such backstop, so repaste recovery must stay on.
        """
        if allow_repaste is None:
            # Real orchestrators disable automated task-body repaste.  Default
            # to the historical behaviour for narrow adapters/test doubles
            # that instantiate this mixin without running Orchestrator.__init__.
            allow_repaste = bool(self.__dict__.get("_allow_automated_repaste", True))
        max_wait_ms = self._ready_wait_ms(role_name, project, max_wait_ms)
        pane = self._project_panes(project).get(role_name)
        if pane is None:
            return
        # #235: two `assign`s for the same role can start two independent
        # _send_when_ready poll loops — nothing dedupes them *before*
        # _deliver() runs (DeliveryManager's single-flight gate only exists
        # once a delivery_id is created, inside _deliver()). Left unguarded,
        # an OLDER call's poll loop keeps running after a NEWER assign for
        # the same role has already superseded it, and can fire a stale
        # "task ยังไม่ถึงมือ" busy-wait/unconfirmed warning about the old
        # attempt at the exact moment `takkub status` correctly shows the
        # NEW task already delivered and running — two contradicting
        # sources of truth reaching Lead at once (proven from a live
        # saas_admin incident transcript). Each call claims the newest
        # "generation" for (project, role) up front; every poll abandons
        # quietly the moment a later call supersedes it — no warning, since
        # a newer delivery is now the source of truth and will report for
        # itself.
        project_ns = self._resolve_project(project)
        dispatch_key = (project_ns, role_name)
        dispatch_gens: dict[tuple[str, str], int] = self.__dict__.setdefault(
            "_send_when_ready_generation", {}
        )
        my_generation = dispatch_gens.get(dispatch_key, 0) + 1
        dispatch_gens[dispatch_key] = my_generation
        elapsed = [0]
        sent = [False]
        # Sticky: once we've ever seen this role parked in the spawn gate's
        # deferred set (spawn_engine._spawn_deferred), stop trusting
        # max_wait_ms for the "no session yet" branch — see _check below.
        gate_seen = [False]
        ready_streak = [0]
        # #130: logged once (not every 150ms poll) the first time the hard
        # timeout is deferred because the pane is busy, not stuck.
        busy_wait_logged = [False]
        # #186: logged once, the first poll that recognises the pane is
        # blocked on an interactive prompt (trust modal / shell y-n) rather
        # than genuinely busy or genuinely stalled.
        prompt_blocked_warned = [False]
        # #186: accumulates only while _deliver() is deferring a would-be
        # blind paste because the pane is still on a recognised prompt.
        prompt_defer_elapsed = [0]
        # #248/#247 round 2: logged/warned once, the first poll that
        # recognises the pane's provider CLI showing an auth-failure marker.
        auth_failure_warned = [False]
        # #254: accumulates only while the pane CONTINUOUSLY shows a
        # provider boot-phase marker (e.g. codex "Booting MCP server: …") —
        # reset to 0 the instant a poll doesn't see it, so an intermittent
        # boot flash never adds up across unrelated stalls. Warned once,
        # separately from busy_wait_logged above, the first time the streak
        # crosses BOOT_STALL_GRACE_SEC.
        boot_stall_elapsed = [0]
        boot_stall_warned = [False]
        # Consecutive-poll counter gating the warn above — see
        # _AUTH_FAILURE_CONFIRM_POLLS. Reset to 0 the moment either the
        # marker stops matching OR the pane reaches its own ready prompt.
        auth_failure_streak = [0]

        def _deliver(unconfirmed: bool = False, busy_ceiling: bool = False) -> None:
            if sent[0]:
                return
            if (
                unconfirmed
                and pane.session is not None
                and pane.session.is_alive
                and _prompt_block_reason(pane.session)
            ):
                # Never blind-paste into an active prompt — the bytes land as
                # keystrokes on the modal itself, not the composer, so the
                # task is lost outright rather than merely unconfirmed (#186).
                # Give the auto-trust responder (or a human) a further
                # bounded grace to clear it before falling through to the
                # ordinary best-effort blind paste below.
                prompt_defer_elapsed[0] += _READY_POLL_INTERVAL_MS
                if prompt_defer_elapsed[0] < _PROMPT_BLOCK_DEFER_CEILING_MS:
                    QTimer.singleShot(_READY_POLL_INTERVAL_MS, _check)
                    return
                _log_event(
                    "task_deliver_prompt_defer_ceiling",
                    project=self._resolve_project(project),
                    role=role_name,
                )
            sent[0] = True
            if pane.session is None or not pane.session.is_alive:
                return
            pane.set_state("working", note=task[:60])
            _task_sess = pane.session
            payload = _paste_payload(_sanitize_pane_text(task))
            generation = int(getattr(pane, "_session_generation", 0))
            if "_delivery_manager" not in self.__dict__:
                self._delivery_manager = DeliveryManager(
                    event_sink=lambda event, details: _log_event(event, **details)
                )
            manager: DeliveryManager = self._delivery_manager
            delivery = manager.create(
                task_id="",
                project_id=self._resolve_project(project),
                pane_id=role_name,
                session_generation=generation,
                payload=payload,
            )
            if not manager.begin_write(delivery.delivery_id, generation):
                _log_event(
                    "task_delivery_single_flight_block",
                    project=self._resolve_project(project),
                    role=role_name,
                    delivery_id=delivery.delivery_id,
                )
                return
            wrote = _safe_session_write(
                _task_sess,
                payload,
                priority=WritePriority.TASK,
                kind="task",
                expires_at=delivery.expires_at,
                session_generation=generation,
                delivery_id=delivery.delivery_id,
                validator=lambda d=delivery.delivery_id, g=generation: manager.validate_for_write(
                    d, g
                ),
            )
            if not wrote:
                manager.mark_failed(delivery.delivery_id)
                _log_event(
                    "writer_queue_full",
                    project=self._resolve_project(project),
                    role=role_name,
                    delivery_id=delivery.delivery_id,
                )
                return
            manager.mark_written(delivery.delivery_id)
            manager.begin_submit(delivery.delivery_id, generation)
            if "_last_delivery_ids" not in self.__dict__:
                self._last_delivery_ids = {}
            self._last_delivery_ids[(self._resolve_project(project), role_name)] = (
                delivery.delivery_id
            )

            def _on_resend(rem: int, r=role_name, p=project) -> None:
                manager.retry_enter(delivery.delivery_id, generation)
                _log_event(
                    "task_deliver_enter_resend",
                    project=self._resolve_project(p),
                    role=r,
                    remaining=rem,
                    delivery_id=delivery.delivery_id,
                )

            def _on_settled() -> None:
                if pane.session is not _task_sess:
                    manager.mark_failed(delivery.delivery_id)
                    return
                try:
                    accepted = not _task_sess.is_at_ready_prompt()
                except Exception:
                    accepted = False
                if accepted:
                    manager.mark_accepted(delivery.delivery_id)
                else:
                    manager.mark_uncertain(delivery.delivery_id)

            # Self-healing submit: the task pastes as a `[Pasted text]` placeholder
            # and an Enter landing mid-render is swallowed, leaving the teammate
            # sitting on the placeholder forever instead of running the spec — the
            # original #22 symptom. _deliver only runs once the pane is at its ready
            # prompt (or blind on timeout), so verifying the submit landed and
            # resending is safe (a busy/booting pane is not "ready" → no resend).
            _orch_attr("_delayed_enter_verified", _delayed_enter_verified)(
                pane,
                _task_sess,
                _enter_delay_ms(payload),
                payload=payload if allow_repaste else None,
                content_fragment=task,
                on_resend=_on_resend,
                on_repaste=lambda rem, r=role_name, p=project: _log_event(
                    "task_deliver_repaste",
                    project=self._resolve_project(p),
                    role=r,
                    remaining=rem,
                ),
                on_settled=_on_settled,
                delivery_id=delivery.delivery_id,
                session_generation=generation,
                expires_at=delivery.expires_at,
                validator=lambda d=delivery.delivery_id, g=generation: manager.validate_for_write(
                    d, g
                ),
            )
            if unconfirmed:
                # Delivered blind — the pane never signalled ready, so on a cold
                # re-spawn the paste may have been swallowed (issue #26). Surface
                # it to the Lead instead of letting delegation fail silently.
                self._warn_lead_delivery_unconfirmed(
                    role_name, project, max_wait_ms, busy_ceiling=busy_ceiling
                )

        def _check() -> None:
            if sent[0]:
                return
            if dispatch_gens.get(dispatch_key) != my_generation:
                # #235: a newer assign() for this same role started a fresh
                # _send_when_ready generation — abandon this stale poll loop
                # quietly (mark sent so no later branch tries to _deliver()
                # the superseded payload either).
                sent[0] = True
                _log_event(
                    "task_deliver_superseded",
                    project=project_ns,
                    role=role_name,
                )
                return
            if pane.session is None or not pane.session.is_alive:
                # Session absent or not yet alive — may be deferred by the
                # spawn gate (modal/popup blocking ConPTY construction, see
                # spawn_engine._retry_deferred_spawn). That retry loop has no
                # timeout of its own — it keeps re-checking every 50ms until
                # the gate clears, however long that takes. So while the role
                # is (or was) parked in the gate's deferred set, keep polling
                # past max_wait_ms too: giving up here on a timer that's
                # shorter than the gate's own retry window silently drops the
                # task the moment the pane finally spawns (no session left
                # polling to paste it, no warning to the Lead — see the
                # 2026-07-11 dogfooding bug where a ~70s gate block outlived
                # the 45s default and the task vanished into a blank pane).
                elapsed[0] += _READY_POLL_INTERVAL_MS
                _deferred = getattr(self, "_spawn_deferred", None)
                _dk = f"{self._resolve_project(project)}::{role_name}"
                if _deferred is not None and _dk in _deferred:
                    gate_seen[0] = True
                # Sticky, not a live re-check: _retry_deferred_spawn discards
                # the deferred marker BEFORE its follow-up spawn() call
                # actually attaches a session (a ~35ms quiet-window gap) — a
                # poll landing in that gap would read "not deferred" even
                # though the pane is about to come up. Once gate_seen has
                # ever flipped True we commit to waiting it out regardless of
                # elapsed, and only bail if the pane itself was torn down
                # (closed / replaced by a fresh spawn) rather than re-timing
                # out on that narrow race.
                if elapsed[0] < max_wait_ms or gate_seen[0]:
                    if not gate_seen[0] or self._project_panes(project).get(role_name) is pane:
                        QTimer.singleShot(_READY_POLL_INTERVAL_MS, _check)
                        return
                # Hard timeout and either never gate-deferred, or the pane
                # was torn down while we waited: nothing left to paste into
                # (no session exists, unlike the ready-prompt-timeout branch
                # below). Warn instead of the silent drop this used to be.
                sent[0] = True
                self._warn_lead_delivery_unconfirmed(role_name, project, max_wait_ms)
                _log_event(
                    "task_deliver_timeout_no_session",
                    project=self._resolve_project(project),
                    role=role_name,
                )
                return
            if not prompt_blocked_warned[0]:
                # #186: recognise "blocked on a prompt that needs a keypress"
                # as its own state, checked on every poll independent of
                # elapsed/max_wait_ms — NOT folded into the busy/stall split
                # below, which only runs once the hard timeout has already
                # fired and can't tell a periodically-redrawing modal apart
                # from genuine work output. Warn the Lead the first poll that
                # sees it, however early, instead of waiting out the full
                # busy-wait ceiling.
                _reason = _prompt_block_reason(pane.session)
                if _reason:
                    prompt_blocked_warned[0] = True
                    _log_event(
                        "task_deliver_blocked_on_prompt",
                        project=self._resolve_project(project),
                        role=role_name,
                        reason=_reason,
                    )
                    self._warn_lead_delivery_blocked_prompt(role_name, project, _reason)
            # #271: computed once per poll (not just while the #254 warning
            # below is still armed) so the elapsed>=max_wait_ms blind-paste
            # guard further down can reuse the SAME read instead of calling
            # shows_startup_marker() a second time — a live session would
            # answer identically either way, but a second call also desyncs
            # any test double driven by a finite side_effect sequence.
            try:
                _still_booting = pane.session.shows_startup_marker()
                if not isinstance(_still_booting, bool):
                    # Defensive: a real PtySession always returns bool, but a
                    # test double / unconfigured mock session returns a
                    # truthy Mock object by default — never treat that as a
                    # genuine boot-phase marker hit (same guard as the
                    # auth-failure check below).
                    _still_booting = False
            except Exception:
                _still_booting = False
            if not boot_stall_warned[0]:
                # #254: same "checked every poll, own escalation" shape as
                # the prompt-block check above, for the same reason —
                # a continuously-redrawing boot spinner (codex/agy loading a
                # configured MCP server) keeps seconds_since_output() low
                # forever, so the busy/stall split further below (which only
                # engages once the ordinary delivery timeout has already
                # fired) would silently extend #144's busy-wait for up to
                # BUSY_WAIT_CEILING_SEC with no further signal. Escalating
                # far earlier, on a short CONTINUOUS streak, gives Lead
                # something concrete to act on well before that.
                if _still_booting:
                    boot_stall_elapsed[0] += _READY_POLL_INTERVAL_MS
                    if boot_stall_elapsed[0] >= _orch_attr("BOOT_STALL_GRACE_SEC", 110) * 1000:
                        boot_stall_warned[0] = True
                        _log_event(
                            "task_deliver_boot_stall",
                            project=self._resolve_project(project),
                            role=role_name,
                            elapsed_sec=round(boot_stall_elapsed[0] / 1000, 1),
                        )
                        self._warn_lead_delivery_boot_stall(
                            role_name, project, boot_stall_elapsed[0] / 1000
                        )
                else:
                    boot_stall_elapsed[0] = 0
            try:
                _pane_ready_now = pane.session.is_at_ready_prompt()
            except Exception:
                _pane_ready_now = False
            if not auth_failure_warned[0]:
                if _pane_ready_now:
                    # A CLI genuinely stuck on auth can never reach its own
                    # ready prompt — seeing one is proof any auth-failure
                    # marker still visible in the footer region is stale
                    # (e.g. a just-finished test run that printed one of the
                    # generic phrases, now scrolled into the tail rows with
                    # the CLI back at idle). Ready wins: let the normal
                    # ready_streak/_deliver() path below handle this poll
                    # instead of blind-delivering, and reset the streak so a
                    # later genuine relapse still needs its own confirmation.
                    auth_failure_streak[0] = 0
                else:
                    # #248/#247 round 2: recognise a provider CLI's own
                    # auth-failure text as its own state, checked on every
                    # poll like the prompt-block check above — fails fast
                    # instead of riding out the ordinary busy/stall path,
                    # which could take up to BUSY_WAIT_CEILING_SEC (1800s
                    # default) to say anything concrete about a pane that
                    # was never going to recover on its own.
                    try:
                        _provider = getattr(pane.model, "provider_name", None) or "claude"
                        _auth_reason = pane.session.auth_failure_reason(_provider)
                        if not isinstance(_auth_reason, str):
                            # Defensive: a real PtySession always returns str |
                            # None, but a test double / unconfigured mock
                            # session returns a truthy Mock object by default —
                            # never treat that as a genuine auth failure.
                            _auth_reason = None
                    except Exception:
                        _auth_reason = None
                    if _auth_reason:
                        auth_failure_streak[0] += 1
                    else:
                        auth_failure_streak[0] = 0
                    if _auth_reason and auth_failure_streak[0] >= _AUTH_FAILURE_CONFIRM_POLLS:
                        auth_failure_warned[0] = True
                        sent[0] = True
                        _log_event(
                            "task_deliver_auth_failure",
                            project=self._resolve_project(project),
                            role=role_name,
                            provider=_provider,
                            reason=_auth_reason,
                        )
                        self._warn_lead_auth_failure(role_name, project, _provider, _auth_reason)
                        # #269: blind-pasting into a CLI that just told us it
                        # isn't signed in only loses the task — unlike the
                        # no-content watchdog's timeout (which can be a slow
                        # boot), an auth-failure marker is a definitive
                        # "this provider is unusable right now" signal, unaffected
                        # by retrying the same provider. Route to the SAME
                        # close+respawn+degrade-to-claude recovery the no-content
                        # watchdog uses below instead, so any provider's auth
                        # failure recovers the same way no-content already does
                        # (previously ONLY no-content ever reached
                        # `provider_override`, so a provider whose CLI prints a
                        # recognisable auth-failure marker — e.g. gemini's "not
                        # signed in" — never degraded at all, #269).
                        self._recover_auth_failed_pane(
                            role_name, project, pane, task, provider=_provider, reason=_auth_reason
                        )
                        return
            no_content_ceiling_ms = _orch_attr("NO_CONTENT_WATCHDOG_SEC", 75) * 1000
            try:
                _no_content = (
                    pane.session is not None
                    and pane.session.is_alive
                    and pane.session.first_content_ts() is None
                )
            except Exception:
                _no_content = False
            if _no_content and elapsed[0] >= no_content_ceiling_ms:
                _no_content_key = _exit_key(project_ns, role_name)
                _ps_watchdog = getattr(self, "_pane_state", {}).get(_no_content_key)
                _no_content_attempts = (
                    _ps_watchdog.no_content_recover_attempts if _ps_watchdog is not None else 0
                )
                # close()+spawn() destroys and recreates the AgentPane widget
                # (paneClosed -> main_window._remove_teammate_pane tears it
                # down; a respawn's _ensure_teammate_pane builds a brand new
                # one) — this `pane`/`_check` closure would be polling a dead
                # object afterward, so recovery hands off to a FRESH
                # `_send_when_ready` call (started once the delayed respawn
                # lands) instead of trying to keep this loop alive. Gated on
                # the PERSISTED PaneState counter, not a local one-shot flag,
                # because that fresh call has its own brand-new closure state
                # — only a value that survives the close()/respawn cycle can
                # cap this at exactly one retry + one degrade overall.
                if _no_content_attempts < 2:
                    sent[0] = True
                    self._recover_no_content_pane(
                        role_name, project, pane, task, degrade=_no_content_attempts >= 1
                    )
                    return
                # Already used both the retry and the degrade attempt, still
                # no content — fall through to the ordinary
                # elapsed[0] >= max_wait_ms path below as the final safety
                # net (blind-deliver, unconfirmed).
            if _pane_ready_now:
                ready_streak[0] += 1
                # Wait for 5.0 seconds (33 polls of 150ms) of consecutive ready state
                # to ensure the CLI has finished async background loading (e.g. account verification).
                # For tests with tiny max_wait_ms, cap the requirement so it can succeed.
                required_polls = min(33, max(1, max_wait_ms // 150 - 1))
                if ready_streak[0] >= required_polls:
                    _deliver()
                    return
            else:
                ready_streak[0] = 0
            elapsed[0] += _READY_POLL_INTERVAL_MS
            if elapsed[0] >= max_wait_ms:
                # Hard timeout: pane never reached the ready prompt. That is
                # ALSO exactly what a pane legitimately busy on a prior
                # long-running task looks like (e.g. running a 6-minute test
                # suite) — is_at_ready_prompt() can't distinguish "stuck
                # empty" from "working" (#130, proven 6/6 false positives:
                # `takkub status` showed state=working with progress that had
                # just moved). Reuse the same structural PTY-output signal the
                # self-heal above already depends on (provider-agnostic —
                # works for codex/agy/claude alike, no text-marker parsing) to
                # tell them apart: a busy pane keeps producing output, a stuck
                # one goes silent. Only warn once the pane has been
                # continuously silent for STALL_THRESHOLD_SEC — the same bar
                # `takkub status` already uses to call a pane "stalled" —
                # otherwise keep polling so the task still lands normally the
                # moment the pane returns to ready.
                if _still_booting:
                    # #271: never blind-paste while the provider's own
                    # boot-phase marker (codex "Booting MCP server: …", agy
                    # equivalent) is still on screen — the composer hasn't
                    # rendered yet, so the bytes land as raw keystrokes on
                    # the boot splash and the task is lost outright, not
                    # merely unconfirmed (same risk class as the trust-modal
                    # defer in _deliver() above). Keep waiting past
                    # max_wait_ms instead, capped at the same
                    # BUSY_WAIT_CEILING_SEC ceiling as the busy-pane branch
                    # below so a boot that genuinely never finishes still
                    # gets a last-resort blind paste rather than polling
                    # forever. The [delivery-boot-stall] notice (#254,
                    # boot_stall_warned above) already tells Lead this pane
                    # is stuck here — no separate warning needed.
                    busy_wait_ceiling_ms = _orch_attr("BUSY_WAIT_CEILING_SEC", 1800) * 1000
                    if elapsed[0] < busy_wait_ceiling_ms:
                        QTimer.singleShot(_READY_POLL_INTERVAL_MS, _check)
                        return
                    _log_event(
                        "task_deliver_boot_marker_ceiling_timeout",
                        project=self._resolve_project(project),
                        role=role_name,
                        elapsed_sec=round(elapsed[0] / 1000, 1),
                    )
                    _deliver(unconfirmed=True, busy_ceiling=True)
                    return
                seconds_since_output = pane.session.seconds_since_output()
                stall_threshold_sec = _orch_attr("STALL_THRESHOLD_SEC", 300)
                if seconds_since_output < stall_threshold_sec:
                    # Follow-up to #130: the busy grace period above has no
                    # ceiling of its own, so a pane that produces output
                    # forever without ever returning to ready (e.g. a TUI
                    # stuck redrawing a spinner/progress animation on a loop,
                    # never actually accepting input) would poll past this
                    # branch indefinitely with no warning ever reaching the
                    # Lead — the exact silent-drop risk #26 was fixed to
                    # prevent, just reintroduced for the "busy" case instead
                    # of the "stuck empty" one. `elapsed[0]` has been
                    # accumulating since this poll loop started (it is never
                    # reset), so it doubles as the total-wait clock the
                    # ceiling needs.
                    busy_wait_ceiling_ms = _orch_attr("BUSY_WAIT_CEILING_SEC", 1800) * 1000
                    if elapsed[0] < busy_wait_ceiling_ms:
                        if not busy_wait_logged[0]:
                            busy_wait_logged[0] = True
                            _log_event(
                                "task_deliver_wait_extended",
                                project=self._resolve_project(project),
                                role=role_name,
                                seconds_since_output=round(seconds_since_output, 1),
                            )
                            # #144: until now the Lead heard nothing about a
                            # busy-wait extension until either the task
                            # finally landed or the ceiling above fired — up
                            # to BUSY_WAIT_CEILING_SEC (30 min default) later.
                            # Warn once, right as the extension begins, so the
                            # Lead knows delivery is still in flight instead
                            # of silently assuming it landed.
                            self._warn_lead_delivery_busy_wait(
                                role_name, project, seconds_since_output
                            )
                        QTimer.singleShot(_READY_POLL_INTERVAL_MS, _check)
                        return
                    # Ceiling reached while the pane was busy the whole time —
                    # a different symptom than the silent/empty case below, so
                    # it gets its own log event (distinct stats) and a warning
                    # that says so (a Lead reading "pane อาจค้าง empty" here
                    # would look for an empty pane and find one full of
                    # output, misdiagnosing it).
                    _log_event(
                        "task_deliver_busy_ceiling_timeout",
                        project=self._resolve_project(project),
                        role=role_name,
                        elapsed_sec=round(elapsed[0] / 1000, 1),
                    )
                    _deliver(unconfirmed=True, busy_ceiling=True)
                    return
                # Paste best-effort (markers may be a false negative) but flag
                # it as unconfirmed so the Lead verifies/re-assigns rather
                # than assuming the task landed (issue #26).
                _deliver(unconfirmed=True)
                return
            QTimer.singleShot(_READY_POLL_INTERVAL_MS, _check)

        # Initial wait before starting the poll loop. Allows the pane UI
        # to settle before we hammer it.
        # Start immediately if the pane is already gate-deferred, as we'll
        # just wait out the gate anyway.
        gate_deferred = getattr(pane, "deferred_spawn", False)
        QTimer.singleShot(0 if gate_deferred else _READY_POLL_FIRST_MS, _check)

    def _send_when_ready_no_repaste(
        self,
        role_name: str,
        task: str,
        *,
        project: str | None = None,
        max_wait_ms: int | None = None,
    ) -> None:
        """Dispatch with the v2 no-repaste flag and legacy override support."""
        target = self._send_when_ready
        try:
            parameters = tuple(inspect.signature(target).parameters.values())
            parameter_names = {parameter.name for parameter in parameters}
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
            )
        except (TypeError, ValueError):
            parameter_names = set()
            accepts_kwargs = True
        kwargs = {}
        if accepts_kwargs or "project" in parameter_names:
            kwargs["project"] = project
        if max_wait_ms is not None and (accepts_kwargs or "max_wait_ms" in parameter_names):
            kwargs["max_wait_ms"] = max_wait_ms
        if accepts_kwargs or "allow_repaste" in parameter_names:
            kwargs["allow_repaste"] = False
        target(role_name, task, **kwargs)

    def _reap_stale_deliveries(self) -> None:
        """Sweep `_delivery_manager` for deliveries that have been stuck in
        an in-flight state (never confirmed accepted) past their TTL and
        tell Lead about each one (issue #255 item 3).

        `DeliveryManager.expire_stale()` existed with zero callers before
        this — a delivery that never settles (never reaches ACCEPTED,
        UNCERTAIN, or FAILED) just sat in the registry forever with no
        signal to Lead beyond whatever the delivery poll loop itself
        already sent while it was still running. Wired into the same 5s
        tick as the other watchdog sub-checks in `_check_idle_teammates`
        (orchestrator.py) so this runs even for a delivery whose owning
        `_send_when_ready` poll loop already gave up and stopped ticking on
        its own (`sent[0] = True` paths) without ever reaching a terminal
        DeliveryManager state — the one gap none of the poll loop's own
        one-shot warnings above cover."""
        delivery_manager = getattr(self, "_delivery_manager", None)
        if delivery_manager is None:
            return
        for delivery in delivery_manager.expire_stale():
            self._notify_lead(
                delivery.project_id,
                f"⚠️ [delivery-stale-reap] task delivery ค้างอยู่สำหรับ {delivery.pane_id} "
                f"นานเกิน {delivery.expires_at - delivery.created_at:.0f}s โดยไม่ยืนยันว่า "
                f"ถึงมือ ({delivery.state.value}) — ยกเลิกอัตโนมัติแล้ว ถ้า {delivery.pane_id} "
                f"ยังไม่ได้รับ task ให้ assign ใหม่ (issue #255)",
                from_role="system",
                note="delivery_stale_reap",
            )
            _log_event(
                "delivery_stale_reaped",
                role=delivery.pane_id,
                project=delivery.project_id,
                delivery_id=delivery.delivery_id,
            )

    def _warn_lead_delivery_blocked_prompt(
        self, role_name: str, project: str | None, reason: str
    ) -> None:
        """Tell the Lead, once, the first delivery poll that finds the target
        pane sitting on an interactive prompt requiring a keypress (trust/
        onboarding modal, or a generic shell y/N) — fired immediately on
        detection, regardless of elapsed time, unlike the busy-wait (#144)
        and unconfirmed (#26) warnings below which only fire once the normal
        delivery timeout has already passed.

        Before this (#186) a modal that periodically redraws (a spinner,
        agy's boot-phase "verifying your account" text) advanced
        `seconds_since_output()` exactly like ordinary generation, so it was
        silently folded into the #144 busy-wait extension — the Lead heard
        nothing concrete until either it cleared on its own or the
        BUSY_WAIT_CEILING_SEC ceiling fired, up to 30 minutes later. Most
        providers auto-answer this within seconds (`_auto_trust`), so this
        warning is informational, not necessarily actionable — but a Lead
        who never sees it has no way to tell "quietly progressing" from
        "stuck needing a keypress" apart. Callers gate on their own one-shot
        flag (`prompt_blocked_warned` in _send_when_ready's `_check`
        closure) so this fires at most once per delivery. No-op when warning
        the Lead about itself."""
        if role_name == LEAD.name:
            return
        project_ns = self._resolve_project(project)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        kind = {
            "trust": "trust/onboarding modal",
            "permission": "tool-permission approval dialog",
        }.get(reason, "interactive shell prompt")
        msg = (
            f"⚠️ [delivery-blocked-prompt] {role_name} pane ติดอยู่ที่ {kind} "
            f"ที่ต้องกดตอบก่อนถึงจะรับ task ได้ — cockpit กำลัง auto-answer อยู่ "
            f"ถ้ายังไม่เคลียร์เองให้เข้าไปดู pane ตรงๆ (issue #186)"
        )
        self._notify_lead(project_ns, msg)
        _log_event(
            "delivery_blocked_prompt_warned",
            role=role_name,
            project=project_ns,
            reason=reason,
        )

    def _warn_lead_delivery_busy_wait(
        self, role_name: str, project: str | None, seconds_since_output: float
    ) -> None:
        """Tell the Lead, once, the moment an assign enters the #130
        busy-wait extension — the target pane never reached its ready prompt
        within the normal delivery timeout but is still producing output, so
        delivery keeps polling/resending rather than giving up. Before this
        (#144) the Lead heard nothing about that until either the task landed
        or the BUSY_WAIT_CEILING_SEC absolute ceiling fired, up to 30 minutes
        later by default — this fires right as the wait begins instead.
        Callers gate on their own one-shot flag (``busy_wait_logged`` in
        _send_when_ready's ``_check`` closure) so this is called at most once
        per delivery, keeping it from flooding the Lead on every 150 ms poll.
        No-op when warning the Lead about itself."""
        if role_name == LEAD.name:
            return
        project_ns = self._resolve_project(project)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        ceiling_sec = _orch_attr("BUSY_WAIT_CEILING_SEC", 1800)
        msg = (
            f"⏳ [delivery-busy-wait] {role_name} pane ยังไม่ถึง ready prompt แต่มี output "
            f"ล่าสุดเมื่อ {seconds_since_output:.0f}s ที่แล้ว (ยังไม่นิ่ง) — "
            f"task ยังไม่ถึงมือ กำลังรอ/จะ resend ต่อจนกว่าจะ ready หรือครบ {ceiling_sec}s "
            f"(issue #144)"
        )
        self._notify_lead(project_ns, msg)
        _log_event(
            "delivery_busy_wait_warned",
            role=role_name,
            project=project_ns,
            seconds_since_output=round(seconds_since_output, 1),
        )

    def _warn_lead_delivery_boot_stall(
        self, role_name: str, project: str | None, elapsed_sec: float
    ) -> None:
        """Tell the Lead, once, when a delivery poll sees the target pane's
        provider CLI CONTINUOUSLY showing its own boot-phase marker (e.g.
        codex/agy "Booting MCP server: …") for BOOT_STALL_GRACE_SEC straight
        — issue #254.

        Deliberately a SEPARATE notice from the generic busy-wait one
        (#144) above, fired much earlier (~BOOT_STALL_GRACE_SEC, ~2 min
        default) and worded as an actionable escalation rather than a
        progress update: a real incident sat in this exact state with the
        task NEVER pasted at all (composer still on its startup
        placeholder) all the way to the 30-minute BUSY_WAIT_CEILING_SEC —
        #144's single "still waiting" notice at the top of that window gave
        Lead nothing concrete to act on in the meantime. Names the specific
        symptom (boot, not generic busy) and the manual recovery — the
        escape hatch this issue asked for, short of a one-command restart
        primitive: close the wedged pane and reassign, since the task text
        itself survives via the ledger (`takkub task show`).

        Callers gate on their own one-shot flag (``boot_stall_warned`` in
        _send_when_ready's ``_check`` closure) so this fires at most once
        per delivery. No-op when warning the Lead about itself.

        #270: deliberately NOT auto-degraded like the no-content/auth-failure
        watchdogs (see ``_recover_broken_pane``) — a continuous boot marker
        means the CLI IS rendering and could still clear on its own (a slow
        first-cold-start MCP handshake, for example), unlike those two which
        are definitive dead ends. Auto-killing a pane that was about to
        succeed would waste the boot time already sunk and could paper over
        a real config bug (e.g. a broken MCP server entry) Lead should
        actually see and fix rather than have silently substituted away.
        So this stays Lead's call — but the notice now names the concrete
        `--provider` escape hatch (issue #270) instead of only "close and
        reassign", since a plain reassign to the SAME stuck provider just
        stalls again."""
        if role_name == LEAD.name:
            return
        project_ns = self._resolve_project(project)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        msg = (
            f"⛔ [delivery-boot-stall] {role_name} pane ค้างอยู่ที่ boot phase "
            f"(กำลังโหลด MCP server) มาแล้ว {elapsed_sec:.0f}s ติดต่อกันโดยไม่ถึง ready prompt "
            f"— task ยังไม่ถูกส่งเข้าไปเลย นี่ต่างจาก [delivery-busy-wait] ทั่วไป (#144) เพราะ pane "
            f"นี้ดูเหมือนติด boot ไม่ผ่าน ไม่ใช่แค่กำลังทำงานอยู่ ถ้ายังไม่คลี่คลายเอง: "
            f"`takkub close --role {role_name}` แล้ว `takkub assign --role {role_name} "
            f"--provider claude ...` เพื่อบังคับ spawn ใหม่ด้วย claude แทน provider เดิมที่ค้าง "
            f"(#270 — assign ใหม่แบบไม่ระบุ --provider จะไปติด provider เดิมซ้ำ) task เดิมดูได้ผ่าน "
            f"`takkub task show --role {role_name}` (issue #254)"
        )
        self._notify_lead(project_ns, msg)
        _log_event(
            "delivery_boot_stall_warned",
            role=role_name,
            project=project_ns,
            elapsed_sec=round(elapsed_sec, 1),
        )
        # #273: best-effort follow-up — ask the stuck provider's own CLI
        # what's actually wrong (e.g. `codex mcp list`) instead of leaving
        # Lead with only the generic "boot phase" wording above. Async and
        # fully optional: this notice has ALREADY been sent unchanged, so a
        # missing/slow/failed diagnostic changes nothing about it.
        self._run_boot_diagnostic_async(role_name, project, elapsed_sec)

    @staticmethod
    def _boot_diagnostic_notice_text(
        role_name: str, provider: str, argv: tuple[str, ...], exit_code: int, output: str
    ) -> str | None:
        """Pure: build the Lead follow-up notice from a finished diagnostic
        process's result, or None when nothing should be reported.

        A clean exit (0) means the provider's own health-check found
        nothing wrong — not itself news for a STUCK-boot diagnostic, so it
        stays silent rather than adding noise. Split out from
        `_run_boot_diagnostic_async`'s QProcess glue so this decision is
        unit-testable without spawning a real process (mirrors
        `Orchestrator._uncommitted_warning` next to
        `_check_uncommitted_async`)."""
        combined = (output or "").strip()
        if exit_code == 0 or not combined:
            return None
        return (
            f"🔎 [boot-diagnostic] {role_name} · `{provider} {' '.join(argv)}` "
            f"ตอบ (#273):\n{combined[:500]}"
        )

    def _run_boot_diagnostic_async(
        self, role_name: str, project: str | None, elapsed_sec: float
    ) -> None:
        """#273: probe the boot-stalled role's EFFECTIVE provider with its
        own ``ProviderSpec.boot_diagnostic_argv`` health-check command (e.g.
        ``codex mcp list``) and, only if it reports a real error, send Lead a
        follow-up notice with that error verbatim instead of leaving the
        generic `_warn_lead_delivery_boot_stall` wording as the only signal.

        Real incident (#273): the actual codex failure —
        ``Error: failed to load bootstrap configuration / Caused by: invalid
        transport in ...`` — was fully diagnosable via `codex mcp list` in
        under a second, but cockpit had no way to surface it, costing ~40
        minutes of manual config-file guessing.

        Runs via QProcess (mirrors `Orchestrator._check_uncommitted_async`)
        — NEVER blocks the Qt main thread. No-op, silently, whenever:
        the effective provider has no confirmed diagnostic command
        (``boot_diagnostic_argv`` is None — most providers today), its
        binary can't be found, the process errors/times out, OR it exits
        clean (a clean run means the provider's config is fine — this is a
        stuck-boot diagnostic, not a routine health poll, so a clean answer
        is not itself news). Every one of those is a no-op by design: this
        method may add a follow-up notice, never replace or delay the one
        `_warn_lead_delivery_boot_stall` already sent (#254 must not
        regress on providers without a confirmed diagnostic — the vast
        majority)."""
        from .provider_config import effective_provider_for
        from .provider_spec import PROVIDER_REGISTRY

        project_ns = self._resolve_project(project)
        provider = effective_provider_for(role_name, project=project_ns)
        spec = PROVIDER_REGISTRY.get(provider)
        argv = spec.boot_diagnostic_argv if spec is not None else None
        if not argv or spec.custom_discovery_fn is None:
            return
        try:
            binary = spec.custom_discovery_fn()
        except Exception:
            binary = None
        if not binary:
            return

        proc = QProcess(self)
        timeout = QTimer(self)
        timeout.setSingleShot(True)
        timeout.setInterval(8_000)
        state = {"done": False}

        def _settle(notice_text: str | None) -> None:
            if state["done"]:
                return
            state["done"] = True
            timeout.stop()
            timeout.deleteLater()
            try:
                proc.kill()
            except Exception:
                pass
            proc.deleteLater()
            if not notice_text:
                return
            self._notify_lead(project_ns, notice_text, from_role=role_name)
            _log_event(
                "boot_diagnostic_reported",
                role=role_name,
                project=project_ns,
                provider=provider,
                elapsed_sec=round(elapsed_sec, 1),
            )

        def _on_finished(_code: int, _status: object) -> None:
            try:
                out = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
                err = bytes(proc.readAllStandardError()).decode("utf-8", "replace")
            except Exception:
                out, err = "", ""
            notice_text = self._boot_diagnostic_notice_text(
                role_name, provider, argv, proc.exitCode(), err or out
            )
            _settle(notice_text)

        proc.finished.connect(_on_finished)
        proc.errorOccurred.connect(lambda _e: _settle(None))
        timeout.timeout.connect(lambda: _settle(None))
        timeout.start()
        proc.start(binary, list(argv))

    def _warn_lead_auth_failure(
        self, role_name: str, project: str | None, provider: str, reason: str
    ) -> None:
        """Tell the Lead, once, the first delivery poll that finds the
        pane's provider CLI showing an auth-failure marker (#248/#247 round
        2) — fired immediately on detection, like
        `_warn_lead_delivery_blocked_prompt` above, instead of riding out
        the ordinary busy/stall path which could otherwise take up to
        BUSY_WAIT_CEILING_SEC (1800s default) to say anything concrete.
        Names the provider and how to log back in
        (`ProviderSpec.post_install_note`, the same login hint doctor/
        install flows already surface) so the Lead doesn't have to go
        spelunking in the pane to figure out which CLI is broken. Callers
        gate on their own one-shot flag (`auth_failure_warned` in
        `_send_when_ready`'s `_check` closure). No-op when warning the Lead
        about itself."""
        if role_name == LEAD.name:
            return
        project_ns = self._resolve_project(project)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        from .provider_spec import PROVIDER_REGISTRY

        spec = PROVIDER_REGISTRY.get(provider)
        login_hint = (spec.post_install_note if spec is not None else "") or (
            f"log back into the {provider} CLI"
        )
        display = (spec.display_name if spec is not None else "") or provider.capitalize()
        msg = (
            f'🔒 [auth-failure] {role_name} pane ({display}) ติด auth error: "{reason}" — '
            f"{login_hint} แล้วสั่งงานใหม่อีกครั้ง (#248/#247)"
        )
        self._notify_lead(project_ns, msg)
        _log_event(
            "auth_failure_warned",
            role=role_name,
            project=project_ns,
            provider=provider,
            reason=reason,
        )

    def _warn_lead_auth_failure_degrade(
        self, role_name: str, project: str | None, provider: str, reason: str
    ) -> None:
        """Second Lead notice for an auth-failure recovery (#269), fired
        right after `_warn_lead_auth_failure` above (which names the
        provider/reason/login-hint) — tells Lead the pane is ALSO being
        degraded to claude and respawned, mirroring `_warn_lead_no_content`'s
        degrade message below for the no-content path. Split out the same
        way that one is, so the message can be asserted on independently of
        the close→respawn side effect."""
        if role_name == LEAD.name:
            return
        project_ns = self._resolve_project(project)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        from .provider_spec import PROVIDER_REGISTRY

        spec = PROVIDER_REGISTRY.get(provider)
        display = (spec.display_name if spec is not None else "") or provider.capitalize()
        msg = (
            f"⚠️ [auth-failure-degrade] {role_name} pane ({display}) ยัง auth ไม่ผ่าน "
            f'("{reason}") — degrade เป็น claude substitute แล้ว spawn ใหม่ (#269); '
            f"ถ้าต้องการใช้ {display} ต่อ ให้ login แล้วสั่งงานใหม่อีกครั้ง"
        )
        self._notify_lead(project_ns, msg)
        _log_event(
            "auth_failure_degrade_warned",
            role=role_name,
            project=project_ns,
            provider=provider,
            reason=reason,
        )

    def _warn_lead_no_content(self, role_name: str, project: str | None, *, degrade: bool) -> None:
        """One-line Lead notice for `_recover_no_content_pane` below — split
        out from the respawn logic so the message can be asserted on
        independently of the close→respawn side effect. No-op when warning
        the Lead about itself or when Lead itself is not reachable."""
        if role_name == LEAD.name:
            return
        project_ns = self._resolve_project(project)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        ceiling = _orch_attr("NO_CONTENT_WATCHDOG_SEC", 75)
        if degrade:
            msg = (
                f"⚠️ [no-content-degrade] {role_name} pane ไม่มี output เลยแม้ retry แล้ว 1 ครั้ง "
                f"(รอบละ {ceiling}s) — degrade เป็น claude substitute แล้ว spawn ใหม่ (#248/#247)"
            )
        else:
            msg = (
                f"🔁 [no-content-retry] {role_name} pane spawn แล้วไม่มี output เลยเกิน {ceiling}s "
                f"— กำลัง kill pane + retry spawn อีกครั้ง (#248/#247)"
            )
        self._notify_lead(project_ns, msg)
        _log_event(
            "no_content_pane_warned",
            role=role_name,
            project=project_ns,
            degrade=degrade,
        )

    def _recover_no_content_pane(
        self,
        role_name: str,
        project: str | None,
        pane: AgentPane,
        task: str,
        *,
        degrade: bool,
    ) -> None:
        """#248/#247 round 2: a pane whose provider CLI never rendered ANY
        content within NO_CONTENT_WATCHDOG_SEC of spawn — the exact "spawn
        succeeded, CLI never printed anything" symptom that made
        `orchestrator._pane_display_state` add the "spawning" label in
        round 1 (first_content_ts() staying None is otherwise
        indistinguishable from a slow-but-healthy cold boot).

        First occurrence (``degrade=False``): close + respawn once. No
        conversation has actually started (zero content ever rendered), so
        none of `_auto_recover_stuck`'s snapshot/--resume machinery is
        needed — a plain fresh spawn is equivalent and far simpler.

        Second occurrence (``degrade=True``): the retry ALSO produced
        nothing, so stop trying this provider for this pane — force the
        next spawn to claude via `PaneState.provider_override`. See
        `_recover_broken_pane` for the shared close+respawn+degrade
        mechanics (also used by `_recover_auth_failed_pane` below, #269)."""
        self._warn_lead_no_content(role_name, self._resolve_project(project), degrade=degrade)
        self._recover_broken_pane(
            role_name, project, pane, task, degrade=degrade, kind="no_content"
        )

    def _recover_auth_failed_pane(
        self,
        role_name: str,
        project: str | None,
        pane: AgentPane,
        task: str,
        *,
        provider: str,
        reason: str,
    ) -> None:
        """#269: a pane whose provider CLI is showing its own auth-failure
        marker (confirmed over `_AUTH_FAILURE_CONFIRM_POLLS` consecutive
        polls by `_check` above) — unlike the no-content watchdog's timeout
        (which can just mean a slow boot), an auth-failure marker is a
        definitive "this provider is unusable right now" signal. Retrying
        the same provider cannot help (a CLI stuck on login stays stuck
        until a human logs it in out-of-band — proven in #269 by Lead
        manually close+reassigning the same role 3 times with identical
        results), so this degrades straight to claude — no non-degraded
        retry step, unlike `_recover_no_content_pane`'s first occurrence.

        Before this, ONLY the no-content watchdog ever reached
        `PaneState.provider_override` — a provider whose CLI renders a
        recognisable auth-failure marker (e.g. gemini's "not signed in")
        never has `first_content_ts()` stay ``None`` (the marker text IS
        content), so the no-content branch's `_no_content` check never
        fires and this path never engaged, leaving that role permanently
        stuck reassigning into the same broken login every time (#269)."""
        self._warn_lead_auth_failure_degrade(
            role_name, self._resolve_project(project), provider, reason
        )
        self._recover_broken_pane(role_name, project, pane, task, degrade=True, kind="auth_failure")

    def _recover_broken_pane(
        self,
        role_name: str,
        project: str | None,
        pane: AgentPane,
        task: str,
        *,
        degrade: bool,
        kind: str,
    ) -> None:
        """Shared close+respawn(+degrade-to-claude) mechanics for
        `_recover_no_content_pane` and `_recover_auth_failed_pane` (#269) —
        see those two for why/when each calls in with ``degrade``.

        ``degrade=True`` forces the next spawn to claude via
        `PaneState.provider_override` (mirrors `model_override`'s existing
        per-pane precedent in spawn_engine.py; consulted ahead of
        `provider_config.effective_provider_for`'s normal config/
        availability resolution), the same "unavailable provider → claude
        substitute" idea the cockpit already applies globally, just
        triggered per-pane here instead of by the disabled-providers/
        binary-missing check that mechanism normally runs on.
        `provider_override` naturally clears the next time this role's
        PaneState is popped (a plain `close()`/`done()`, or the
        `self.close()` a few lines below), so a later fresh `assign()` for
        this role tries the real provider again rather than staying
        degraded forever.

        The caller (`_check`) does NOT keep polling the same closure
        afterward — `close()` tears the AgentPane widget down entirely
        (`paneClosed` → `main_window._remove_teammate_pane`) and a respawn
        builds a brand new one (`_ensure_teammate_pane`), so the `pane`
        object this call was given goes stale the moment `close()` returns.
        Instead this starts a FRESH `_send_when_ready` call once the
        delayed respawn lands (which re-fetches the pane from the registry
        at its own top), the same way `_auto_recover_stuck`'s
        `_do_respawn` re-sends a snapshot task after ITS close→respawn
        cycle. The 2s close-then-respawn pause also mirrors
        `_auto_recover_stuck` — it lets the PTY/WebEngine teardown finish
        before a new session binds to the same role slot."""
        if role_name == LEAD.name:
            return
        project_ns = self._resolve_project(project)
        key = _exit_key(project_ns, role_name)
        cwd = getattr(pane, "_session_cwd", None)
        # Snapshot before close() pops the whole PaneState — restored into
        # the fresh one _do_respawn creates so the attempt count survives
        # the pop (same snapshot/restore shape _auto_recover_stuck uses for
        # its own counters). Shared across both `kind`s: an auth-failure
        # recovery bumping this is harmless (it only ever makes a LATER
        # no-content check on this same pane degrade sooner, never later).
        _ps_snap = getattr(self, "_pane_state", {}).get(key)
        snap_attempts = _ps_snap.no_content_recover_attempts if _ps_snap is not None else 0
        _log_event(
            f"{kind}_pane_recover",
            role=role_name,
            project=project_ns,
            degrade=degrade,
            prior_attempts=snap_attempts,
        )
        self.close(role_name, project=project_ns, suppress_pipeline=True, suppress_auto_chain=True)

        def _do_respawn() -> None:
            ps = self._ps(key)
            ps.no_content_recover_attempts = snap_attempts + 1
            if degrade:
                ps.provider_override = "claude"
            ok, msg = self.spawn(role_name, cwd=cwd, project=project_ns, _from_auto_respawn=True)
            _log_event(
                f"{kind}_pane_respawned",
                role=role_name,
                project=project_ns,
                degrade=degrade,
                ok=ok,
                msg=msg[:160],
            )
            if ok and task:
                self._send_when_ready(role_name, task, project=project_ns)

        QTimer.singleShot(2_000, _do_respawn)

    def _warn_lead_delivery_unconfirmed(
        self,
        role_name: str,
        project: str | None,
        wait_ms: int = 45_000,
        *,
        busy_ceiling: bool = False,
    ) -> None:
        """Tell the Lead that an assign hit its hard timeout without the
        target pane ever signalling ready. The task was pasted blind and may
        not have landed (cold re-spawn render differs from boot), so the Lead
        should verify / re-assign instead of trusting the 'task queued' reply
        (issue #26). No-op when warning the Lead about itself.

        ``busy_ceiling=True`` means the pane produced output the whole time
        (never went silent) but still never returned to its ready prompt
        before the #130 busy-wait absolute ceiling — a different symptom from
        the plain empty/stuck case, so it gets distinct wording (a Lead told
        to look for "pane อาจค้าง empty" would misdiagnose a pane that is
        visibly full of output) and a separate log event."""
        if role_name == LEAD.name:
            return
        project_ns = self._resolve_project(project)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        wait_label = f"{wait_ms / 1000:g}s"
        if busy_ceiling:
            msg = (
                f"⚠️ [delivery-unconfirmed] {role_name} pane มี output ต่อเนื่องมาตลอดแต่ไม่กลับสู่ "
                f"ready prompt เกิน {wait_label} — "
                f"task ถูก paste แบบ blind อาจไม่ติด (pane ค้างแบบ 'มีเสียง' เช่น TUI/spinner วนซ้ำ "
                f"ไม่ใช่ pane ว่างเงียบ). เช็ค pane / re-assign ถ้ายังไม่ลง — "
                f"อย่าถือว่าส่งสำเร็จ (issue #131)"
            )
        else:
            msg = (
                f"⚠️ [delivery-unconfirmed] {role_name} pane ไม่ถึง ready prompt ใน "
                f"{wait_label} — "
                f"task ถูก paste แบบ blind อาจไม่ติด (pane อาจค้าง empty). "
                f"เช็ค pane / re-assign ถ้ายังว่าง — อย่าถือว่าส่งสำเร็จ (issue #26)"
            )
        self._notify_lead(project_ns, msg)
        _log_event(
            "delivery_unconfirmed_busy_ceiling" if busy_ceiling else "delivery_unconfirmed",
            role=role_name,
            project=project_ns,
            wait_ms=wait_ms,
        )

    def _warn_lead_spawn_failed(self, role_name: str, project: str | None, reason: str) -> None:
        """Tell the Lead that an assign's pane spawn failed. The CLI acks
        'task queued' to the Lead's shell before the async spawn runs, so a
        spawn failure is otherwise invisible and the delegation silently dies
        (#26). No-op when the failed role is the Lead itself."""
        if role_name == LEAD.name:
            return
        project_ns = self._resolve_project(project)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        msg = (
            f"⚠️ [spawn-failed] {role_name} pane สร้างไม่สำเร็จ — task ไม่ได้ส่ง ({reason}). "
            f"ลอง assign {role_name} ใหม่อีกครั้ง (ถ้ายิง parallel ลองยิงทีละตัว) — "
            f"อย่าถือว่า 'task queued' = สำเร็จ (issue #26)"
        )
        self._notify_lead(project_ns, msg)
        _log_event("spawn_failed_warned", role=role_name, project=project_ns)

    def _warn_lead_spawn_stuck(self, role_name: str, project: str | None, age_sec: float) -> None:
        """Tell the Lead that an assign has been sitting in the spawn FIFO
        queue for longer than spawn_engine.SPAWN_QUEUE_STUCK_SEC without ever
        reaching a pane (#140). Until this, a queued assign was silent —
        _warn_lead_spawn_failed only ever fired for an outright spawn
        *failure*, never for one stuck waiting behind another spawn that
        wouldn't finish. Called by spawn_engine._check_spawn_queue_stuck,
        which has already forced the arbiter open and requeued this item, so
        the wording reflects that a retry is already underway. No-op when the
        stuck role is the Lead itself."""
        if role_name == LEAD.name:
            return
        project_ns = self._resolve_project(project)
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        msg = (
            f"⚠️ [spawn-stuck] {role_name} assign ค้างใน spawn queue นานเกิน {age_sec:.0f}s "
            f"— arbiter ถูกปล่อยแล้วและกำลัง retry ให้อัตโนมัติ แต่ pane ยังไม่ขึ้น "
            f"ณ ตอนนี้. เช็คด้วย takkub list — ถ้ายังไม่ขึ้นให้ลอง assign {role_name} "
            f"ใหม่อีกครั้ง (issue #139/#140)"
        )
        self._notify_lead(project_ns, msg)
        _log_event(
            "spawn_stuck_warned",
            role=role_name,
            project=project_ns,
            age_sec=round(age_sec, 1),
        )

    def _warn_lead_respawn_capped(self, role_name: str, project: str) -> None:
        """Bug-3 fix: tell Lead that a teammate hit AUTO_RESPAWN_MAX and gave up.

        Without this notice the Lead never learns the pane is permanently down,
        auto-chain siblings wait forever for a done event that never comes, and
        the operator stares at a deadlocked workflow.  No-op when Lead is absent
        (queuing is not needed — if Lead comes back, respawn is already capped
        and the operator will see the dead pane slot directly).

        #4 fix: shard failed-bookkeeping runs BEFORE the Lead-alive early return
        so the group closes (and fires a handoff via queue) even when Lead is down.
        """
        project_ns = self._resolve_project(project)

        # #4: hoist shard bookkeeping before the Lead-alive gate so the group
        # closes and queues its handoff regardless of whether Lead is running.
        base_role_c, shard_idx_c = _split_shard(role_name)
        if shard_idx_c is not None:
            key_c = f"{project_ns}::{role_name}"
            # Access pane state without importing PaneState (avoid orchestrator cycle).
            # The dict value is a PaneState instance when present; None means not tracked.
            ps_c = getattr(self, "_pane_state", {}).get(key_c)
            if ps_c is not None and ps_c.shard_total > 0:
                group_key_c = f"{project_ns}::{base_role_c}"
                group_c = self._shard_groups.get(group_key_c)
                if group_c and not group_c.closed:
                    group_c.failed.add(role_name)
                    if len(group_c.done) + len(group_c.failed) >= group_c.total:
                        group_c.closed = True
                        self._inject_shard_fanout_handoff(project_ns, group_c)
                        self._shard_groups.pop(group_key_c, None)

        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        # Read AUTO_RESPAWN_MAX from orchestrator module via sys.modules to avoid a
        # top-level import cycle (lead_inbox is forbidden from importing orchestrator).
        _orch_m = _sys.modules.get("agent_takkub.orchestrator")
        auto_respawn_max = getattr(_orch_m, "AUTO_RESPAWN_MAX", 2) if _orch_m else 2
        msg = (
            f"⚠️ [respawn-capped] {role_name} ({project_ns}) หยุด auto-respawn แล้ว "
            f"(crash {auto_respawn_max} ครั้งติด) — pane ดับถาวรจนกว่า Lead จะ assign ใหม่ "
            f"ถ้า pane นี้อยู่ใน auto-chain verify hop อาจค้างได้ — ตรวจสอบ takkub list"
        )
        self._notify_lead(project_ns, msg)
        _log_event("respawn_capped_warned", role=role_name, project=project_ns)

    # ------------------------------------------------------------------
    # Peer CC durability helpers
    # ------------------------------------------------------------------

    def _pending_cc_path(self, project_ns: str) -> pathlib.Path:
        return (
            _orch_attr("RUNTIME_DIR", _RUNTIME_DIR_DEFAULT) / f"pending-lead-cc-{project_ns}.json"
        )

    def _save_pending_cc(self, project_ns: str) -> None:
        """Persist current queue for project_ns so it survives orchestrator restart."""
        try:
            _orch_attr("ensure_runtime", _ensure_runtime_default)()
            queue = self._pending_lead_cc.get(project_ns, [])
            path = self._pending_cc_path(project_ns)
            if not queue:
                path.unlink(missing_ok=True)
                return
            path.write_text(json.dumps(queue, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _load_pending_cc(self) -> None:
        """Restore queued CC messages from disk on startup."""
        try:
            _orch_attr("ensure_runtime", _ensure_runtime_default)()
            runtime_dir = _orch_attr("RUNTIME_DIR", _RUNTIME_DIR_DEFAULT)
            for p in runtime_dir.glob("pending-lead-cc-*.json"):
                proj = p.stem[len("pending-lead-cc-") :]
                try:
                    items = json.loads(p.read_text(encoding="utf-8"))
                    if items:
                        self._pending_lead_cc[proj] = items
                except Exception:
                    pass
        except Exception:
            pass

    def _pending_done_path(self, project_ns: str) -> pathlib.Path:
        return (
            _orch_attr("RUNTIME_DIR", _RUNTIME_DIR_DEFAULT)
            / f"pending-done-notices-{project_ns}.json"
        )

    def _save_pending_done_notices(self, project_ns: str) -> None:
        """Persist queued done notices so they survive an orchestrator restart
        while the Lead is down (issue #13). Mirrors _save_pending_cc."""
        try:
            _orch_attr("ensure_runtime", _ensure_runtime_default)()
            queue = self._pending_done_notices.get(project_ns, [])
            path = self._pending_done_path(project_ns)
            if not queue:
                path.unlink(missing_ok=True)
                return
            path.write_text(json.dumps(queue, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _load_pending_done_notices(self) -> None:
        """Restore queued done notices from disk on startup. Mirrors
        _load_pending_cc."""
        try:
            _orch_attr("ensure_runtime", _ensure_runtime_default)()
            runtime_dir = _orch_attr("RUNTIME_DIR", _RUNTIME_DIR_DEFAULT)
            for p in runtime_dir.glob("pending-done-notices-*.json"):
                proj = p.stem[len("pending-done-notices-") :]
                try:
                    items = json.loads(p.read_text(encoding="utf-8"))
                    valid = (
                        [
                            item
                            for item in items
                            if isinstance(item, dict) and isinstance(item.get("body"), str)
                        ]
                        if isinstance(items, list)
                        else []
                    )
                    if valid:
                        self._pending_done_notices[proj] = valid
                except Exception:
                    pass
        except Exception:
            pass

    def _flush_pending_lead_cc(self, project_ns: str) -> None:
        """Deliver queued CC messages to Lead if it is currently alive.

        Called after Lead spawns. If Lead is not ready yet, the queue is
        left intact so the next explicit flush attempt (or next send()) can
        try again. A separate QTimer in spawn() retries until Lead is alive.
        """
        pending = self._pending_lead_cc.get(project_ns)
        if not pending:
            return
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return  # Lead still not alive — keep queue, retry later
        if not self._lead_can_accept_injection(project_ns):
            # User has an unsubmitted draft — leave the queue intact for the
            # next retry (spawn's timer or the next live send()) rather than
            # paste the CC over it.
            return
        # Hand every CC to the same ready-prompt-aware serialised queue used by
        # done notices — a tight paste loop must not stack messages into a Lead
        # that went busy after the initial readiness check (finding lead_inbox:693).
        # Track delivered items so a _notify_lead that raises mid-flush preserves
        # ONLY the undelivered tail (durable for the next retry, M4#22) and never
        # re-enqueues an already-handed-off CC (the duplicate-delivery regression).
        items = list(pending)
        delivered = 0
        try:
            for item in items:
                body = item.get("body") if isinstance(item, dict) else None
                if isinstance(body, str):
                    self._notify_lead(
                        project_ns,
                        body,
                        from_role=item.get("from_role", "system"),
                        note="cc_flush",
                    )
                delivered += 1  # a malformed item is dropped, not retried forever
        finally:
            remaining = items[delivered:]
            if remaining:
                self._pending_lead_cc[project_ns] = remaining
            else:
                self._pending_lead_cc.pop(project_ns, None)
            self._save_pending_cc(project_ns)
        if delivered:
            _log_event("send_cc_flushed", project=project_ns, count=delivered)

    # ------------------------------------------------------------------
    # Lead-notify queue: ready-prompt-aware serialised delivery
    # ------------------------------------------------------------------

    def _enqueue_live_lead_notice(
        self,
        project_ns: str,
        body: str,
        *,
        front: bool = False,
        pane_token: str | None = None,
        queued_ts: float | None = None,
    ) -> None:
        """Append one ready-to-deliver body without starting the pump.

        *pane_token* (#228) records which pane instance's `done()` call — if
        any — produced *body*, so `_pump_lead_notify` can re-check at actual
        delivery time whether that role slot has since been respawned. None
        for system-authored bodies (combined digests, CC relays, engine
        notices) that carry no single-pane origin claim.

        *queued_ts* (#266): defaults to now — pass the original occurrence
        time through on a requeue so it survives round-trips through the
        durable store instead of resetting on every hop (see `_notify_lead`).
        Previously this queue never carried a timestamp at all (documented
        as "the live queue delivers promptly enough that staleness isn't a
        concern" — #264/#266 proved that wrong: busy-retry + draft-hold +
        durable spill can delay live-queue delivery by minutes).
        """
        if not hasattr(self, "_lead_notify_queue"):
            self._lead_notify_queue = {}
        if not hasattr(self, "_lead_notify_pumping"):
            self._lead_notify_pumping = set()
        queue = self._lead_notify_queue.setdefault(project_ns, collections.deque())
        item = (body, pane_token, queued_ts if queued_ts is not None else time.time())
        if front:
            # Keep blocking notices FIFO with each other while placing them
            # ahead of informational/digest bodies already waiting on a busy
            # Lead.
            priority_end = 0
            while priority_end < len(queue) and _is_blocking_lead_notice(queue[priority_end][0]):
                priority_end += 1
            queue.insert(priority_end, item)
        else:
            queue.append(item)

    def _arm_lead_digest(self, project_ns: str, window_ms: int) -> None:
        """Debounce the project's digest using a generation-checked timer.

        ``QTimer.singleShot`` timers cannot be cancelled.  A monotonically
        increasing token makes older callbacks harmless while still restarting
        the full window whenever another notice joins the burst.
        """
        if not hasattr(self, "_digest_timer"):
            self._digest_timer = {}
        generation = int(self._digest_timer.get(project_ns, 0)) + 1
        self._digest_timer[project_ns] = generation
        QTimer.singleShot(
            window_ms,
            lambda p=project_ns, g=generation: self._flush_lead_digest(p, generation=g),
        )

    def _flush_lead_digest(
        self,
        project_ns: str,
        *,
        generation: int | None = None,
        arm_pump: bool = True,
        trailing_body: str | None = None,
    ) -> bool:
        """Move a pending burst into the live queue as one Lead turn.

        ``generation`` is supplied by timer callbacks.  Early flushes (notably
        before an auto-chain handoff) invalidate the outstanding callback and
        preserve the chronological order: digest first, actionable handoff
        second.
        """
        timers = getattr(self, "_digest_timer", {})
        if generation is not None and timers.get(project_ns) != generation:
            return False
        # Keep the last generation instead of deleting it. An early flush can
        # leave its uncancellable singleShot callback outstanding; a later
        # burst must receive a strictly newer token so that old callback cannot
        # accidentally flush the new mail.

        pending = getattr(self, "_lead_digest_queue", {}).pop(project_ns, None)
        if not pending:
            return False
        items = list(pending)
        now_ts = time.time()
        already_read = getattr(self, "_inbox_seen", {}).get(project_ns, ())
        lines = []
        for entry in items:
            item_body, item_pane_token, item_ts = _unwrap_notice_item(entry)
            item_facts = _unwrap_notice_facts(entry)
            role = _notice_role_tag(item_body)
            if _notice_fingerprint(item_body) in already_read:
                # #241 option A: Lead already pulled this exact report via
                # `takkub inbox`/`takkub wait` before the debounce window
                # closed — collapse instead of re-pasting content Lead has
                # already read.
                age = _format_notice_age(now_ts, item_ts)
                lines.append(f"• [{role or 'system'}] (อ่านแล้วผ่าน takkub inbox{age})")
                continue
            line = _format_digest_item(item_body, item_ts, now_ts, facts=item_facts)
            if self._provenance_stale(project_ns, role, item_pane_token):
                line = f"{_STALE_ORIGIN_BANNER.format(role=role)}\n{line}"
            lines.append(line)
        digest = "\n".join(
            [
                f"📬 [Lead Inbox Digest — {len(items)} update{'s' if len(items) != 1 else ''}]",
                *lines,
            ]
        )
        if trailing_body:
            # Auto-chain uses this path: the actionable handoff follows the
            # digest in the same payload/turn, so Lead sees the prerequisite
            # done notes first without waiting through a separate digest turn.
            digest = f"{digest}\n\n{trailing_body}"
        self._enqueue_live_lead_notice(project_ns, digest)
        _log_event("lead_inbox_digest", project=project_ns, count=len(items))
        if arm_pump:
            self._arm_lead_notify_pump(project_ns)
        return True

    def _provenance_stale(self, project_ns: str, role: str | None, pane_token: str | None) -> bool:
        """True when a role-attributed notice's origin pane can no longer be
        confirmed live under that role slot (#228).

        *role* is None for bodies that carry no per-pane origin claim (system
        notices, CC relays, combined digests) — never stale, there is nothing
        to verify. *pane_token* is None when the caller didn't attach one
        (system-authored bodies, or legacy/test-constructed queue entries) —
        also treated as nothing-to-verify rather than flagged, so this only
        ever fires for a genuine `[role done]`/`[role FAILED]` body whose
        producing `done()` call recorded its own pane token.
        """
        if role is None or pane_token is None:
            return False
        current = self._current_pane_identity(project_ns, role)
        return current != pane_token

    def _other_roles_still_active(self, project_ns: str, exclude_role: str) -> bool:
        """(#264) True if some role OTHER than Lead and *exclude_role* (the
        one that just produced the notice currently being enqueued) is
        still in a non-terminal state — i.e. could plausibly still produce
        its own digestible notice soon enough to join the SAME burst.

        Read via `list_status` — the exact same role→state snapshot
        `takkub list` shows — rather than a bespoke check, so this can
        never disagree with what Lead sees when they look. `exclude_role`
        matters because `_notify_lead` runs from inside `done()` BEFORE
        that pane's own `pane.set_state("done", ...)` call lands (the
        notice must exist before the state transition that would otherwise
        make it "terminal"), so the reporting role's own pane still reads
        "working" at this exact moment — excluding it by name is the only
        way to avoid it always counting as its own reason to wait.
        """
        status = self.list_status(project=project_ns)
        for role, state in status.items():
            if role in (LEAD.name, exclude_role):
                continue
            if state not in _DIGEST_TERMINAL_PANE_STATES:
                return True
        return False

    def _revalidate_system_notice(self, project_ns: str, body: str) -> str:
        """(#266) A delivery-health system notice
        ([delivery-unconfirmed]/[spawn-stuck]/[delivery-boot-stall]/
        [spawn-failed]/[delivery-busy-wait]) is enqueued the moment the
        condition is DETECTED, but can then sit in the live/durable queue
        through the busy-retry cap (~30s), a durable spill, and the reaper's
        staleness window (up to another 60s) before it's actually pasted —
        #264's delivery chain. Nothing re-checked whether the condition was
        still true by the time it finally landed. Real evidence: a
        `[delivery-unconfirmed] codex ...` notice arrived at Lead's pane
        AFTER that codex pane had already finished its task and closed.

        Called right before a matching notice is actually written — if the
        target role's pane has since reached a terminal state (closed,
        crashed, or simply never existed to begin with — nothing left to
        confirm or deny), the underlying claim ("still hasn't reached ready
        / still stuck") can no longer be true, so *body* is prefixed with a
        "resolved since" banner instead of being presented as Lead's
        current reality. The original body is kept, not dropped — Lead may
        still want the history (e.g. to notice a pattern of stalls for that
        role), just not mistake it for the present.

        Bodies that aren't one of these 5 markers (including plain done/CC/
        FAILED notices) pass through completely unchanged — this only ever
        touches the specific claim class that's cheaply re-checkable via
        the pane registry alone.
        """
        role = _system_marker_role(body)
        if role is None:
            return body
        status = self.list_status(project=project_ns)
        state = status.get(role)
        if state is not None and state not in _DIGEST_TERMINAL_PANE_STATES:
            return body  # role's pane is still around and non-terminal — still live
        return (
            "✅ [คลี่คลายแล้ว — pane ของ role นี้ปิด/จบไปแล้วตั้งแต่ตอนที่รอส่งข้อความนี้อยู่ "
            "ข้อความด้านล่างเป็นภาพ ณ ตอนเกิดเหตุ ไม่ใช่สถานะปัจจุบัน]\n" + body
        )

    def _notify_lead(
        self,
        project_ns: str,
        body: str,
        *,
        from_role: str = "system",
        note: str = "notify",
        pane_token: str | None = None,
        digest_facts: DigestFacts | None = None,
        queued_ts: float | None = None,
    ) -> None:
        """Queue *body* for delivery to the Lead pane.

        If Lead is alive, clean done and peer-CC notices are debounced into
        one digest (`_INBOX_DIGEST_WINDOW_MS`, 15s by default) — UNLESS
        (#264) no other role in the project is still active, in which case
        the much shorter `_INBOX_DIGEST_ADAPTIVE_SETTLE_MS` window applies
        instead, since there's nothing left to wait for a combine with. See
        `_other_roles_still_active`. Blocking failures and sequencing
        handoffs bypass the window entirely. The ready-prompt-aware pump
        then serialises writes so concurrent notices never overwrite each
        other mid-generation.

        If Lead is absent the item falls directly into the durable
        _pending_done_notices (survives a restart, delivered on next Lead spawn).

        *from_role* and *note* are stored only in the durable fallback record so
        callers that care about the audit trail can pass them through; the live
        delivery path only needs *body*. *pane_token* (#228) is the calling
        pane's own auth token, captured by `done()` before any teardown — it
        rides along through every queue so a delivery that happens after the
        role slot was respawned can be flagged instead of silently looking
        like it came from the pane currently running under that name.
        *digest_facts* (#245) rides along ONLY into the digest queue — the
        cockpit-computed fact table `done()` built for this notice, rendered
        by `_format_digest_item` instead of re-parsing `body` as prose. None
        for every notice that isn't a clean `done()` report (FAILED/blocking
        notices never reach the digest queue at all, so it never matters
        there).

        Lazy-initialises queue / pumping-set so partial test fixtures (those that
        use Orchestrator.__new__ and bypass __init__) don't need to pre-populate
        these attributes.

        *queued_ts* (#266): the wall-clock the underlying event actually
        OCCURRED, defaulting to now. A caller re-routing an item that was
        already queued once (durable requeue, e.g. `_flush_pending_done_notices`)
        should pass the ORIGINAL timestamp through here rather than letting
        it default — otherwise every requeue hop resets "when did this
        happen" to "whenever it happened to be re-processed", defeating the
        whole point of stamping it.
        """
        occurred_ts = queued_ts if queued_ts is not None else time.time()
        lead = self._project_panes(project_ns).get(LEAD.name)
        if lead and lead.session and lead.session.is_alive:
            window_ms = _inbox_digest_window_ms()
            digestible = _is_digestible_lead_notice(body)
            immediate = _is_immediate_lead_notice(body)
            blocking = _is_blocking_lead_notice(body)

            if digestible and not immediate and window_ms > 0:
                if not hasattr(self, "_lead_digest_queue"):
                    self._lead_digest_queue = {}
                self._lead_digest_queue.setdefault(project_ns, collections.deque()).append(
                    (body, pane_token, occurred_ts, digest_facts)
                )
                # #264: adaptive window — if nothing else could plausibly
                # still join this burst, don't make Lead pay the full
                # window's tax waiting for a combine that will never
                # happen. Re-evaluated on EVERY notice added to the burst
                # (each one re-arms via _arm_lead_digest), so the LAST
                # item in a genuine multi-role burst is the one that
                # switches to the short settle — not the first.
                if not self._other_roles_still_active(project_ns, from_role):
                    window_ms = min(window_ms, _INBOX_DIGEST_ADAPTIVE_SETTLE_MS)
                self._arm_lead_digest(project_ns, window_ms)
            elif blocking:
                # Failures are explicitly outside the digest policy. Do not
                # make Lead read an older informational digest turn before the
                # blocking alert; the digest keeps its original timer.
                self._enqueue_live_lead_notice(
                    project_ns, body, front=True, pane_token=pane_token, queued_ts=occurred_ts
                )
                self._arm_lead_notify_pump(project_ns)
            elif "[auto-chain handoff]" in body.lower():
                combined = self._flush_lead_digest(
                    project_ns,
                    arm_pump=False,
                    trailing_body=body,
                )
                if not combined:
                    self._enqueue_live_lead_notice(
                        project_ns, body, pane_token=pane_token, queued_ts=occurred_ts
                    )
                self._arm_lead_notify_pump(project_ns)
            else:
                # A sequencing handoff must not sit behind the debounce window.
                # Flush older done/CC mail first so auto-chain still sees the
                # done notes it explicitly tells Lead to re-read. Other
                # immediate engine notices follow the same chronological rule.
                self._flush_lead_digest(project_ns, arm_pump=False)
                self._enqueue_live_lead_notice(
                    project_ns, body, pane_token=pane_token, queued_ts=occurred_ts
                )
                self._arm_lead_notify_pump(project_ns)

            # Tell the UI Lead has new mail so it can red-dot the Lead pane-tab
            # when the user is on another pane. Best-effort: a partial test
            # fixture built via Orchestrator.__new__ won't have the bound signal.
            try:
                self.leadNotified.emit(project_ns)
            except Exception:
                pass
        else:
            if not hasattr(self, "_pending_done_notices"):
                self._pending_done_notices = {}
            self._pending_done_notices.setdefault(project_ns, []).append(
                {
                    "role": from_role,
                    "note": note,
                    "body": body,
                    "pane_token": pane_token,
                    "queued_ts": occurred_ts,
                }
            )
            self._save_pending_done_notices(project_ns)
            _log_event("done_notice_queued", project=project_ns, role=from_role)

        # Legacy mode (window=0) reaches the immediate branch above and retains
        # byte-for-byte single-notice delivery.

    def _arm_lead_notify_pump(self, project_ns: str) -> None:
        """Start a pump for *project_ns* if one is not already running.

        Calls _pump_lead_notify directly (synchronously) for the first attempt so
        tests that do not run a Qt event loop still see the write happen.  Retries
        when Lead is busy are scheduled via QTimer.singleShot.
        """
        pumping: set = getattr(self, "_lead_notify_pumping", set())
        if not hasattr(self, "_lead_notify_pumping"):
            self._lead_notify_pumping = pumping
        if project_ns in pumping:
            return
        pumping.add(project_ns)
        self._pump_lead_notify(project_ns)

    def _pump_lead_notify(self, project_ns: str) -> None:
        """Deliver one notice to Lead when it is at the ready prompt, then re-arm.

        Serialises concurrent done-notices so they never overwrite each other
        mid-generation.  Falls back to _pending_done_notices when Lead dies
        while items are still in the queue.

        Busy-retry cap: after LEAD_NOTIFY_BUSY_CAP consecutive retries (~30 s) the
        remaining items are spilled to _pending_done_notices so they survive a crash
        and the hot-loop stops.
        """
        queue = getattr(self, "_lead_notify_queue", {}).get(project_ns)
        if not queue:
            pumping: set = getattr(self, "_lead_notify_pumping", set())
            pumping.discard(project_ns)
            getattr(self, "_lead_notify_retry", {}).pop(project_ns, None)
            return

        # #133: a previous item's self-heal submit-verify chain is still
        # deciding whether ITS write landed. Writing a second item now — even
        # though is_at_ready_prompt() may already read True again — is exactly
        # what raced two independent chains into the same Lead composer
        # (events.log: duplicate `remaining: 3` lead_notify_repaste entries
        # proved two chains, not one bursty one). Wait for on_settled instead
        # of trusting the ready-prompt read alone. Not counted against the
        # busy-retry cap — that budget is for a genuinely wedged Lead, a
        # different condition from "our own last write hasn't resolved yet".
        if project_ns in getattr(self, "_lead_notify_verify_active", set()):
            QTimer.singleShot(400, lambda: self._pump_lead_notify(project_ns))
            return

        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            # Lead died — move remaining items to the durable queue.
            items = list(queue)
            queue.clear()
            pumping = getattr(self, "_lead_notify_pumping", set())
            pumping.discard(project_ns)
            getattr(self, "_lead_notify_retry", {}).pop(project_ns, None)
            if not hasattr(self, "_pending_done_notices"):
                self._pending_done_notices = {}
            for b in items:
                b_body, b_pane_token, b_ts = _unwrap_notice_item(b)
                self._pending_done_notices.setdefault(project_ns, []).append(
                    {
                        "role": "system",
                        "note": "notify",
                        "body": b_body,
                        "pane_token": b_pane_token,
                        "queued_ts": b_ts,
                    }
                )
            if items:
                self._save_pending_done_notices(project_ns)
            return

        if not lead.session.is_at_ready_prompt():
            # Lead is busy — check retry cap before re-scheduling.
            if not hasattr(self, "_lead_notify_retry"):
                self._lead_notify_retry = {}
            count = self._lead_notify_retry.get(project_ns, 0) + 1
            self._lead_notify_retry[project_ns] = count
            if count > LEAD_NOTIFY_BUSY_CAP:
                # Lead has been wedged too long — spill to durable and stop.
                items = list(queue)
                queue.clear()
                pumping = getattr(self, "_lead_notify_pumping", set())
                pumping.discard(project_ns)
                self._lead_notify_retry.pop(project_ns, None)
                if not hasattr(self, "_pending_done_notices"):
                    self._pending_done_notices = {}
                for b in items:
                    b_body, b_pane_token, b_ts = _unwrap_notice_item(b)
                    self._pending_done_notices.setdefault(project_ns, []).append(
                        {
                            "role": "system",
                            "note": "notify_spill",
                            "body": b_body,
                            "pane_token": b_pane_token,
                            "queued_ts": b_ts,
                        }
                    )
                if items:
                    self._save_pending_done_notices(project_ns)
                _log_event("lead_notify_spill", project=project_ns, count=len(items))
                return
            # Retry after a short delay without consuming the item.
            QTimer.singleShot(400, lambda: self._pump_lead_notify(project_ns))
            return

        if not self._lead_can_accept_injection(project_ns):
            # Lead is idle but the user has an unsubmitted draft in its input
            # line (issue #3) — holding here instead of pasting over it is
            # the actual fix. Distinct from the busy-retry cap above: this is
            # gated on wall-clock (LeadDraftState.pending_since), not a retry
            # count, since other callers of the same gate poll on different
            # cadences. Past the hold timeout, spill rather than clobber the
            # draft indefinitely — same durable fallback + red-dot mechanism
            # as the busy-cap spill above.
            if self._lead_draft_hold_expired(project_ns):
                items = list(queue)
                queue.clear()
                pumping = getattr(self, "_lead_notify_pumping", set())
                pumping.discard(project_ns)
                getattr(self, "_lead_notify_retry", {}).pop(project_ns, None)
                if not hasattr(self, "_pending_done_notices"):
                    self._pending_done_notices = {}
                for b in items:
                    b_body, b_pane_token, b_ts = _unwrap_notice_item(b)
                    self._pending_done_notices.setdefault(project_ns, []).append(
                        {
                            "role": "system",
                            "note": "notify_draft_spill",
                            "body": b_body,
                            "pane_token": b_pane_token,
                            "queued_ts": b_ts,
                        }
                    )
                if items:
                    self._save_pending_done_notices(project_ns)
                _log_event("lead_notify_draft_spill", project=project_ns, count=len(items))
            else:
                QTimer.singleShot(400, lambda: self._pump_lead_notify(project_ns))
            return

        # Lead is alive and idle — deliver one item; reset retry counter.
        getattr(self, "_lead_notify_retry", {}).pop(project_ns, None)
        # Deliver-then-ack: peek instead of pop so a write() exception (session
        # torn down between the liveness checks above and this write) never
        # drops the item — see HIGH#1,
        # docs/reviews/2026-07-11-full-system-review-codex.md.
        raw_body, item_pane_token, item_ts = _unwrap_notice_item(queue[0])
        # #266: re-check delivery-health system notices right before they
        # actually land — see _revalidate_system_notice's docstring for why
        # (the queue can sit for minutes between enqueue and this point).
        # No-op for every other notice shape (plain done/CC/FAILED bodies).
        raw_body = self._revalidate_system_notice(project_ns, raw_body)
        item_role = _notice_role_tag(raw_body)
        if self._provenance_stale(project_ns, item_role, item_pane_token):
            raw_body = f"{_STALE_ORIGIN_BANNER.format(role=item_role)}\n{raw_body}"
        # #266: stamp every notice with when it actually occurred, not just
        # when it happened to be flushed — this queue previously carried no
        # timestamp at all (see _enqueue_live_lead_notice's docstring).
        raw_body = f"{_occurred_stamp(item_ts)}{raw_body}"
        body = _sanitize_pane_text(raw_body)
        _notify_sess = lead.session
        payload = _paste_payload(body)
        notice_expires_at = time.time() + float(
            os.environ.get("TAKKUB_TASK_DELIVERY_TTL_SEC", "30")
        )
        try:
            wrote = _safe_session_write(
                _notify_sess,
                payload,
                priority=WritePriority.TASK,
                kind="notification",
                expires_at=notice_expires_at,
                session_generation=getattr(lead, "_session_generation", None),
            )
            if not wrote:
                raise RuntimeError("PTY writer queue rejected Lead notification")
        except Exception:
            # The write failed — this item and anything queued behind it are
            # still unsent. Spill the whole live queue to the durable store
            # rather than lose it; a torn-down session will not recover
            # mid-pump, so retrying live here would just fail again.
            items = list(queue)
            queue.clear()
            pumping = getattr(self, "_lead_notify_pumping", set())
            pumping.discard(project_ns)
            getattr(self, "_lead_notify_retry", {}).pop(project_ns, None)
            if not hasattr(self, "_pending_done_notices"):
                self._pending_done_notices = {}
            for b in items:
                b_body, b_pane_token, b_ts = _unwrap_notice_item(b)
                self._pending_done_notices.setdefault(project_ns, []).append(
                    {
                        "role": "system",
                        "note": "notify_write_failed",
                        "body": b_body,
                        "pane_token": b_pane_token,
                        "queued_ts": b_ts,
                    }
                )
            self._save_pending_done_notices(project_ns)
            _log_event("lead_notify_write_failed", project=project_ns, count=len(items))
            return
        # Write succeeded — now it is safe to dequeue.
        queue.popleft()
        delay = _enter_delay_ms(payload)
        # #133: mark the chain in flight BEFORE scheduling it so no other
        # writer (a re-entrant pump, _force_deliver_done_notices) can slip a
        # write in first. on_settled is the only place this clears.
        if not hasattr(self, "_lead_notify_verify_active"):
            self._lead_notify_verify_active = set()
        self._lead_notify_verify_active.add(project_ns)

        def _on_verify_settled(p=project_ns) -> None:
            getattr(self, "_lead_notify_verify_active", set()).discard(p)

        # Self-healing submit: a done-report whose Enter is swallowed mid-paste-
        # render leaves Lead idle with the report unsubmitted — it "won't run on"
        # (issue #22 residual). Verify the submit landed and resend if not.
        _orch_attr("_delayed_enter_verified", _delayed_enter_verified)(
            lead,
            _notify_sess,
            delay,
            payload=None,
            content_fragment=body,
            on_resend=lambda rem: _log_event(
                "lead_notify_enter_resend", project=project_ns, remaining=rem
            ),
            on_repaste=lambda rem: _log_event(
                "lead_notify_repaste", project=project_ns, remaining=rem
            ),
            on_settled=_on_verify_settled,
            expires_at=notice_expires_at,
        )
        self.leadInjected.emit(body)

        if queue:
            # More items waiting — re-arm after this item has had time to submit
            # and Lead has begun processing it (so is_at_ready_prompt drops).
            QTimer.singleShot(delay + 300, lambda: self._pump_lead_notify(project_ns))
        else:
            pumping = getattr(self, "_lead_notify_pumping", set())
            pumping.discard(project_ns)

    def _flush_pending_done_notices(self, project_ns: str) -> None:
        """Deliver queued done notices to Lead if it is currently alive.

        Called after Lead spawns. Routes all items through _notify_lead so they
        enter the ready-prompt-aware queue instead of being dumped all at once."""
        pending = self._pending_done_notices.get(project_ns)
        if not pending:
            return
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        if not self._lead_can_accept_injection(project_ns):
            # User has an unsubmitted draft — leave items durable and back off
            # silently. Handing them to _notify_lead here would move them into
            # the live queue only for _pump_lead_notify's own draft-hold gate to
            # spill them straight back to durable (draft-hold-expired is sticky
            # once past DRAFT_HOLD_TIMEOUT_S), and doing that per item produced
            # a duplicate spill log per notice, every reaper tick (#108). Items
            # stay parked here indefinitely — the reaper's staleness clock
            # (_pending_done_since) never force-bypasses a genuine draft-hold
            # (#118: force-flush clobbering an unsubmitted draft mid-keystroke
            # is worse than a late notice); it only escalates the not-ready
            # branch.
            return
        # Deliver-then-ack, one item at a time (HIGH#1,
        # docs/reviews/2026-07-11-full-system-review-codex.md): process a fixed
        # snapshot of the items present when this flush started, removing each
        # from the durable list right before handing it to _notify_lead rather
        # than popping/persisting the whole list empty up front. A crash
        # between any two items then loses nothing — items not yet reached are
        # still on disk. The snapshot (not a live re-check) also bounds this
        # loop to exactly len(items) iterations even though _notify_lead's own
        # synchronous pump may re-spill a failed item back onto this same
        # durable list — without the snapshot that re-spill would be picked
        # back up and reprocessed forever.
        items = list(pending)
        transferred = 0
        for item in items:
            current = self._pending_done_notices.get(project_ns)
            if current:
                current.pop(0)
                if not current:
                    self._pending_done_notices.pop(project_ns, None)
                self._save_pending_done_notices(project_ns)
            try:
                # #266: preserve the ORIGINAL occurrence time through this
                # requeue hop — without it, every durable→live round-trip
                # would reset "when did this happen" to "whenever it got
                # reprocessed", defeating _occurred_stamp's whole purpose.
                self._notify_lead(
                    project_ns,
                    item["body"],
                    pane_token=item.get("pane_token"),
                    queued_ts=item.get("queued_ts"),
                )
            except Exception:
                self._pending_done_notices.setdefault(project_ns, []).insert(0, item)
                self._save_pending_done_notices(project_ns)
                _log_event("done_notices_flush_failed", project=project_ns, transferred=transferred)
                return
            transferred += 1
        _log_event("done_notices_flushed", project=project_ns, count=transferred)

    def _reap_pending_done_notices(self) -> None:
        """Flush durable done-notices for any project whose Lead is idle.

        Runs on every idle-watchdog tick so notices spilled while Lead was busy
        (durability-cap exceeded) are delivered as soon as Lead returns to the
        ready prompt — without needing a restart.  Skips projects whose Lead is
        absent or still busy to avoid ping-pong (flush → re-spill → flush loop).
        """
        pending = getattr(self, "_pending_done_notices", None)
        if not pending:
            return
        if not hasattr(self, "_pending_done_since"):
            self._pending_done_since = {}
        now = time.time()
        for project_ns in list(pending.keys()):
            lead = self._project_panes(project_ns).get(LEAD.name)
            if not (lead and lead.session and lead.session.is_alive):
                # Lead absent — leave items durable for the next spawn; reset the
                # staleness clock so it only counts time the Lead was actually up.
                self._pending_done_since.pop(project_ns, None)
                continue
            if not lead.session.is_at_ready_prompt():
                # Not-ready is a transient, non-user-caused condition (Lead
                # mid-turn, or an is_at_ready_prompt() false-negative from a
                # blocker marker in its own visible conversation — #20) that
                # silently stranded notices forever (#70: a second active
                # project's Lead never reaped). Escalate: after
                # _DONE_NOTICE_STALE_S of an alive-but-never-ready Lead, force
                # delivery regardless of the gate so the chain can never stall
                # indefinitely.
                since = self._pending_done_since.setdefault(project_ns, now)
                if now - since >= _DONE_NOTICE_STALE_S:
                    _log_event(
                        "done_notice_force_flush",
                        project=project_ns,
                        stalled_s=round(now - since),
                        count=len(pending.get(project_ns, [])),
                    )
                    self._pending_done_since.pop(project_ns, None)
                    self._force_deliver_done_notices(project_ns)
            elif self._lead_can_accept_injection(project_ns):
                self._pending_done_since.pop(project_ns, None)
                self._flush_pending_done_notices(project_ns)
            else:
                # Ready but a draft is genuinely sitting unsubmitted in the
                # input line (#108/#118: user typing, not a stuck engine
                # state). Unlike not-ready, this is user-caused and must NEVER
                # be force-bypassed — clobbering a live draft mid-keystroke is
                # worse than a late notice. Park indefinitely in the durable
                # queue until the draft clears on its own, then the
                # ready+can-accept branch above flushes it normally. Reset
                # the staleness clock so a prior not-ready streak can't leak
                # into a force-flush once the state flips to draft-blocked.
                #
                # #163: this branch has genuinely no delivery ceiling by
                # design (see above) — a real draft-hold can keep a done
                # report parked here for as long as the draft sits unsent, no
                # matter how long. That's an intentional tradeoff, not a gap
                # this fix reopens: `_force_deliver_done_notices` pastes
                # blind into the composer, so using it here would reintroduce
                # exactly the clobbering #118 fixed, just on a longer fuse.
                # Instead, `list_status`/`list_status_detailed`
                # (`_has_pending_lead_notice`, orchestrator.py) surface the
                # role as pending via `takkub list`/`status` — a read-only
                # channel that stays safe to consult regardless of composer
                # state, so Lead (or whoever is watching it) is never left
                # with zero signal a report is stuck, even while this branch
                # itself keeps waiting for the draft to clear.
                self._pending_done_since.pop(project_ns, None)

    def _force_deliver_done_notices(self, project_ns: str) -> None:
        """Last-resort delivery for spilled done-notices when the Lead reads as
        perpetually not-ready (is_at_ready_prompt() false-negative). Bypasses the
        ready gate that _flush/_pump honour, pasting the spilled notices as a
        single combined message (one paste + one verified submit, so no
        clobbering) into the live Lead. Used only by the reaper's staleness
        escalation — see _DONE_NOTICE_STALE_S (#70)."""
        pending = getattr(self, "_pending_done_notices", {}).get(project_ns)
        if not pending:
            return
        # #133: never write into Lead's composer while _pump_lead_notify's own
        # chain is still verifying an earlier write — same guard, same shared
        # set, so the two writers can't race each other either.
        if project_ns in getattr(self, "_lead_notify_verify_active", set()):
            return
        lead = self._project_panes(project_ns).get(LEAD.name)
        if not (lead and lead.session and lead.session.is_alive):
            return
        # Deliver-then-ack (HIGH#1,
        # docs/reviews/2026-07-11-full-system-review-codex.md): only pop/persist
        # empty once the write is known to have succeeded, so a torn-down
        # session mid-write leaves the items durable for the next attempt
        # instead of vanishing.
        valid = [
            item for item in pending if isinstance(item, dict) and isinstance(item.get("body"), str)
        ]
        if not valid:
            self._pending_done_notices.pop(project_ns, None)
            self._save_pending_done_notices(project_ns)
            return

        def _flagged(item: dict) -> str:
            # #266: same re-validate + occurred-at stamping _pump_lead_notify
            # applies to its own live-queue delivery — this force-deliver
            # path exists specifically for items that have been stuck
            # durable through _DONE_NOTICE_STALE_S (60s) of a not-ready
            # Lead, so it's at LEAST as likely to be carrying stale claims.
            item_body = self._revalidate_system_notice(project_ns, item.get("body", ""))
            role = _notice_role_tag(item_body)
            if self._provenance_stale(project_ns, role, item.get("pane_token")):
                item_body = f"{_STALE_ORIGIN_BANNER.format(role=role)}\n{item_body}"
            item_body = f"{_occurred_stamp(item.get('queued_ts'))}{item_body}"
            return _sanitize_pane_text(item_body)

        body = "\n\n".join(_flagged(item) for item in valid)
        sess = lead.session
        payload = _paste_payload(body)
        notice_expires_at = time.time() + float(
            os.environ.get("TAKKUB_TASK_DELIVERY_TTL_SEC", "30")
        )
        try:
            wrote = _safe_session_write(
                sess,
                payload,
                priority=WritePriority.TASK,
                kind="notification",
                expires_at=notice_expires_at,
                session_generation=getattr(lead, "_session_generation", None),
            )
            if not wrote:
                raise RuntimeError("PTY writer queue rejected forced Lead notification")
        except Exception:
            _log_event("done_notice_force_deliver_failed", project=project_ns, count=len(pending))
            return
        self._pending_done_notices.pop(project_ns, None)
        self._save_pending_done_notices(project_ns)
        delay = _enter_delay_ms(payload)
        if not hasattr(self, "_lead_notify_verify_active"):
            self._lead_notify_verify_active = set()
        self._lead_notify_verify_active.add(project_ns)

        def _on_verify_settled(p=project_ns) -> None:
            getattr(self, "_lead_notify_verify_active", set()).discard(p)

        _orch_attr("_delayed_enter_verified", _delayed_enter_verified)(
            lead,
            sess,
            delay,
            payload=None,
            content_fragment=body,
            on_resend=lambda rem: _log_event(
                "done_notice_force_enter_resend", project=project_ns, remaining=rem
            ),
            on_repaste=lambda rem: _log_event(
                "done_notice_force_repaste", project=project_ns, remaining=rem
            ),
            on_settled=_on_verify_settled,
            expires_at=notice_expires_at,
        )
        self.leadInjected.emit(body)
