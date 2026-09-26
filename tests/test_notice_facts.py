"""Unit tests for notice_facts (#244) — pure extraction, no I/O."""

from __future__ import annotations

from agent_takkub.notice_facts import extract_issue_ref, lead_owned_text
from agent_takkub.orchestrator_text import _rewrite_task_for_codex


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


class TestOrchestratorPreambleIsNotTheTasksRef:
    """#733: the stored assign text is `<orchestrator preamble><Lead's spec>`.

    A codex pane's digest shipped `[ref #641]` for a task that was actually
    about #731, because the codex `task_notice_preamble` (which cites #641) is
    prepended BEFORE the text `done()` reads.
    """

    def test_leads_own_ref_wins_over_the_preambles(self):
        lead = "แก้บั๊กที่ทำให้ pane ถูกฆ่ากลางงาน (#731) — ตรวจ #729 ด้วย"
        assert extract_issue_ref(_rewrite_task_for_codex(lead)) == "#731"

    def test_preamble_only_ref_is_not_reported_as_the_tasks_ref(self):
        # Lead's spec carries no issue number at all: the orchestrator's own
        # #641 is a rule reference, not this task's issue — show nothing.
        assert extract_issue_ref(_rewrite_task_for_codex("ทดสอบของจริง อย่าลืมสรุปผล")) is None

    def test_lead_text_is_recovered_verbatim(self):
        lead = "บรรทัดแรก\n\nบรรทัดที่สอง #725"
        assert lead_owned_text(_rewrite_task_for_codex(lead)).strip() == lead

    def test_payload_without_a_preamble_is_untouched(self):
        # Non-codex providers get no notice; their text must pass through
        # unchanged (and a task with no ref still reports no ref).
        assert lead_owned_text("ทดสอบ #999") == "ทดสอบ #999"
        assert extract_issue_ref("ทดสอบ #999") == "#999"
