"""Unit tests for digest_facts (#245) — pure fact model + renderer, no I/O."""

from __future__ import annotations

from agent_takkub.digest_facts import DigestFacts, format_digest_fact_line, union_files_touched


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
