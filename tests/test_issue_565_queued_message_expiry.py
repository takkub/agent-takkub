"""Regression test for #565: stale queued messages replay into unrelated new assignments.

Issue: queued `send` messages are keyed by (project, role) only, without a task/
assignment scope. When a pane closes and a new assignment opens for the same
role, old queued messages from the previous assignment would be delivered,
causing confusing information to be pasted into an unrelated task.

Fix: Messages older than _QUEUED_NO_PANE_MAX_AGE_HOURS (default 12 hours) are
dropped instead of delivered, preventing stale messages from old tasks replaying
into unrelated new assignments.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from agent_takkub import role_messages


def test_queued_no_pane_for_role_separates_pending_and_expired() -> None:
    """Verify that queued_no_pane_for_role separates pending from expired messages."""
    runtime_dir = MagicMock()
    now = time.time()

    # Create mock messages: one fresh, one old
    fresh_msg = {
        "id": "fresh123",
        "ts": now - 3600,  # 1 hour ago
        "to": "backend",
        "state": "queued_no_pane",
        "body": "Fresh message",
    }
    old_msg = {
        "id": "old456",
        "ts": now - 48 * 3600,  # 48 hours ago (beyond the 12-hour default)
        "to": "backend",
        "state": "queued_no_pane",
        "body": "Old message from previous task",
    }
    other_state_msg = {
        "id": "sent789",
        "ts": now - 1000 * 3600,  # Very old
        "to": "backend",
        "state": "sent",
        "body": "Sent message (different state)",
    }

    with patch("agent_takkub.role_messages.read") as mock_read:
        mock_read.return_value = [fresh_msg, old_msg, other_state_msg]

        # Call with default 12-hour expiry
        pending, expired = role_messages.queued_no_pane_for_role(runtime_dir, "project", "backend")

    # Fresh message should be in pending
    assert len(pending) == 1, f"Expected 1 pending message, got {len(pending)}"
    assert pending[0]["id"] == "fresh123"

    # Old message should be in expired
    assert len(expired) == 1, f"Expected 1 expired message, got {len(expired)}"
    assert expired[0]["id"] == "old456"

    # Sent message should be ignored (different state)
    all_backend_msgs = [m for m in [fresh_msg, old_msg, other_state_msg] if m["to"] == "backend"]
    assert len(all_backend_msgs) == 3, "Setup error: should have 3 backend messages"


def test_queued_no_pane_for_role_with_custom_expiry() -> None:
    """Verify that the expiry timeout is configurable via environment variable."""
    runtime_dir = MagicMock()
    now = time.time()

    # Message 25 hours old
    old_msg = {
        "id": "old",
        "ts": now - 25 * 3600,
        "to": "backend",
        "state": "queued_no_pane",
        "body": "Old message",
    }

    with patch("agent_takkub.role_messages.read") as mock_read:
        mock_read.return_value = [old_msg]

        # With default 12-hour expiry, this should expire
        with patch.dict("os.environ", {}, clear=False):
            pending, expired = role_messages.queued_no_pane_for_role(
                runtime_dir, "project", "backend"
            )
        assert len(expired) == 1, "Message 25 hours old should expire with 12h default"

        # With 30-hour expiry, this should be pending
        with patch.object(role_messages, "_QUEUED_NO_PANE_MAX_AGE_HOURS", 30):
            pending, expired = role_messages.queued_no_pane_for_role(
                runtime_dir, "project", "backend"
            )
        assert len(pending) == 1, "Message 25 hours old should NOT expire with 30h timeout"
        assert len(expired) == 0


def test_queued_no_pane_for_role_empty_result() -> None:
    """Verify that empty results return empty lists correctly."""
    runtime_dir = MagicMock()

    with patch("agent_takkub.role_messages.read") as mock_read:
        mock_read.return_value = []

        pending, expired = role_messages.queued_no_pane_for_role(runtime_dir, "project", "backend")

    assert pending == []
    assert expired == []
