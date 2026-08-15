"""Unit tests for notice_facts (#244) — pure extraction, no I/O."""

from __future__ import annotations

from agent_takkub.notice_facts import extract_issue_ref


class TestExtractIssueRef:
    def test_finds_hash_number(self):
        assert extract_issue_ref("แก้ issue #244: digest ...") == "#244"

    def test_finds_first_of_several(self):
        # #241 mentioned as "related" in the body — the first ref in the
        # assign text should win (the one the task is primarily about).
        assert extract_issue_ref("fix #244 (related to #241)") == "#244"

    def test_no_ref_returns_none(self):
        assert extract_issue_ref("ทำความสะอาด logging ทั่วไป") is None

    def test_empty_or_none_returns_none(self):
        assert extract_issue_ref("") is None
        assert extract_issue_ref(None) is None

    def test_does_not_match_bare_hash_without_digits(self):
        assert extract_issue_ref("channel #general") is None

    def test_rejects_runs_longer_than_six_digits(self):
        # A 7+ digit run is not a plausible issue number — the \b boundary
        # after a max-6-digit capture can never land inside a longer run of
        # digits, so it simply doesn't match (never truncates to a
        # nonsense 6-digit prefix).
        assert extract_issue_ref("code #1234567") is None
