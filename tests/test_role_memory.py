"""Tests for per-(role × project) learned memory (role_memory.py).

Each teammate role gets its own ``runtime/role-memory/<project>/<role>.md`` that
it reads on spawn and appends learnings to. Seeding must be idempotent (never
clobber accumulated learnings), per-role templated, and per-project isolated.
"""

from __future__ import annotations

import pathlib

import pytest

from agent_takkub import role_memory as rm
from agent_takkub.role_memory import ensure_role_memory, role_memory_path


@pytest.fixture
def isolated_role_memory(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Redirect ROLE_MEMORY_DIR to a tmp path so tests don't write under runtime/."""
    target = tmp_path / "role-memory"
    monkeypatch.setattr(rm, "ROLE_MEMORY_DIR", target)
    return target


class TestEnsureRoleMemory:
    def test_seeds_file_when_missing(self, isolated_role_memory: pathlib.Path) -> None:
        path = ensure_role_memory("proj_a", "frontend")
        assert path is not None
        assert path == isolated_role_memory / "proj_a" / "frontend.md"
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "frontend" in text  # header names the role
        assert "proj_a" in text  # …and the project
        assert "## Conventions" in text  # base sections present

    def test_qa_template_has_login_and_flows(self, isolated_role_memory: pathlib.Path) -> None:
        text = ensure_role_memory("proj_a", "qa").read_text(encoding="utf-8")
        assert "Test login" in text
        assert "Known flows" in text

    def test_role_specific_sections(self, isolated_role_memory: pathlib.Path) -> None:
        assert "Components" in ensure_role_memory("p", "frontend").read_text(encoding="utf-8")
        assert "Endpoints" in ensure_role_memory("p", "backend").read_text(encoding="utf-8")

    def test_unknown_role_gets_base_template_only(self, isolated_role_memory: pathlib.Path) -> None:
        text = ensure_role_memory("proj_a", "data-eng").read_text(encoding="utf-8")
        assert "## Conventions" in text
        assert "Gotchas" in text
        assert "Test login" not in text  # no qa extras
        assert "Endpoints" not in text  # no backend extras

    def test_idempotent_preserves_appended_learnings(
        self, isolated_role_memory: pathlib.Path
    ) -> None:
        path = ensure_role_memory("proj_a", "qa")
        learned = path.read_text(encoding="utf-8") + "\n- learned: login is admin / test123\n"
        path.write_text(learned, encoding="utf-8")
        # Re-ensure (next spawn) must NOT clobber the role's accumulated learning.
        again = ensure_role_memory("proj_a", "qa")
        assert again == path
        assert "admin / test123" in path.read_text(encoding="utf-8")

    def test_per_project_and_per_role_isolation(self, isolated_role_memory: pathlib.Path) -> None:
        qa_a = ensure_role_memory("proj_a", "qa")
        qa_b = ensure_role_memory("proj_b", "qa")
        fe_a = ensure_role_memory("proj_a", "frontend")
        assert qa_a != qa_b != fe_a
        assert qa_a.parent.name == "proj_a"
        assert qa_b.parent.name == "proj_b"
        assert fe_a.name == "frontend.md"

    def test_sanitizes_unsafe_names(self, isolated_role_memory: pathlib.Path) -> None:
        path = role_memory_path("my proj/weird:name", "qa")
        assert path.parent.name == "my_proj_weird_name"  # no / or : in the segment
        assert path.name == "qa.md"

    def test_no_parent_dir_traversal(self, isolated_role_memory: pathlib.Path) -> None:
        # Dots are dropped so a `..` project/role can't escape ROLE_MEMORY_DIR.
        for evil in ("..", "../../etc", "a..b"):
            p = role_memory_path(evil, "qa")
            assert ".." not in p.parts
            assert isolated_role_memory in p.parents
        assert role_memory_path("p", "..").name == "__.md"


class TestCuration:
    """ensure_role_memory curates the accumulated file on read (#43): dedup
    repeated bullets + size-cap, preserving the header + seeded skeleton."""

    def test_dedup_collapses_repeated_bullet(self, isolated_role_memory: pathlib.Path) -> None:
        path = ensure_role_memory("p", "qa")
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n- pitfall: ZZZWIDGET needs a retry\n- Pitfall:  zzzwidget needs a retry  \n",
            encoding="utf-8",
        )
        ensure_role_memory("p", "qa")  # curate on next spawn
        lines = [
            ln for ln in path.read_text(encoding="utf-8").splitlines() if "zzzwidget" in ln.lower()
        ]
        assert len(lines) == 1, lines
        # newest (last) occurrence is the one kept, verbatim.
        assert lines[0] == "- Pitfall:  zzzwidget needs a retry  "

    def test_dedup_keeps_distinct_bullets(self, isolated_role_memory: pathlib.Path) -> None:
        path = ensure_role_memory("p", "qa")
        path.write_text(
            path.read_text(encoding="utf-8") + "\n- alpha note one\n- beta note two\n",
            encoding="utf-8",
        )
        ensure_role_memory("p", "qa")
        text = path.read_text(encoding="utf-8")
        assert "alpha note one" in text and "beta note two" in text

    def test_size_cap_trims_oldest_keeps_newest(
        self, isolated_role_memory: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rm, "_MEM_MAX_ENTRIES", 5)
        path = ensure_role_memory("p", "backend")
        notes = "".join(f"\n- note number {i:03d}\n" for i in range(20))
        path.write_text(path.read_text(encoding="utf-8") + notes, encoding="utf-8")
        ensure_role_memory("p", "backend")
        text = path.read_text(encoding="utf-8")
        n_bullets = sum(1 for ln in text.splitlines() if rm._BULLET_RE.match(ln))
        assert n_bullets <= 5
        assert "number 019" in text  # newest survives
        assert "number 000" not in text  # oldest trimmed

    def test_preserves_header_and_seeded_headings(self, isolated_role_memory: pathlib.Path) -> None:
        path = ensure_role_memory("p", "qa")
        # A duplicate forces a curation rewrite.
        path.write_text(path.read_text(encoding="utf-8") + "\n- dupe\n- dupe\n", encoding="utf-8")
        ensure_role_memory("p", "qa")
        text = path.read_text(encoding="utf-8")
        assert "# qa — learned notes" in text  # header survives
        assert "## Conventions / patterns" in text  # seeded skeleton survives
        assert "## Gotchas / pitfalls" in text

    def test_sub_headings_stay_in_body(self) -> None:
        _header, sections = rm._split_doc("# h\n\n## A\n- one\n### sub\n- two\n## B\n- three\n")
        assert len(sections) == 2  # only ## splits; ### is body
        assert any("### sub" in ln for ln in sections[0][1])

    def test_curation_is_idempotent(self, isolated_role_memory: pathlib.Path) -> None:
        text = ensure_role_memory("p", "qa").read_text(encoding="utf-8")
        new, changed, archived = rm._curate_text(text)
        assert changed is False and new == text
        assert archived == []

    def test_literal_braces_preserved(self, isolated_role_memory: pathlib.Path) -> None:
        path = ensure_role_memory("p", "devops")
        brace = "- health: {{.State.Health.Status}} must be healthy"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n" + brace + "\n- dupe\n- dupe\n",
            encoding="utf-8",
        )
        ensure_role_memory("p", "devops")  # must not raise on literal braces
        assert brace in path.read_text(encoding="utf-8")

    def test_best_effort_on_unreadable(
        self, isolated_role_memory: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = ensure_role_memory("p", "qa")

        def boom(*_a, **_k):
            raise OSError("read failed")

        monkeypatch.setattr(pathlib.Path, "read_text", boom)
        # A read failure during curation must be swallowed — ensure still returns.
        assert ensure_role_memory("p", "qa") == path

    def test_long_entry_truncated_to_first_sentence(
        self, isolated_role_memory: pathlib.Path
    ) -> None:
        # A done-report-style paragraph pasted as one bullet must be cut down
        # at curation time, not left to bloat the spawn prompt forever.
        path = ensure_role_memory("p", "backend")
        essay = "- fix (2026-08-04): " + ("root cause analysis word " * 60) + ". more detail after."
        assert len(essay) > rm._MEM_MAX_ENTRY_CHARS
        path.write_text(path.read_text(encoding="utf-8") + "\n" + essay + "\n", encoding="utf-8")
        ensure_role_memory("p", "backend")
        text = path.read_text(encoding="utf-8")
        kept = [ln for ln in text.splitlines() if ln.startswith("- fix (2026-08-04):")]
        assert len(kept) == 1, kept
        assert len(kept[0]) <= rm._MEM_MAX_ENTRY_CHARS
        assert kept[0].endswith(" …")
        assert "more detail after" not in text

    def test_long_entry_without_sentence_end_cuts_at_word_boundary(
        self, isolated_role_memory: pathlib.Path
    ) -> None:
        path = ensure_role_memory("p", "qa")
        no_punct = "- " + ("wordwordword " * 80).strip()  # no . ! ? anywhere
        assert len(no_punct) > rm._MEM_MAX_ENTRY_CHARS
        path.write_text(path.read_text(encoding="utf-8") + "\n" + no_punct + "\n", encoding="utf-8")
        ensure_role_memory("p", "qa")
        text = path.read_text(encoding="utf-8")
        kept = [ln for ln in text.splitlines() if ln.startswith("- wordwordword")]
        assert len(kept) == 1, kept
        assert kept[0].endswith(" …")
        # cut lands on a word boundary — content before " …" ends with a
        # whole "wordwordword" token, never a partial word like "wordwordwor"
        assert kept[0][:-2].endswith("wordwordword")
        assert len(kept[0]) <= rm._MEM_MAX_ENTRY_CHARS

    def test_short_entry_not_truncated(self, isolated_role_memory: pathlib.Path) -> None:
        path = ensure_role_memory("p", "frontend")
        short = "- ports are 3001 / 3002 for this project"
        path.write_text(path.read_text(encoding="utf-8") + "\n" + short + "\n", encoding="utf-8")
        ensure_role_memory("p", "frontend")
        assert short in path.read_text(encoding="utf-8")

    def test_multiline_entry_collapsed_when_over_budget(
        self, isolated_role_memory: pathlib.Path
    ) -> None:
        # A bullet with continuation lines (the multi-line-entry convention
        # `_block_split` groups) must also be collapsed to one truncated line.
        path = ensure_role_memory("p", "backend")
        block = "- header line here.\n  " + ("continuation text words " * 40)
        assert len(block) > rm._MEM_MAX_ENTRY_CHARS
        path.write_text(path.read_text(encoding="utf-8") + "\n" + block + "\n", encoding="utf-8")
        ensure_role_memory("p", "backend")
        text = path.read_text(encoding="utf-8")
        assert "continuation text words continuation" not in text
        kept = [ln for ln in text.splitlines() if ln.startswith("- header line here")]
        assert len(kept) == 1, kept

    def test_byte_cap_is_6000(self) -> None:
        # Regression guard for the token-reduction task (2026-08): the cap was
        # deliberately lowered from 16000 to 6000 bytes/spawn.
        assert rm._MEM_MAX_BYTES == 6_000

    def test_header_states_entry_length_rule(self, isolated_role_memory: pathlib.Path) -> None:
        text = ensure_role_memory("p", "qa").read_text(encoding="utf-8")
        assert "2-3 บรรทัด" in text
        assert "ห้าม paste done report" in text


class TestArchive:
    """L2 archive (#151): entries curation truncates/trims out of the L1 file
    are appended in FULL to a sibling ``<role>-archive.md`` before being cut —
    and that sibling file is never the one a spawn prompt inlines."""

    def test_truncated_entry_full_text_archived(self, isolated_role_memory: pathlib.Path) -> None:
        path = ensure_role_memory("p", "backend")
        essay = "- fix (2026-08-04): " + ("root cause analysis word " * 60) + ". more detail after."
        path.write_text(path.read_text(encoding="utf-8") + "\n" + essay + "\n", encoding="utf-8")
        ensure_role_memory("p", "backend")  # curate → truncate L1 + archive full text
        archive = rm.role_memory_archive_path("p", "backend")
        assert archive.exists()
        archived_text = archive.read_text(encoding="utf-8")
        assert "more detail after" in archived_text  # the part L1 cut away

    def test_trimmed_entry_full_text_archived(
        self, isolated_role_memory: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rm, "_MEM_MAX_ENTRIES", 5)
        path = ensure_role_memory("p", "backend")
        notes = "".join(f"\n- note number {i:03d}\n" for i in range(20))
        path.write_text(path.read_text(encoding="utf-8") + notes, encoding="utf-8")
        ensure_role_memory("p", "backend")
        l1_text = path.read_text(encoding="utf-8")
        assert "number 000" not in l1_text  # trimmed out of L1 as before (#43)
        archive_text = rm.role_memory_archive_path("p", "backend").read_text(encoding="utf-8")
        assert "number 000" in archive_text  # ...but the full entry survives in L2

    def test_archive_not_injected_into_spawn_content(
        self, isolated_role_memory: pathlib.Path
    ) -> None:
        # spawn_engine inlines exactly `ensure_role_memory(...)`'s return path's
        # content into the prompt — prove that content never contains what got
        # archived, and that the archive lives at a different path entirely.
        path = ensure_role_memory("p", "backend")
        essay = "- fix: " + ("root cause analysis word " * 60) + ". tail text that must not leak."
        assert len(essay) > rm._MEM_MAX_ENTRY_CHARS
        path.write_text(path.read_text(encoding="utf-8") + "\n" + essay + "\n", encoding="utf-8")
        mem_path = ensure_role_memory("p", "backend")
        injected = mem_path.read_text(encoding="utf-8")
        assert "tail text that must not leak" not in injected
        archive_path = rm.role_memory_archive_path("p", "backend")
        assert archive_path != mem_path
        assert "tail text that must not leak" in archive_path.read_text(encoding="utf-8")

    def test_dedup_drop_not_archived(self, isolated_role_memory: pathlib.Path) -> None:
        # A duplicate bullet dedup drops is a verbatim repeat of the kept one —
        # nothing lost, so it must NOT create an archive file at all.
        path = ensure_role_memory("p", "qa")
        path.write_text(
            path.read_text(encoding="utf-8") + "\n- dupe entry text\n- dupe entry text\n",
            encoding="utf-8",
        )
        ensure_role_memory("p", "qa")
        assert not rm.role_memory_archive_path("p", "qa").exists()

    def test_archive_timestamp_is_single_line_iso_date(
        self, isolated_role_memory: pathlib.Path
    ) -> None:
        path = ensure_role_memory("p", "backend")
        essay = "- fix: " + ("root cause analysis word " * 60) + ". overflow tail."
        assert len(essay) > rm._MEM_MAX_ENTRY_CHARS
        path.write_text(path.read_text(encoding="utf-8") + "\n" + essay + "\n", encoding="utf-8")
        ensure_role_memory("p", "backend")
        archive_text = rm.role_memory_archive_path("p", "backend").read_text(encoding="utf-8")
        stamps = [ln for ln in archive_text.splitlines() if ln.startswith("### ")]
        assert stamps, archive_text
        for s in stamps:
            date_part = s.removeprefix("### ").strip()
            assert len(date_part) == 10 and date_part.count("-") == 2  # "YYYY-MM-DD"

    def test_archive_caps_and_rotates_oldest(
        self, isolated_role_memory: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive_path = rm.role_memory_archive_path("p", "backend")
        rm._archive_entries("p", "backend", ["- oldest entry marker AAA " + "x" * 200])
        size_after_one = len(archive_path.read_text(encoding="utf-8").encode("utf-8"))
        monkeypatch.setattr(rm, "_ARCHIVE_MAX_BYTES", size_after_one + 50)
        rm._archive_entries("p", "backend", ["- newest entry marker BBB " + "y" * 200])
        text = archive_path.read_text(encoding="utf-8")
        assert len(text.encode("utf-8")) <= size_after_one + 50
        assert "newest entry marker BBB" in text
        assert "oldest entry marker AAA" not in text

    def test_l1_header_points_to_archive_and_search(
        self, isolated_role_memory: pathlib.Path
    ) -> None:
        text = ensure_role_memory("p", "backend").read_text(encoding="utf-8")
        assert "backend-archive.md" in text
        assert "takkub search" in text

    def test_archive_header_names_project_and_role(
        self, isolated_role_memory: pathlib.Path
    ) -> None:
        rm._archive_entries("proj_a", "qa", ["- some archived thing"])
        text = rm.role_memory_archive_path("proj_a", "qa").read_text(encoding="utf-8")
        assert "qa" in text and "proj_a" in text

    def test_archive_path_is_sibling_file(self, isolated_role_memory: pathlib.Path) -> None:
        mem = rm.role_memory_path("proj_a", "backend")
        archive = rm.role_memory_archive_path("proj_a", "backend")
        assert archive.parent == mem.parent
        assert archive.name == "backend-archive.md"
        assert archive != mem

    def test_archive_entries_noop_on_empty_list(self, isolated_role_memory: pathlib.Path) -> None:
        rm._archive_entries("p", "backend", [])
        assert not rm.role_memory_archive_path("p", "backend").exists()

    def test_archive_best_effort_on_write_failure(
        self, isolated_role_memory: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a, **_k):
            raise OSError("write failed")

        monkeypatch.setattr(pathlib.Path, "write_text", boom)
        rm._archive_entries("p", "backend", ["- something"])  # must not raise


class TestAppendFailureEntry:
    """Auto-capture of `done(failed=True)` reports (ReflexionMemory-style) —
    no agent decision required, unlike every other bullet in role-memory."""

    def test_writes_under_flaky_section_for_qa(self, isolated_role_memory: pathlib.Path) -> None:
        ensure_role_memory("p", "qa")
        assert rm.append_failure_entry("p", "qa", "login smoke failed: 500 on /auth") is True
        text = rm.role_memory_path("p", "qa").read_text(encoding="utf-8")
        flaky = text.split("## Flaky / known-failing")[1]
        assert "login smoke failed: 500 on /auth" in flaky

    def test_falls_back_to_gotchas_when_no_flaky_section(
        self, isolated_role_memory: pathlib.Path
    ) -> None:
        # backend's seed has no "## Flaky / known-failing" — must not invent one.
        ensure_role_memory("p", "backend")
        assert rm.append_failure_entry("p", "backend", "migration crashed on empty table") is True
        text = rm.role_memory_path("p", "backend").read_text(encoding="utf-8")
        assert "## Flaky / known-failing" not in text
        gotchas = text.split("## Gotchas / pitfalls")[1]
        assert "migration crashed on empty table" in gotchas

    def test_seeds_file_when_missing(self, isolated_role_memory: pathlib.Path) -> None:
        # Never called before ensure_role_memory (e.g. first-ever failure) —
        # must seed the file itself rather than no-op.
        path = rm.role_memory_path("p", "frontend")
        assert not path.exists()
        assert rm.append_failure_entry("p", "frontend", "build broke on missing env var") is True
        assert path.exists()
        assert "build broke on missing env var" in path.read_text(encoding="utf-8")

    def test_entry_prefixed_with_date_and_fail_marker(
        self, isolated_role_memory: pathlib.Path
    ) -> None:
        rm.append_failure_entry("p", "qa", "checkout 500")
        text = rm.role_memory_path("p", "qa").read_text(encoding="utf-8")
        m = rm._FAIL_ENTRY_RE.search(text)
        assert m is not None
        assert m.group(1) == "checkout 500"

    def test_dedup_same_reason_across_calls(self, isolated_role_memory: pathlib.Path) -> None:
        rm.append_failure_entry("p", "qa", "checkout 500 on submit")
        second = rm.append_failure_entry("p", "qa", "  Checkout 500 on submit  ")
        assert second is False  # dedup no-op, not an error
        text = rm.role_memory_path("p", "qa").read_text(encoding="utf-8")
        assert text.lower().count("checkout 500 on submit") == 1

    def test_distinct_reasons_both_kept(self, isolated_role_memory: pathlib.Path) -> None:
        rm.append_failure_entry("p", "qa", "checkout 500 on submit")
        rm.append_failure_entry("p", "qa", "login 500 on auth")
        text = rm.role_memory_path("p", "qa").read_text(encoding="utf-8")
        assert "checkout 500 on submit" in text
        assert "login 500 on auth" in text

    def test_empty_reason_is_noop(self, isolated_role_memory: pathlib.Path) -> None:
        assert rm.append_failure_entry("p", "qa", "") is False
        assert rm.append_failure_entry("p", "qa", "   ") is False

    def test_respects_entry_char_cap(self, isolated_role_memory: pathlib.Path) -> None:
        rm.append_failure_entry("p", "backend", "x " * 500)
        text = rm.role_memory_path("p", "backend").read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if ln.startswith("- ") and "fail —" in ln]
        assert len(lines) == 1
        assert len(lines[0]) <= rm._MEM_MAX_ENTRY_CHARS

    def test_best_effort_on_write_failure(
        self, isolated_role_memory: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ensure_role_memory("p", "qa")

        def boom(*_a, **_k):
            raise OSError("write failed")

        monkeypatch.setattr(pathlib.Path, "write_text", boom)
        assert rm.append_failure_entry("p", "qa", "anything") is False  # must not raise

    def test_curation_runs_immediately_so_budget_never_bloats(
        self, isolated_role_memory: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rm, "_MEM_MAX_ENTRIES", 3)
        ensure_role_memory("p", "qa")
        for i in range(10):
            rm.append_failure_entry("p", "qa", f"distinct failure number {i}")
        text = rm.role_memory_path("p", "qa").read_text(encoding="utf-8")
        n_bullets = sum(1 for ln in text.splitlines() if rm._BULLET_RE.match(ln))
        assert n_bullets <= 3
        assert "distinct failure number 9" in text  # newest survives


class TestDistillPending:
    """One-shot flag (#distill): when curation cuts real entries out to the L2
    archive, the role's NEXT spawn should get a nudge to consolidate its
    surviving notes instead of quietly losing knowledge to age-based trimming
    forever. `_archive_entries` sets it; `consume_distill_pending` clears it."""

    def test_archiving_real_entries_sets_flag(self, isolated_role_memory: pathlib.Path) -> None:
        assert rm.consume_distill_pending("p", "backend") is False  # nothing yet
        rm._archive_entries("p", "backend", ["- something that overflowed"])
        assert rm.role_memory_distill_flag_path("p", "backend").exists()

    def test_empty_archive_call_does_not_set_flag(self, isolated_role_memory: pathlib.Path) -> None:
        rm._archive_entries("p", "backend", [])
        assert not rm.role_memory_distill_flag_path("p", "backend").exists()

    def test_consume_is_one_shot(self, isolated_role_memory: pathlib.Path) -> None:
        rm._archive_entries("p", "backend", ["- overflow entry"])
        assert rm.consume_distill_pending("p", "backend") is True
        assert rm.consume_distill_pending("p", "backend") is False  # already cleared
        assert not rm.role_memory_distill_flag_path("p", "backend").exists()

    def test_flag_survives_l2_write_failure(
        self, isolated_role_memory: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The L1→L2 knowledge loss already happened even if the archive write
        # itself fails — the flag must still be set so the nudge isn't lost.
        def boom(*_a, **_k):
            raise OSError("write failed")

        monkeypatch.setattr(pathlib.Path, "write_text", boom)
        rm._archive_entries("p", "backend", ["- something"])
        assert rm.consume_distill_pending("p", "backend") is True

    def test_ensure_role_memory_curation_sets_flag_via_size_cap(
        self, isolated_role_memory: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end through the real curation path (not calling
        # _archive_entries directly): a size-cap trim during ensure_role_memory
        # must also flip the flag.
        monkeypatch.setattr(rm, "_MEM_MAX_ENTRIES", 5)
        path = ensure_role_memory("p", "backend")
        notes = "".join(f"\n- note number {i:03d}\n" for i in range(20))
        path.write_text(path.read_text(encoding="utf-8") + notes, encoding="utf-8")
        assert rm.consume_distill_pending("p", "backend") is False  # not yet curated
        ensure_role_memory("p", "backend")  # triggers the trim
        assert rm.consume_distill_pending("p", "backend") is True

    def test_per_project_and_role_isolation(self, isolated_role_memory: pathlib.Path) -> None:
        rm._archive_entries("proj_a", "backend", ["- overflow"])
        assert rm.consume_distill_pending("proj_a", "backend") is True
        assert rm.consume_distill_pending("proj_a", "frontend") is False
        assert rm.consume_distill_pending("proj_b", "backend") is False

    def test_best_effort_mark_does_not_raise(
        self, isolated_role_memory: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a, **_k):
            raise OSError("touch failed")

        monkeypatch.setattr(pathlib.Path, "touch", boom)
        rm._archive_entries("p", "backend", ["- something"])  # must not raise

    def test_best_effort_consume_does_not_raise_on_unlink_failure(
        self, isolated_role_memory: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rm._archive_entries("p", "backend", ["- something"])

        def boom(*_a, **_k):
            raise OSError("unlink failed")

        monkeypatch.setattr(pathlib.Path, "unlink", boom)
        assert rm.consume_distill_pending("p", "backend") is False  # swallowed, not raised


class TestHasLearnedContent:
    """tok-5: a freshly-seeded role-memory file (skeleton only) must read as
    *empty* so the spawn path injects a one-line pointer instead of the whole
    skeleton; the moment a real bullet is appended it must read as *has content*
    so the full learned-notes block returns."""

    def test_seeded_file_is_empty(self, isolated_role_memory: pathlib.Path) -> None:
        # Every seeded role file is just placeholders (incl. the one content-shaped
        # "(ว่าง — เติมเมื่อเรียนรู้)" bullet) → no real learned content.
        for role in ("frontend", "backend", "qa", "devops", "reviewer"):
            text = ensure_role_memory("p", role).read_text(encoding="utf-8")
            assert rm.has_learned_content(text, "p", role) is False, role

    def test_real_bullet_counts_as_content(self) -> None:
        assert (
            rm.has_learned_content("## Conventions\n- use pnpm not npm here\n", "p", "frontend")
            is True
        )
        assert rm.has_learned_content("## X\n* starred bullet too\n", "p", "backend") is True

    def test_bare_placeholder_does_not_count(self) -> None:
        # The seed's bare "-" / "-\n" placeholders must not register as content.
        assert (
            rm.has_learned_content("## Gotchas / pitfalls\n-\n\n## Key decisions\n-\n", "p", "qa")
            is False
        )

    def test_seed_va_placeholder_does_not_count(self) -> None:
        # The "(ว่าง …)" content-shaped seed bullet must be excluded too.
        assert rm.has_learned_content("- (ว่าง — เติมเมื่อเรียนรู้)\n", "p", "frontend") is False

    def test_empty_string(self) -> None:
        assert rm.has_learned_content("", "p", "qa") is False

    def test_appended_note_flips_to_content(self, isolated_role_memory: pathlib.Path) -> None:
        path = ensure_role_memory("p", "frontend")
        assert rm.has_learned_content(path.read_text(encoding="utf-8"), "p", "frontend") is False
        path.write_text(path.read_text(encoding="utf-8") + "\n- learned: ports are 3001\n", "utf-8")
        assert rm.has_learned_content(path.read_text(encoding="utf-8"), "p", "frontend") is True
