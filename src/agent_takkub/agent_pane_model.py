"""AgentPaneModel: session + state bookkeeping for one agent slot, split out
of AgentPane so the engine can eventually operate without a display
(issue #105 Phase A — see docs/design/2026-07-11-105-phaseA-pane-split.md).

Owns everything the orchestrator/watchdogs need to read or drive: the
PtySession reference, pane state ("empty"/"active"/"working"/"done"/
"exited"/"error"), the last-report note, worktree-branch tag, token-meter
bookkeeping, and the throughput/idle timestamps the stuck-pane and
runaway-throughput watchdogs poll.

Deliberately has NO QWidget/QWebEngineView/terminal dependency and no
rendering logic (xterm.js buffering, spinner animation, header widgets stay
in agent_pane.py) — this module is importable and unit-testable without a
display. AgentPane wraps one instance of this class per pane and proxies its
state-bearing attributes onto it (see the `session`/`state`/... properties
in agent_pane.py) so the rest of the codebase's `pane.session`/`pane.state`
call sites need no changes.
"""

from __future__ import annotations

from .pty_session import PtySession
from .roles import LEAD, Role
from .token_meter import effective_context_limit, format_tokens, usage_color


class AgentPaneModel:
    """Session + state for one agent slot (no view)."""

    def __init__(self, role: Role) -> None:
        self.role = role
        self.state: str = "empty"
        self.last_note: str | None = None
        self.session: PtySession | None = None

        # Isolated git worktree branch (issue #81) — None = shared cwd.
        self.worktree_branch: str | None = None

        # Teardown guard: True once orchestrator.close()/done() called
        # terminate first, so the next exit is "expected" rather than a crash.
        self.expected_exit: bool = False

        # Bumped on every attach_session so a stale processExited from a
        # replaced session can be told apart from the current one.
        self.session_generation: int = 0

        # Wall-clock of the most recent PTY byte — the stuck-pane watchdog's
        # silence timer.
        self.last_output_ts: float = 0.0
        # Monotonic byte counter — the runaway-throughput watchdog's rate data.
        self.tp_total_bytes: int = 0

        # Token-meter bookkeeping (see AgentPane._refresh_token_meter).
        self.spawn_ts: float = 0.0
        self.session_cwd: str | None = None
        self.session_jsonl: object | None = None
        self.last_usage: dict | None = None
        # #103: the raw token_meter.read_pane_usage() result of every status
        # ("ok"/"unsupported"/"no_data"), for remote/api.py's DATA-MIN-safe
        # /api/activity "context" field via token_meter_context() below.
        # `last_usage` above stays "ok"-only — status_header's tab-color
        # aggregation and the session-cap watchdog must keep seeing exactly
        # the pre-#103 contract (numeric usage or None), never a status dict.
        self.last_usage_raw: dict | None = None
        # This pane's own Claude session transcript uuid (PaneState.session_uuid
        # mirror — see AgentPane.attach_session). The token meter resolves its
        # JSONL by this exact uuid, never by newest-mtime-in-cwd, so panes
        # sharing a cwd (issue #129) can't read each other's usage numbers.
        # None until spawn (or a later /resume-triggered SessionStart hook)
        # reports it.
        self.session_uuid: str | None = None
        # Provider capability is set by AgentPane.attach_session(). Claude is
        # currently the only provider whose JSONL usage schema token_meter can
        # read (#103); other providers must not arm the session-cap watchdog.
        self.provider_name: str | None = None
        self.supports_token_meter: bool = False
        # Edge-trigger latch: warn once while at/above the cap, then re-arm
        # only after a later usage sample falls below it (e.g. /compact).
        self.session_cap_warning_active: bool = False
        # Known context cap for the token badge (None = derive per-model).
        self.context_limit: int | None = None
        if role.name == LEAD.name:
            from .plan_tier import is_pro

            self.context_limit = None if is_pro() else 1_000_000

        # Set by spawn_engine after a pipeline hop spawns this pane.
        self.transcript_path: object | None = None

    def mark_expected_exit(self) -> None:
        """Called before terminate so the next exit notification isn't
        treated as a crash."""
        self.expected_exit = True

    def current_usage(self) -> dict | None:
        """Last-known usage dict for status-bar aggregation, or None if this
        pane has no active session / hasn't logged a turn yet."""
        if self.session is None:
            return None
        return self.last_usage

    def record_token_meter_result(self, usage: dict | None) -> None:
        """Store the latest `token_meter.read_pane_usage()` result of ANY
        status for `token_meter_context()` below, and — only when
        `status == "ok"` — additionally mirror it into `last_usage` the way
        `current_usage()`/status-bar aggregation/the session-cap watchdog
        already require (numeric usage or None, never a status dict)."""
        self.last_usage_raw = usage
        if usage is not None and usage.get("status", "ok") == "ok":
            self.last_usage = usage

    def token_meter_context(self) -> dict | None:
        """DATA-MIN-safe summary of the latest token-meter read — numbers and
        a coarse status tag only, no path/model text — for remote/api.py's
        `/api/activity` "context" field. None when this pane's provider never
        armed the meter or no session has resolved yet (same "nothing to
        show" meaning `current_usage()` already has)."""
        if self.session is None or self.last_usage_raw is None:
            return None
        usage = self.last_usage_raw
        status = usage.get("status", "ok")
        if status != "ok":
            return {"prompt": None, "limit": None, "pct": None, "status": status}
        prompt = usage["prompt"]
        limit = usage.get("limit") or effective_context_limit(
            usage["model"], prompt, base=self.context_limit
        )
        pct = round((prompt / limit) * 100) if limit else None
        return {"prompt": prompt, "limit": limit, "pct": pct, "status": "ok"}

    def set_worktree_branch(self, branch: str | None) -> None:
        self.worktree_branch = branch or None

    def set_session_uuid(self, session_uuid: str | None) -> None:
        """Update this pane's known session transcript uuid — called at
        attach (spawn-time value) and again whenever the `SessionStart`
        hook reports a rollover (manual `/resume`/`/clear`), so the token
        meter never chases a stale uuid after the user switches sessions
        mid-pane."""
        self.session_uuid = session_uuid or None

    def configure_provider(self, provider_name: str, *, supports_token_meter: bool) -> None:
        """Reset meter/watchdog state for a newly attached provider session."""
        self.provider_name = provider_name
        self.supports_token_meter = bool(supports_token_meter)
        self.session_cap_warning_active = False

    def observe_session_cap(self, prompt: int, threshold: int | None) -> bool:
        """Return True exactly once for each below→at/above cap crossing.

        `threshold=None` means the watchdog is disabled for this pane (cap
        ratio configured to 0) — always returns False.
        """
        if not self.supports_token_meter or threshold is None:
            self.session_cap_warning_active = False
            return False
        if prompt < threshold:
            self.session_cap_warning_active = False
            return False
        if self.session_cap_warning_active:
            return False
        self.session_cap_warning_active = True
        return True

    def reset_session_cap_watchdog(self) -> None:
        """Re-arm after a transcript rollover or session teardown."""
        self.session_cap_warning_active = False

    def decide_exit_state(self, code: int) -> tuple[str, str | None]:
        """Pure decision for what a process exit should transition state to.

        Expected exits (orchestrator.close()/done() already called terminate,
        or the pane was already in "done") land back on "empty". Anything
        else is an unexpected crash — surface "exited" so the user can retry.
        """
        if self.state == "done" or self.expected_exit:
            return "empty", None
        return "exited", f"agent process exited unexpectedly (code {code})"

    def format_token_badge(self, usage: dict) -> dict:
        """Pure formatting for the header token badge — factored out of the
        view so it's unit-testable without a QLabel.

        Caller must only pass a usage dict whose `status` is `"ok"` (or a
        pre-#103 claude dict with no `status` key at all — same thing).
        `"unsupported"`/`"no_data"` go through `format_unsupported_badge`
        instead; this method assumes every numeric key is present.
        """
        prompt = usage["prompt"]
        # A provider-reported `limit` (codex's model_context_window, kimi's
        # max_context_tokens) is authoritative — trust it over the per-model
        # table, which only knows claude model ids and would otherwise cap a
        # 258k-context codex turn at the wrong 200k default. None (claude,
        # and any provider with no live cap to report) falls through to the
        # table exactly as before #103.
        limit = usage.get("limit") or effective_context_limit(
            usage["model"], prompt, base=self.context_limit
        )
        pct = (prompt / limit) if limit else 0.0
        return {
            "text": f"{format_tokens(prompt)}/{format_tokens(limit)} · {int(pct * 100)}%",
            "color": usage_color(pct),
            "limit": limit,
            "tooltip": (
                f"model: {usage['model']}\n"
                f"prompt: {usage['prompt']:,} tokens  (input {usage['input']:,} + "
                f"cache write {usage['cache_creation']:,} + cache read {usage['cache_read']:,})\n"
                f"output: {usage['output']:,} tokens\n"
                f"context limit: {limit:,}"
            ),
        }

    def format_unsupported_badge(self, usage: dict) -> dict:
        """Pure formatting for a pane whose provider armed the token meter
        (`ProviderSpec.supports_token_meter=True`) but this poll came back
        `"unsupported"` (confirmed no token data exists for this provider) or
        `"no_data"` (this pane hasn't logged a turn yet). Faint/neutral by
        design — this is informational chrome, not a warning."""
        return {
            "text": "tokens n/a",
            "color": usage_color(0.0),  # neutral grey — same tier as <50% fill
            "tooltip": usage.get("reason") or "token usage unavailable for this provider",
        }
