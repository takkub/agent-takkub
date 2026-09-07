"""issue #516 point 5 — boot-context ceiling ratchet.

Recomputes `boot_context.build_report` for every role in
`docs/audit/boot-context-baseline.json` and fails if that role's
REPO-CONTROLLED categories (role_file_base, guards_fixed, graft_caveats,
permission_gate_appendix, skill_matrix_appendix, repo/global CLAUDE.md,
mcp_config — `RoleBootReport.repo_controlled_est_tokens`) grew past
`allowance_ratio` since that baseline was captured. Catches a role-file/
appendix regression (someone adds a big new guard block, etc.) — it does NOT
validate an absolute real-token budget; see
docs/audit/2026-09-07-boot-context.md section 2 for why the estimator
cannot be trusted for that.

#516b (addendum, Lead 2026-09-07): the categories in
`boot_context.DYNAMIC_STATE_CATEGORIES` (learned_notes,
project_memory_pointer, native_project_memory*) are deliberately EXCLUDED
from this gate. They are per-machine runtime state a pane's own `takkub
done` writes throughout the day, not repo content — a dev machine mid-day
with a role that just accumulated new learned notes is not a regression,
and gating on it produced a false-positive red gate (a real incident: every
role grew by the same ~826 naive tokens the day `learned_notes` picked up
fresh content, while a clean CI checkout with no accumulated state stayed
green). `test_dynamic_state_categories_reported_not_gated` below documents
that these are still measured and reported, just never failed on.

Run targeted (never full gate, per #485/#325):
    PYTHONPATH=src py -3 -m pytest tests/test_boot_context_ceiling.py
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_takkub import boot_context

_BASELINE_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "audit" / "boot-context-baseline.json"
)


def _load_baseline() -> dict:
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


def test_baseline_file_is_well_formed():
    data = _load_baseline()
    assert data["roles"], "baseline must list at least one role"
    assert 1.0 < data["allowance_ratio"] < 3.0


def test_per_role_boot_context_within_allowance():
    data = _load_baseline()
    project = data["project"]
    allowance = data["allowance_ratio"]
    failures: list[str] = []
    for role, baseline in data["roles"].items():
        report = boot_context.build_report(role, project)
        ceiling_tokens = baseline["repo_controlled_est_tokens"] * allowance
        if report.repo_controlled_est_tokens > ceiling_tokens:
            failures.append(
                f"{role}: {report.repo_controlled_est_tokens} repo-controlled tok now vs "
                f"baseline {baseline['repo_controlled_est_tokens']} tok (ceiling "
                f"{ceiling_tokens:.0f} @ {allowance}x) — regenerate "
                "docs/audit/boot-context-baseline.json if this growth is intentional, "
                "else find what regressed"
            )
    assert not failures, "\n".join(failures)


def test_dynamic_state_categories_reported_not_gated():
    """#516b: learned_notes/native_project_memory/project_memory_pointer are
    real per-machine state, not a repo regression — they must never enter
    the ceiling sum, even when huge on this machine right now."""
    data = _load_baseline()
    report = boot_context.build_report("frontend", data["project"])
    assert report.repo_controlled_est_tokens <= report.total_est_tokens
    dynamic_cats = [
        c for c in report.categories if c.category in boot_context.DYNAMIC_STATE_CATEGORIES
    ]
    # Whatever this machine's accumulated dynamic state happens to be right
    # now, it must be fully excluded from the gated subtotal.
    assert report.repo_controlled_est_tokens == report.total_est_tokens - sum(
        c.est_tokens for c in dynamic_cats
    )


def test_native_project_memory_measurable_not_gated():
    """#516b: native_project_memory (Lead's own, post-#516-F1) is real
    per-machine session-history state that grows every release — NOT a repo
    regression signal. Smoke-checks the measurement call still succeeds;
    deliberately does not assert a ceiling (see module docstring)."""
    data = _load_baseline()
    native = boot_context.measure_native_project_memory(data["project"])
    assert native is None or native.est_tokens >= 0
