"""Role registry. Default roles + colors + grid positions.

The cockpit reserves 8 slots in a 3-column grid:

  col 0 (left):   Lead (always-on)
  col 1 (middle): frontend / backend / mobile / devops / codex
  col 2 (right):  gemini / qa / reviewer + dynamic add-slot

Custom roles can be added at runtime via Orchestrator.register_role.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Role:
    name: str
    label: str
    color: str  # hex
    column: int  # 0=lead, 1=middle, 2=right
    row: int


LEAD = Role("lead", "Lead", "#f5c542", column=0, row=0)

DEFAULT_TEAMMATES: tuple[Role, ...] = (
    Role("frontend", "Frontend", "#22d3ee", column=1, row=0),
    Role("backend", "Backend", "#3b82f6", column=1, row=1),
    Role("mobile", "Mobile", "#a855f7", column=1, row=2),
    Role("devops", "DevOps", "#22c55e", column=1, row=3),
    # Gemini is a non-claude pane: orchestrator launches the `gemini`
    # binary directly (interactive TUI) and skips all claude flags +
    # ECC mutes. Sits at col=2 row=0 (the slot designer used to occupy)
    # because Gemini's role is "third brain" planning / second opinion,
    # which lives alongside qa/reviewer in the support column.
    # Designer was removed from defaults; .claude/agents/designer.md
    # is preserved so custom-slot add still works for users who want it.
    # Colour is Google's signature blue so it visually stands apart
    # from claude-backed (cyan) and codex (teal) roles.
    Role("gemini", "Gemini", "#4285f4", column=2, row=0),
    Role("qa", "QA", "#f97316", column=2, row=1),
    Role("reviewer", "Reviewer", "#ef4444", column=2, row=2),
    # Codex is a non-claude pane: orchestrator launches the `codex`
    # binary directly (interactive TUI) and skips all claude flags +
    # ECC mutes. Sits in column 1 (dev specialists) below devops
    # because Codex's strength is code work, not support/review.
    # Colour is OpenAI's signature teal so it visually stands apart
    # from the claude-backed roles.
    Role("codex", "Codex", "#10a37f", column=1, row=4),
    # Design Critic: post-QA visual reviewer. Picks up screenshots from
    # runtime/exports/<date>/<project>/screenshots/ (where QA mb-shots
    # them), pushes each to a gemini pane via `takkub send`, consolidates
    # the visual feedback into a proposal markdown, then reports back.
    # Sits below reviewer in the support/review column — design critique
    # is to UI what code review is to code. Pink keeps it distinct from
    # reviewer (red) and gemini (google-blue) right above it.
    Role("critic", "Design Critic", "#ec4899", column=2, row=3),
    # Plain PowerShell pane — no claude, no codex, no gemini. Spawned via
    # the "Open Shell" status-bar button when the user wants a quick
    # ad-hoc shell inside the cockpit grid (run a one-off command, tail a
    # log, poke at git) without losing context to a separate terminal
    # window. Neutral slate so it doesn't compete with agent panes.
    Role("shell", "Shell", "#94a3b8", column=2, row=4),
)

ALL_DEFAULT: tuple[Role, ...] = (LEAD, *DEFAULT_TEAMMATES)

# Panes the USER types into directly, so they are never auto-locked by the
# accidental-input guard: the Lead (the command surface) and an ad-hoc Shell
# (opened explicitly to run commands). Every other role is orchestrator-driven
# and defaults to input-locked. (A locked pane can still be unlocked per-pane
# via its 🔒 button — these two just don't start locked and have no button.)
USER_DRIVEN_ROLES: frozenset[str] = frozenset({"lead", "shell"})


def by_name(name: str) -> Role | None:
    name = name.lower().strip()
    for r in ALL_DEFAULT:
        if r.name == name:
            return r
    return None
