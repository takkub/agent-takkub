"""Tests for `_rewrite_task_for_codex` — the override-notice prepender
that gets called on every task heading into a codex pane. Guards against
the bug where codex misinterprets Lead's standard
`[ROLE: ... ห้าม spawn subagent]` prefix as also forbidding the
`takkub done` shell command.
"""

from __future__ import annotations

from agent_takkub.orchestrator import _CODEX_TASK_NOTICE, _rewrite_task_for_codex


def test_prepends_notice_to_plain_task() -> None:
    out = _rewrite_task_for_codex("review docs/foo.md and report")
    assert out.startswith(_CODEX_TASK_NOTICE)
    assert out.endswith("review docs/foo.md and report")


def test_prepends_notice_to_role_prefixed_task() -> None:
    task = "[ROLE: codex reviewer — ทำงานเองโดยตรง ห้าม spawn subagent]\nCross-check refactor X."
    out = _rewrite_task_for_codex(task)
    # Notice appears BEFORE the inline ROLE prefix so the override is at
    # equal or greater proximity than the constraint.
    assert out.index(_CODEX_TASK_NOTICE) < out.index("[ROLE:")


def test_notice_mentions_shell_command_requirement() -> None:
    # Smoke check on the notice text itself so future edits don't drop
    # the load-bearing phrase.
    assert "shell command" in _CODEX_TASK_NOTICE
    assert "takkub done" in _CODEX_TASK_NOTICE
    assert "Bash tool" in _CODEX_TASK_NOTICE


def test_idempotent_when_notice_already_present() -> None:
    once = _rewrite_task_for_codex("do thing")
    twice = _rewrite_task_for_codex(once)
    # Replay path (auto-respawn) reuses the stored task; we must not
    # stack duplicate notices on each replay.
    assert twice == once
    assert twice.count(_CODEX_TASK_NOTICE) == 1
