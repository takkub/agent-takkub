# Test Plan

Unit:
- project-root containment, traversal/symlink escape,
- size/encoding guards,
- conflict version/hash,
- ignore policy,
- preview artifact validation,
- design state transitions.

Qt/UI:
- explorer collapse/expand,
- project switch state,
- editor open/focus/close,
- per-project Preview ownership,
- keepalive,
- no WebEngine reparent regression.

Integration:
- terminal path -> editor,
- editor save -> HMR -> preview,
- agent changes open file -> conflict,
- design publish -> preview -> approve/revise.

Git: modified/added/deleted/renamed, dirty tree, worktree isolation, Thai/Unicode/spaces.

Performance: 10k+ file repo, project switching, repeated git refresh, many panes + editor + preview.

Regression: current pytest, ruff, lint-imports, current Takkub QA gate and UI smoke. Do not edit old expected values merely to pass.
