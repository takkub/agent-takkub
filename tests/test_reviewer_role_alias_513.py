"""#513 (backend half): qa + reviewer + critic merged into one `reviewer`
role taking `--mode code|e2e|ui`.

Covers the parts of the merge that live outside `routing_planner.py` (see
`tests/test_routing_planner.py::TestReviewerModeAliasMapping` and friends for
the classification-logic half):

* `qa.md` / `critic.md` are kept as working files (never deleted) and now
  carry a real, visible deprecation banner pointing at `reviewer --mode …`.
* `reviewer.md` documents the unified `--mode code|e2e|ui` interface and the
  keyword → mode mapping, without dropping the original code-review workflow
  or the qa/critic role files' full content (shard/browser workflow for e2e,
  gemini cross-check pipeline for ui).
"""

from __future__ import annotations

from pathlib import Path

AGENTS_DIR = Path(__file__).parent.parent / ".claude" / "agents"
QA_MD = AGENTS_DIR / "qa.md"
CRITIC_MD = AGENTS_DIR / "critic.md"
REVIEWER_MD = AGENTS_DIR / "reviewer.md"
DOCS_DIR = Path(__file__).parent.parent / "docs" / "lead"
ROLE_AND_WORKFLOW_MD = DOCS_DIR / "role-and-workflow.md"
PATTERNS_MD = DOCS_DIR / "patterns.md"


def _read(path: Path) -> str:
    assert path.exists(), f"{path} must not be deleted — #513 keeps it as an alias for >= 1 release"
    return path.read_text(encoding="utf-8")


class TestDeprecatedRoleFilesKeptAsAliases:
    def test_qa_md_still_exists(self):
        assert QA_MD.exists()

    def test_critic_md_still_exists(self):
        assert CRITIC_MD.exists()

    def test_qa_md_deprecation_banner_present(self):
        content = _read(QA_MD)
        assert "#513" in content
        assert "DEPRECATED" in content
        assert "reviewer" in content
        assert "--mode e2e" in content

    def test_critic_md_deprecation_banner_present(self):
        content = _read(CRITIC_MD)
        assert "#513" in content
        assert "DEPRECATED" in content
        assert "reviewer" in content
        assert "--mode ui" in content

    def test_qa_md_deprecation_banner_precedes_original_workflow(self):
        """The warning must actually surface when the alias is invoked — it
        has to appear before (not after/instead of) the real instructions,
        since `agent_role_dir()` ships the whole file body verbatim to the
        spawned pane."""
        content = _read(QA_MD)
        banner_pos = content.index("DEPRECATED ALIAS")
        workflow_pos = content.index("## Workflow")
        assert banner_pos < workflow_pos

    def test_critic_md_deprecation_banner_precedes_original_workflow(self):
        content = _read(CRITIC_MD)
        banner_pos = content.index("DEPRECATED ALIAS")
        workflow_pos = content.index("## Workflow")
        assert banner_pos < workflow_pos

    def test_qa_md_shard_e2e_workflow_untouched(self):
        """mode=e2e must keep the full browser e2e shard workflow — the
        banner only prepends, it must not have replaced any of this."""
        content = _read(QA_MD)
        assert "Playwright MCP" in content
        assert "--plan --shards" in content or "shard" in content.lower()
        assert "qa-gate" in content

    def test_critic_md_gemini_pipeline_untouched(self):
        """mode=ui must keep the full critic pipeline (proposal + gemini
        cross-check) — the banner only prepends, it must not have replaced
        any of this."""
        content = _read(CRITIC_MD)
        assert "gemini" in content.lower()
        assert "design-review-pipeline" in content


class TestReviewerDocumentsUnifiedModes:
    def test_reviewer_md_documents_three_modes(self):
        content = _read(REVIEWER_MD)
        assert "--mode code|e2e|ui" in content
        assert "--mode code" in content
        assert "--mode e2e" in content
        assert "--mode ui" in content

    def test_reviewer_md_references_513(self):
        assert "#513" in _read(REVIEWER_MD)

    def test_reviewer_md_points_at_qa_and_critic_for_their_modes(self):
        content = _read(REVIEWER_MD)
        assert "qa.md" in content
        assert "critic.md" in content

    def test_reviewer_md_default_code_workflow_unchanged(self):
        """The original code-review workflow steps must still be present —
        the mode section is additive, not a replacement."""
        content = _read(REVIEWER_MD)
        assert "snyk test" in content
        assert "OWASP" in content


class TestLeadDocsRoutingTableUpdated:
    """docs/lead/role-and-workflow.md and patterns.md must match the #513
    routing change, not just describe the pre-merge qa/critic/reviewer split."""

    def test_role_and_workflow_routing_table_names_reviewer_modes(self):
        content = _read(ROLE_AND_WORKFLOW_MD)
        assert "reviewer `--mode e2e`" in content
        assert "reviewer `--mode ui`" in content
        assert "reviewer `--mode code`" in content
        assert "#513" in content

    def test_role_and_workflow_notes_resolve_role_alias(self):
        content = _read(ROLE_AND_WORKFLOW_MD)
        assert "resolve_role_alias" in content

    def test_patterns_md_shard_section_names_513_alias(self):
        content = _read(PATTERNS_MD)
        assert "#513" in content
        assert 'resolve_role_alias("qa")' in content

    def test_patterns_md_critic_pipeline_names_513_alias(self):
        content = _read(PATTERNS_MD)
        assert 'resolve_role_alias("critic")' in content
