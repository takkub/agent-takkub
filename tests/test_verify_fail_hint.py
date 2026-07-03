"""Tests for `_append_verify_fail_hint` — appends the `takkub done --fail`
reporting instruction to verify-role (qa/reviewer) task specs so a failing
check routes back into a Lead-proposed fix loop (Tier 2a part 2).
"""

from __future__ import annotations

import pytest

from agent_takkub.orchestrator_text import _append_verify_fail_hint


@pytest.mark.parametrize("role", ["qa", "reviewer"])
def test_appended_for_verify_roles(role: str) -> None:
    out = _append_verify_fail_hint("smoke /login then report", role)
    assert out.startswith("smoke /login then report")
    assert "done --fail" in out
    assert "verify reporting" in out


@pytest.mark.parametrize("role", ["frontend", "backend", "devops", "mobile", "lead", "critic"])
def test_noop_for_non_verify_roles(role: str) -> None:
    task = "build the /login page"
    # critic is deliberately excluded: it proposes improvements, never pass/fail.
    assert _append_verify_fail_hint(task, role) == task


def test_idempotent_on_replay() -> None:
    once = _append_verify_fail_hint("run e2e", "qa")
    twice = _append_verify_fail_hint(once, "qa")
    # Auto-respawn replays the stored (already-augmented) task — must not stack.
    assert twice == once
    assert twice.count("verify reporting") == 1
