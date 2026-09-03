"""Unit tests for digest_facts (#245) — pure fact model + renderer, no I/O."""

from __future__ import annotations

from agent_takkub.digest_facts import (
    DigestFacts,
    detect_ops_task,
    format_digest_fact_line,
    union_files_touched,
)


class TestFormatDigestFactLine:
    def test_worktree_clean_merge_renders_full_table(self):
        facts = DigestFacts(
            role="backend",
            ref="#245",
            branch="wt/backend-1-123",
            commits_ahead=3,
            uncommitted=0,
            merge_conflicts=False,
            files_touched=5,
            files_dirs=("src", "tests"),
            report_path="runtime/sessions/2026-08-15T120000-backend.md",
            headline="แก้ digest fact table",
        )
        line = format_digest_fact_line(facts, stamp="[12:00:00] ")
        assert "[backend] done [ref #245]" in line
        assert "branch:wt/backend-1-123" in line
        assert "3 commit ahead" in line
        assert "0 ไฟล์ค้าง commit" in line
        assert "merge:clean" in line
        assert "ไฟล์ที่แตะ:5 (src, tests)" in line
        assert "↳ แก้ digest fact table" in line
        assert "report: runtime/sessions/2026-08-15T120000-backend.md" in line
        assert line.startswith("• [12:00:00] ")

    def test_dirty_worktree_shows_warning_marker(self):
        facts = DigestFacts(role="qa", uncommitted=4, merge_conflicts=None, merge_note="")
        line = format_digest_fact_line(facts)
        assert "⚠4 ไฟล์ยังไม่ commit" in line

    def test_merge_conflict_renders_as_conflict(self):
        facts = DigestFacts(role="frontend", merge_conflicts=True)
        assert "merge:CONFLICT" in format_digest_fact_line(facts)

    def test_files_touched_none_shows_unverifiable_not_zero(self):
        # #245's explicit requirement: never a misleading bare 0 for
        # "couldn't check" — must read as unverifiable.
        facts = DigestFacts(role="backend", files_touched=None, files_note="ตรวจไม่ได้ (no snapshot)")
        line = format_digest_fact_line(facts)
        assert "ไฟล์ที่แตะ:ตรวจไม่ได้" in line
        assert "no snapshot" in line
        assert "ไฟล์ที่แตะ:0" not in line

    def test_no_headline_or_report_path_omits_extra_lines(self):
        facts = DigestFacts(role="devops")
        line = format_digest_fact_line(facts)
        assert "\n" not in line

    def test_no_ref_omits_ref_badge(self):
        facts = DigestFacts(role="mobile")
        line = format_digest_fact_line(facts)
        assert "[ref" not in line
        assert "[mobile] done" in line

    def test_pushed_branch_shows_remote_bit(self):
        """#462 — a worktree pane may push its own `wt/*` branch (#438); the
        digest bullet must surface that so Lead knows to delete the remote
        copy after merge/clean instead of finding it as later debris."""
        facts = DigestFacts(role="backend", branch="wt/backend-1-123", pushed=True)
        line = format_digest_fact_line(facts)
        assert "pushed:origin/wt/backend-1-123" in line

    def test_not_pushed_omits_remote_bit(self):
        facts = DigestFacts(role="backend", branch="wt/backend-1-123", pushed=False)
        line = format_digest_fact_line(facts)
        assert "pushed:" not in line

    def test_pushed_without_branch_omits_remote_bit(self):
        """`pushed=True` alone can never render — there is no branch name to
        show `origin/<branch>` for (shared-tree panes never set this)."""
        facts = DigestFacts(role="backend", pushed=True)
        line = format_digest_fact_line(facts)
        assert "pushed:" not in line


class TestOpsTaskZeroFiles:
    """#470: a measured `files_touched:0` on an ops/devops action (deploy,
    migrate, restart, ...) must not read as the #278 "nothing happened"
    alarm — its side effect lives outside the repo. A non-ops role's zero
    must keep the original ⚠️ wording unchanged (#473 depends on that guard
    staying intact)."""

    def test_devops_role_zero_files_is_neutral_not_warning(self):
        # ops_task is set by the caller (Orchestrator.done()) via
        # detect_ops_task on the role + full note — DigestFacts itself never
        # re-derives it (see the module docstring: facts are pre-computed,
        # never re-derived by the formatter).
        facts = DigestFacts(
            role="devops", files_touched=0, headline="Deployed to VPS; migrate DB", ops_task=True
        )
        line = format_digest_fact_line(facts)
        assert "⚠️" not in line
        assert "ยังไม่มีอะไรเปลี่ยน" not in line
        assert "ไฟล์ใน repo ที่แตะ: 0 (งาน ops" in line

    def test_devops_role_zero_files_summarises_side_effects_from_headline(self):
        facts = DigestFacts(
            role="devops", files_touched=0, headline="Deployed to VPS; migrate DB", ops_task=True
        )
        line = format_digest_fact_line(facts)
        assert "deploy" in line
        assert "migrate" in line

    def test_backend_role_zero_files_keeps_warning(self):
        # Unrelated role, ops_task False (the #278 guard must not soften
        # for a plain implementation task).
        facts = DigestFacts(role="backend", files_touched=0, headline="แก้บั๊ก login")
        line = format_digest_fact_line(facts)
        assert "⚠️ ไฟล์ที่แตะ:0 — ยังไม่มีอะไรเปลี่ยน" in line

    def test_devops_role_zero_files_without_ops_task_flag_keeps_warning(self):
        # A caller that never set ops_task=True (or a devops report that
        # genuinely touched 0 files with no measured ops signal) still gets
        # the loud #278 wording — ops_task is what actually gates this, not
        # the role string alone.
        facts = DigestFacts(role="devops", files_touched=0)
        line = format_digest_fact_line(facts)
        assert "⚠️ ไฟล์ที่แตะ:0 — ยังไม่มีอะไรเปลี่ยน" in line

    def test_devops_role_nonzero_files_unaffected(self):
        facts = DigestFacts(role="devops", files_touched=2, files_dirs=("infra",), ops_task=True)
        line = format_digest_fact_line(facts)
        assert "⚠️" not in line
        assert "ไฟล์ที่แตะ:2 (infra)" in line


class TestDetectOpsTask:
    def test_devops_role_is_always_ops(self):
        assert detect_ops_task("devops", "") is True

    def test_sharded_devops_role_is_ops(self):
        assert detect_ops_task("devops#2", "") is True

    def test_other_role_with_ops_keyword_in_note_is_ops(self):
        assert detect_ops_task("backend", "รัน migration บน production DB") is True

    def test_other_role_without_ops_keyword_is_not_ops(self):
        assert detect_ops_task("backend", "แก้ endpoint /auth/login") is False

    def test_empty_role_and_note_is_not_ops(self):
        assert detect_ops_task("", "") is False


class TestUnionFilesTouched:
    def test_combines_committed_and_uncommitted_paths(self):
        diffstat = " src/a.py | 2 +-\n src/b.py | 1 +\n"
        porcelain = " M src/b.py\n?? src/c.py\n"
        count, dirs = union_files_touched(diffstat, porcelain)
        assert count == 3  # a.py, b.py (dedup), c.py
        assert dirs == ["src"]

    def test_empty_inputs_yield_zero(self):
        assert union_files_touched("", "") == (0, [])

    def test_rename_in_porcelain_keeps_new_path(self):
        count, dirs = union_files_touched("", "R  old/name.py -> new/name.py\n")
        assert count == 1
        assert dirs == ["new"]

    def test_top_level_dirs_deduped_and_ordered(self):
        diffstat = " b/x.py | 1 +\n a/y.py | 1 +\n a/z.py | 1 +\n"
        count, dirs = union_files_touched(diffstat, "")
        assert count == 3
        assert dirs == ["a", "b"]  # sorted-path order, deduped
