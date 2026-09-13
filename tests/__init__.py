"""Test utilities and fixtures shared across test modules."""

from __future__ import annotations

# ── Task structure helpers (#585: budget block normalization) ──────────────────


def assert_task_structure(
    task: str,
    expected_role: str | None = None,
    has_codex_notice: bool = False,
    expected_scope: str = "normal",
) -> None:
    """Assert that *task* has the correct structure after #585 budget block injection.

    Structure: [budget_block]\n\n[CODEX_TASK_NOTICE]\n[ROLE: ...]\n[body]

    :param task: The full task text to validate
    :param expected_role: If provided, assert [ROLE: <role>] appears
    :param has_codex_notice: If True, assert CODEX_TASK_NOTICE is present
    :param expected_scope: Expected budget block ("tiny", "normal", "deep")
    """
    from agent_takkub import task_scope
    from agent_takkub.orchestrator import _CODEX_TASK_NOTICE

    if not task:
        raise AssertionError("Task is empty")

    # Check budget block at the start
    expected_budget = task_scope.budget_block(expected_scope)
    if expected_budget:
        # The budget block is prepended with \n\n after it
        assert task.startswith(expected_budget), (
            f"Task should start with budget block for scope '{expected_scope}', "
            f"but starts with: {task[:80]}"
        )

    # Check for CODEX notice
    if has_codex_notice:
        assert _CODEX_TASK_NOTICE in task, (
            "Task should contain CODEX_TASK_NOTICE for codex panes, but it doesn't"
        )

    # Check for ROLE line
    if expected_role:
        role_line_pattern = f"[ROLE: {expected_role}"
        assert role_line_pattern in task, (
            f"Task should contain '[ROLE: {expected_role}]' but doesn't. Task: {task[:200]}"
        )


def extract_task_body(task: str) -> str:
    """Extract the original task body, removing budget blocks and CODEX notices.

    Returns the task starting from the [ROLE: ...] line or the task body,
    useful for comparing tasks before/after budget injection.
    """
    from agent_takkub import task_scope
    from agent_takkub.orchestrator import _CODEX_TASK_NOTICE

    text = task or ""

    # Remove budget blocks from the start
    for block in task_scope.BUDGET_BLOCKS.values():
        if text.startswith(block):
            text = text[len(block) :].lstrip("\r\n")
            break

    # Remove CODEX notice if present
    if _CODEX_TASK_NOTICE in text:
        text = text.replace(_CODEX_TASK_NOTICE, "").lstrip("\r\n")

    return text
