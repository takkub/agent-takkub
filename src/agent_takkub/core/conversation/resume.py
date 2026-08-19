"""Native resume fallback (plan §6.7 / blueprint "7. Native Resume Fallback"):

    Try Native Resume
       Success -> Continue
       Fail -> Create New Provider Session
               -> Restore Takkub Checkpoint
               -> Inject Summary + Context
               -> Continue

`build_resume_context`/`render_resume_prompt` cover the "Restore ... Inject"
half — turning a checkpoint into a payload a fresh provider session could be
seeded with. The "Try Native Resume" / "Create New Provider Session" half is
NOT wired to a real spawn yet (task scope: "เตรียม API + test", not a live
call site) — that belongs with whichever phase actually drives `spawn()`
when a provider's own `--resume`/`--session-id` fails; see
`docs/v2/phase6-report.md` for the gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .checkpoint import CheckpointManager


@dataclass(frozen=True, slots=True)
class ResumeContext:
    conversation_id: str
    checkpoint_id: str | None
    objective: str
    current_state: str
    next_action: str
    decisions: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    important_files: list[str] = field(default_factory=list)
    recent_messages: list[dict] = field(default_factory=list)


def build_resume_context(
    project_id: str | None,
    conversation_id: str,
    *,
    checkpoint_id: str | None = None,
    manager: CheckpointManager | None = None,
) -> ResumeContext | None:
    """None when no checkpoint exists yet — caller falls back to whatever it
    would have done pre-#309 (a plain fresh session, no injected context)."""
    mgr = manager or CheckpointManager()
    snapshot = mgr.load(project_id, conversation_id, checkpoint_id)
    if snapshot is None:
        return None
    summary = snapshot.get("summary") or {}
    working_context = snapshot.get("working-context") or {}
    return ResumeContext(
        conversation_id=conversation_id,
        checkpoint_id=snapshot.get("id"),
        objective=summary.get("objective", ""),
        current_state=summary.get("current_state", ""),
        next_action=summary.get("next_action", ""),
        decisions=list(summary.get("decisions", [])),
        completed=list(summary.get("completed", [])),
        pending=list(summary.get("pending", [])),
        warnings=list(summary.get("warnings", [])),
        important_files=list(summary.get("important_files", [])),
        recent_messages=list(working_context.get("messages", [])),
    )


def render_resume_prompt(context: ResumeContext) -> str:
    """Markdown a fresh provider session's first message could carry — never
    injected automatically today (see module docstring's gap note)."""
    lines = ["# Resuming from checkpoint", ""]
    if context.checkpoint_id:
        lines.append(f"checkpoint: `{context.checkpoint_id}`")
    if context.objective:
        lines.append(f"**Objective:** {context.objective}")
    if context.current_state:
        lines.append(f"**Current state:** {context.current_state}")
    if context.next_action:
        lines.append(f"**Next action:** {context.next_action}")
    for label, items in (
        ("Decisions", context.decisions),
        ("Completed", context.completed),
        ("Pending", context.pending),
        ("Warnings", context.warnings),
        ("Important files", context.important_files),
    ):
        if items:
            lines.append(f"\n**{label}:**")
            lines.extend(f"- {item}" for item in items)
    if context.recent_messages:
        lines.append("\n**Recent messages:**")
        for m in context.recent_messages[-10:]:
            lines.append(f"- ({m.get('role', '?')}) {m.get('text', '')[:200]}")
    return "\n".join(lines) + "\n"
