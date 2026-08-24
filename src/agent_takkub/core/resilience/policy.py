"""Central fail-open policy table (`10_FAIL_OPEN_MATRIX.md`) — one place that
maps every optional/best-effort dependency in the cockpit to a label and a
fallback description, instead of that knowledge living only as scattered
`except Exception: ...` comments across a dozen modules (the audit's own
finding: "fail-open is component-local (each module try/except) and works;
no central policy labels").

This module is a static data table only — it does not itself wrap any call.
`circuit_breaker.get_breaker(name)` is what a caller actually threads through
a real call; `POLICY[name].breaker` just records whether that wiring exists
today, so `doctor_section.py` can render an honest picture instead of
silently pretending every entry is breaker-protected.
"""

from __future__ import annotations

from dataclasses import dataclass


class Label:
    """`10_FAIL_OPEN_MATRIX.md`'s four labels, plus `WARNING` for a
    best-effort local write/read that was never a "call a dependency" in the
    first place (trace write, git status) — the matrix lists it separately
    from the other three so it stays that way here too.

    FATAL     — would abort the assignment. Nothing in this table uses it
                today: every dependency this codebase treats as "optional"
                is, by definition, one whose failure must never fail an
                assignment (plan §0 rules 3+4). Kept as a label so a FUTURE
                genuinely-required dependency has somewhere to be classified
                as fatal instead of silently reusing DEGRADED.
    DEGRADED  — continues, but with a materially reduced result (files
                fallback instead of a code graph, native context instead of
                retrieval).
    OPTIONAL  — purely additive; skipping it loses nothing the task strictly
                needed (a design reference, an inspiration search).
    RETRYABLE — transient by nature; a circuit breaker's half-open probe is
                the right tool (a local store that's momentarily locked, a
                service that's down right now but not permanently).
    WARNING   — logged, never surfaced to the task path; the operation was
                always best-effort persistence/observability, not a
                dependency the task result depends on.
    """

    FATAL = "fatal"
    DEGRADED = "degraded"
    OPTIONAL = "optional"
    RETRYABLE = "retryable"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ServicePolicy:
    service: str
    label: str
    fallback: str
    breaker: bool
    reason: str = ""


POLICY: dict[str, ServicePolicy] = {
    # design_clients.py — every call goes through `_safe_call`, which is now
    # the one choke point wired to `circuit_breaker.get_breaker(SOURCE)`.
    "figma": ServicePolicy("figma", Label.OPTIONAL, "continue without reference", breaker=True),
    "21st.dev": ServicePolicy(
        "21st.dev", Label.OPTIONAL, "continue without reference", breaker=True
    ),
    "penpot": ServicePolicy("penpot", Label.OPTIONAL, "continue without reference", breaker=True),
    # core.brain.facade.recall — local disk (BrainStore), not a remote
    # service, but still wired: a corrupt record / momentary lock is
    # transient in the same shape a breaker already models, and this stops a
    # wedged store from paying its own read cost on every single `assign`.
    "brain_read": ServicePolicy(
        "brain_read",
        Label.RETRYABLE,
        "conversation/files continue",
        breaker=True,
        reason="local disk store; high failure_threshold so a spurious open never "
        "silences recall for other panes reading the same project",
    ),
    # Detection only (`.storybook/` dir / package.json script scan) — no
    # network call, no subprocess; a breaker would have nothing to protect
    # against (a repeated local `Path.is_dir()`/`read_text()` costs nothing
    # close to a network timeout).
    "storybook": ServicePolicy(
        "storybook",
        Label.OPTIONAL,
        "skip — no Storybook detected",
        breaker=False,
        reason="local filesystem scan only, already cheap and already fail-open",
    ),
    # graft_autobuild.py's build sweep already has its own single-flight +
    # semaphore + subprocess timeout + kill-orphan-tree handling — a second,
    # independent breaker around the same subprocess would just duplicate
    # that throttling with different knobs. The other half — `graft ask`/
    # `graft mcp` query calls — run inside a SEPARATELY SPAWNED per-pane MCP
    # server process (graft's own node process, launched by the pane, not
    # invoked synchronously by this codebase), so there is no single
    # in-process call site here to wrap at all. Documented gap: no breaker
    # wired for graft in this pass; `graft_store`'s existing build-completion
    # marker + the autobuild sweep's own retry cadence are what stand in for
    # one today.
    "graft": ServicePolicy(
        "graft",
        Label.DEGRADED,
        "files fallback",
        breaker=False,
        reason="build sweep already self-throttles (single-flight+timeout+semaphore); "
        "query calls run inside a separately-spawned MCP process, not a call site "
        "this codebase invokes directly — gap, not yet breaker-wired",
    ),
    # git_changes_service.py — every subprocess call already has its own
    # short timeout and degrades to an empty result/`None` on any OSError or
    # timeout (see `_run_git`); it is a UI-adjacent module (imports Qt) so it
    # cannot import `core.resilience` without crossing the `core-is-bottom-
    # layer` boundary the wrong way. Local + already bounded — a breaker
    # would add cross-module coupling for no behavior change.
    "git": ServicePolicy(
        "git",
        Label.WARNING,
        "Explorer files still usable",
        breaker=False,
        reason="local subprocess, already timeout-bounded and fail-open per-call",
    ),
    # trace_store.save_last_trace — local best-effort JSON write, already
    # wrapped in `except Exception: ...` with no propagation.
    "trace_write": ServicePolicy(
        "trace_write",
        Label.WARNING,
        "never fail assignment",
        breaker=False,
        reason="local best-effort write, already never raises",
    ),
    # preview_controller.py — a crashed live-preview process/browser tab
    # never blocks the owning task; already contained at its own call site.
    "preview": ServicePolicy(
        "preview",
        Label.WARNING,
        "task continues",
        breaker=False,
        reason="local process/browser containment, already fail-open per-call",
    ),
}


def policy_for(service: str) -> ServicePolicy | None:
    return POLICY.get(service)


__all__ = ["POLICY", "Label", "ServicePolicy", "policy_for"]
