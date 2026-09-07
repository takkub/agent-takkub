"""issue #516 point 5 — boot-context ceiling ratchet.

Recomputes `boot_context.build_report` for every role in
`docs/audit/boot-context-baseline.json` and fails if any role's naive
chars/est-tokens total grew past `allowance_ratio` since that baseline was
captured. Catches a role-file/appendix regression (someone adds a big new
guard block, a role's learned notes stop truncating, etc.) — it does NOT
validate an absolute real-token budget; see
docs/audit/2026-09-07-boot-context.md section 2 for why the naive estimator
cannot be trusted for that.

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
        ceiling_tokens = baseline["total_est_tokens"] * allowance
        if report.total_est_tokens > ceiling_tokens:
            failures.append(
                f"{role}: {report.total_est_tokens} tok now vs baseline "
                f"{baseline['total_est_tokens']} tok (ceiling {ceiling_tokens:.0f} @ "
                f"{allowance}x) — regenerate docs/audit/boot-context-baseline.json "
                "if this growth is intentional, else find what regressed"
            )
    assert not failures, "\n".join(failures)


def test_native_project_memory_within_allowance():
    data = _load_baseline()
    baseline = data.get("native_project_memory")
    if baseline is None:
        return
    native = boot_context.measure_native_project_memory(data["project"])
    if native is None:
        return
    ceiling_tokens = baseline["est_tokens"] * data["allowance_ratio"]
    assert native.est_tokens <= ceiling_tokens, (
        f"native_project_memory grew to {native.est_tokens} tok vs baseline "
        f"{baseline['est_tokens']} tok (shared across EVERY role's pane — see "
        "docs/audit/2026-09-07-boot-context.md finding F1)"
    )
