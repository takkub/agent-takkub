"""Guard tests for docs/lead/role-and-workflow.md (#585 round 3).

Ensures the core playbook file:
1. Stays within the <= 4,000 token budget (Token diet 2).
2. Contains all mandatory core sections so Lead never misses critical rules at boot.
3. Keeps pointers to detailed topic files in docs/lead/*.md.
"""

from __future__ import annotations

from pathlib import Path

from agent_takkub.token_estimate import estimate_file_tokens

REPO_ROOT = Path(__file__).parent.parent
CORE_DOC = REPO_ROOT / "docs" / "lead" / "role-and-workflow.md"
DOCS_LEAD_DIR = REPO_ROOT / "docs" / "lead"
ROOT_CLAUDE = REPO_ROOT / "CLAUDE.md"


def test_core_file_exists():
    assert CORE_DOC.exists(), f"{CORE_DOC} must exist"


def test_role_and_workflow_core_token_budget():
    """Core file must be <= 4,000 tokens as estimated by agent_takkub.token_estimate."""
    tokens = estimate_file_tokens(CORE_DOC)
    assert tokens <= 4000, (
        f"docs/lead/role-and-workflow.md exceeded token budget: {tokens} > 4000 tokens"
    )


def test_mandatory_sections_present():
    """Core file must contain all mandatory sections for Lead operation."""
    content = CORE_DOC.read_text(encoding="utf-8")

    # 1. Lead role
    assert "บทบาท Lead" in content or "Core Playbook" in content

    # 2. Sizing before routing
    assert "Sizing ก่อน routing" in content or "Sizing" in content
    assert "tiny" in content and "normal" in content and "deep" in content

    # 3. Routing table
    assert "Auto-routing" in content or "Routing" in content
    assert "| Keyword | Primary | Cross-check |" in content

    # 4. Auto-fire vs Propose (3 cases)
    assert "Auto-fire" in content or "auto-fire" in content
    assert "3 กรณี" in content
    assert "Irreversible" in content or "irreversible" in content

    # 5. Done-handoff & Long-run mode
    assert "Long-run mode" in content
    assert "auto-chain" in content
    assert "fix loop" in content or "Fix loop" in content
    assert "2 รอบ" in content  # fix loop ceiling

    # 6. Direct-edit policy & tiny carve-out
    assert "Lead direct-edit policy" in content
    assert "tiny-fix carve-out" in content
    assert "#585" in content

    # 7. Anti-patterns
    assert "Anti-patterns" in content
    assert "commit & push" in content
    assert "ห้าม block wait" in content
    assert "takkub codex" in content or "takkub gemini" in content

    # 8. No re-reading rule (Token discipline)
    assert "ห้ามสั่ง pane ไปอ่านไฟล์ที่ Lead อ่านแล้ว" in content


def test_section_docs_exist_and_not_deleted():
    """Detailed topic docs moved from role-and-workflow.md must exist."""
    expected_topic_files = [
        "team-presets.md",
        "worktree-isolation.md",
        "provider-substitution.md",
        "report-publish.md",
        "noise-audit.md",
        "vault.md",
        "multi-project.md",
        "effort-and-scanning.md",
        "anti-patterns.md",
    ]
    for filename in expected_topic_files:
        path = DOCS_LEAD_DIR / filename
        assert path.exists(), f"Topic file {filename} must exist under docs/lead/"
        assert path.stat().st_size > 100, f"Topic file {filename} must not be empty"


def test_root_claude_pointer():
    """Root CLAUDE.md must reference the core playbook file."""
    assert ROOT_CLAUDE.exists()
    content = ROOT_CLAUDE.read_text(encoding="utf-8")
    assert "docs/lead/role-and-workflow.md" in content
