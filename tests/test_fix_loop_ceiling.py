"""Tests for fix loop ceiling (#585 round 3).

Ensures that:
1. Fix loop automatically retries for tiny/normal scope up to MAX_FIX_LOOP_ATTEMPTS (2 rounds).
2. On the 3rd attempt, it hits the ceiling, stops auto-retry, and prompts the user with 1 short question.
3. Deep scope requires user proposal/confirmation (does not auto-retry silently).
4. FixLoopTracker properly records and resets attempt counts per failure signature.
"""

from __future__ import annotations

from agent_takkub.routing_planner import (
    MAX_FIX_LOOP_ATTEMPTS as RP_MAX_ATTEMPTS,
)
from agent_takkub.routing_planner import (
    FixLoopTracker as RPFixLoopTracker,
)
from agent_takkub.routing_planner import (
    check_fix_loop_ceiling as rp_check_ceiling,
)
from agent_takkub.task_scope import (
    MAX_FIX_LOOP_ATTEMPTS,
    FixLoopTracker,
    check_fix_loop_ceiling,
)


def test_max_fix_loop_attempts_is_two():
    assert MAX_FIX_LOOP_ATTEMPTS == 2
    assert RP_MAX_ATTEMPTS == 2


def test_tiny_and_normal_auto_retry_under_ceiling():
    for scope in ("tiny", "normal"):
        # Round 1
        dec1 = check_fix_loop_ceiling(scope, attempt=1, failure_signature="test_fail")
        assert dec1.allowed is True
        assert dec1.action == "auto_retry"
        assert dec1.attempt == 1
        assert dec1.max_attempts == 2
        assert dec1.user_question is None

        # Round 2
        dec2 = check_fix_loop_ceiling(scope, attempt=2, failure_signature="test_fail")
        assert dec2.allowed is True
        assert dec2.action == "auto_retry"
        assert dec2.attempt == 2
        assert dec2.max_attempts == 2
        assert dec2.user_question is None


def test_ceiling_reached_stops_and_asks_user():
    for scope in ("tiny", "normal"):
        # Round 3 (ceiling exceeded)
        dec3 = check_fix_loop_ceiling(scope, attempt=3, failure_signature="TypeError")
        assert dec3.allowed is False
        assert dec3.action == "ask_user"
        assert dec3.attempt == 3
        assert dec3.max_attempts == 2
        assert dec3.user_question is not None
        assert "2 รอบ" in dec3.user_question
        assert "ต้องการให้แก้ต่อด้วยแนวทางไหน" in dec3.user_question

        # Round 4 (still stops)
        dec4 = check_fix_loop_ceiling(scope, attempt=4, failure_signature="TypeError")
        assert dec4.allowed is False
        assert dec4.action == "ask_user"


def test_deep_scope_requires_proposal():
    for attempt in (1, 2, 3):
        dec = check_fix_loop_ceiling("deep", attempt=attempt, failure_signature="migration_fail")
        assert dec.allowed is False
        assert dec.action == "propose"
        assert "deep" in dec.reason


def test_fix_loop_tracker_lifecycle():
    tracker = FixLoopTracker(max_attempts=2)

    # Initial state
    assert tracker.get_attempts("issue_A") == 0
    assert tracker.can_auto_retry("issue_A", scope="normal") is True

    # 1st failure on issue_A
    dec1 = tracker.record_failure("issue_A", scope="normal")
    assert dec1.allowed is True
    assert dec1.attempt == 1
    assert tracker.get_attempts("issue_A") == 1
    assert tracker.can_auto_retry("issue_A", scope="normal") is True

    # 2nd failure on issue_A
    dec2 = tracker.record_failure("issue_A", scope="normal")
    assert dec2.allowed is True
    assert dec2.attempt == 2
    assert tracker.get_attempts("issue_A") == 2
    # Next failure (attempt 3) would exceed ceiling
    assert tracker.can_auto_retry("issue_A", scope="normal") is False

    # 3rd failure on issue_A -> ceiling reached
    dec3 = tracker.record_failure("issue_A", scope="normal")
    assert dec3.allowed is False
    assert dec3.action == "ask_user"
    assert dec3.attempt == 3

    # Distinct issue_B is tracked independently
    assert tracker.get_attempts("issue_B") == 0
    assert tracker.can_auto_retry("issue_B", scope="normal") is True
    dec_b1 = tracker.record_failure("issue_B", scope="normal")
    assert dec_b1.allowed is True
    assert dec_b1.attempt == 1

    # Reset issue_A
    tracker.reset("issue_A")
    assert tracker.get_attempts("issue_A") == 0
    assert tracker.can_auto_retry("issue_A", scope="normal") is True
    # issue_B still recorded
    assert tracker.get_attempts("issue_B") == 1

    # Full reset
    tracker.reset()
    assert tracker.get_attempts("issue_B") == 0


def test_routing_planner_reexports_match():
    # Calling re-exported check_fix_loop_ceiling
    dec1 = rp_check_ceiling("normal", 1)
    assert dec1.allowed is True
    assert dec1.action == "auto_retry"

    dec3 = rp_check_ceiling("normal", 3)
    assert dec3.allowed is False
    assert dec3.action == "ask_user"

    rp_tracker = RPFixLoopTracker()
    assert rp_tracker.max_attempts == 2
