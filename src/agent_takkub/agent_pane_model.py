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

    def set_worktree_branch(self, branch: str | None) -> None:
        self.worktree_branch = branch or None

    def decide_exit_state(self, code: int) -> tuple[str, str | None]:
        """Pure decision for what a process exit should transition state to.

        Expected exits (orchestrator.close()/done() already called terminate,
        or the pane was already in "done") land back on "empty". Anything
        else is an unexpected crash — surface "exited" so the user can retry.
        """
        if self.state == "done" or self.expected_exit:
            return "empty", None
        return "exited", f"claude exited unexpectedly (code {code})"

    def format_token_badge(self, usage: dict) -> dict:
        """Pure formatting for the header token badge — factored out of the
        view so it's unit-testable without a QLabel."""
        prompt = usage["prompt"]
        limit = effective_context_limit(usage["model"], prompt, base=self.context_limit)
        pct = (prompt / limit) if limit else 0.0
        return {
            "text": f"{format_tokens(prompt)}/{format_tokens(limit)} · {int(pct * 100)}%",
            "color": usage_color(pct),
            "tooltip": (
                f"model: {usage['model']}\n"
                f"prompt: {usage['prompt']:,} tokens  (input {usage['input']:,} + "
                f"cache write {usage['cache_creation']:,} + cache read {usage['cache_read']:,})\n"
                f"output: {usage['output']:,} tokens\n"
                f"context limit: {limit:,}"
            ),
        }
