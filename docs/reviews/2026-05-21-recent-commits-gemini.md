# Review of Recent Commits (ca06e2b..8a7b2bc)

## Architecture Cohesion → Security vs. Resiliency
### Observation
The introduction of the environment allowlist (`ab1ff5f`) and the logging of the "resume-bleed" bug (`8a7b2bc`) show a focused effort on role isolation. While the allowlist successfully isolates *secrets* (preventing teammate panes from inheriting cockpit-level API keys), the resume-bleed issue confirms that *session context* can still leak through the shared filesystem (CWD) because the `claude` CLI persistence is CWD-based.
### Evidence
- `src/agent_takkub/orchestrator.py` (lines 65-108): `_build_pane_env()` filters secrets using a case-insensitive allowlist.
- `docs/TASKS.md` (line 169): Logs the bug where `claude --continue` picks up Lead's history when a teammate spawns in the same CWD.
### Recommendation
The "Proper fix" mentioned in `TASKS.md` (scoping by session UUIDs using `--resume <uuid>`) should be prioritized. The current workaround (spawning with a `/.` CWD suffix) is a clever "hack" but fragile, as any future path normalization logic in the orchestrator or Python's `pathlib` might strip the suffix and re-enable the bleed.

## Doc/Code Consistency → Documentation Tooling
### Observation
Commit `0ec15e3` significantly improves the utility of `takkub docs-verify` by stripping fenced code blocks and auto-excluding the `docs/reviews/` directory. This reduces noise from false positives (references inside code examples or vendored reviews) while maintaining line-number accuracy for real drift detection.
### Evidence
- `src/agent_takkub/docs_verify.py` (lines 48-84): `strip_code_blocks()` preserves line count by substituting blank lines.
- Commit `0ec15e3` message: Drift count on current docs dropped from 29 to 12.
### Recommendation
With the false-positive rate now manageable, consider adding a CI step or a pre-commit hook that runs `takkub docs-verify`. This would transform the tool from a reactive diagnostic into a proactive guard against documentation rot.

## Cross-cutting Concerns → Protocol Compliance
### Observation
Commit `ca06e2b` identifies a protocol breach where a teammate reported `done` but failed to `git commit` their changes. This highlights a gap between role instructions (soft enforcement) and infrastructure behavior (hard enforcement).
### Evidence
- `docs/TASKS.md` (line 167): "Backend done-without-commit protocol bug".
### Recommendation
The proposed fix in `TASKS.md` (gating `takkub done` on a clean `git status`) is excellent. Moving the responsibility for protocol verification from the LLM (which can be "forgetful" under load) to the Orchestrator ensures that the "one task, one commit" rule is consistently followed.

## Large-Context Perspective → Multi-tab Isolation
### Observation
The transition to project-scoped keys (`project::role`) for `_recent_exits` and other orchestrator states is a critical architectural improvement for the upcoming multi-tab UI.
### Evidence
- `src/agent_takkub/orchestrator.py` (lines 816-819): `_exit_key` implementation.
- `tests/test_project_scoping.py`: Exhaustive tests covering multi-tab isolation.
### Recommendation
No concerns. the state management logic appears robust and ready for the multi-tab layout. The project-scoping of `_recent_exits` correctly prevents "bleed" between different projects, even if they use the same role names.
