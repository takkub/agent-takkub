# Master Prompt — Paste into Takkub Lead

You are implementing the "Takkub Workspace & Design Upgrade" in `takkub/agent-takkub`.

MANDATORY FIRST STEP:
1. Inspect CURRENT `main`; do not trust stale file paths.
2. Record HEAD SHA and version.
3. Read current architecture docs plus `project_tab.py`, `main_window.py`, `terminal_widget.py`, `agent_pane.py`, `cli.py`, `cli_server.py`, `design_review_html.py`, Graft integration, V2 core contracts, Capability Hub/PermissionEngine, storage/migration docs.
4. Produce a short delta report between current code and this plan before implementation.
5. Adapt seams if current architecture has improved, while preserving requirements.

GOAL:
- collapsible Project Explorer,
- embedded Monaco,
- safe save/conflict,
- Git changes/diff,
- per-project Live Preview,
- Design Director,
- artifact auto-preview,
- Approve/Revise,
- optional design integrations.

NON-NEGOTIABLE:
- Do not rewrite V2.
- Do not remove/replace Graft.
- Do not replace Brain/Conversation V2.
- OpenViking optional only; no vendored AGPL source.
- No local LLM requirement.
- Not a VS Code clone.
- No heavy FS/git/network work on Qt main thread.
- Never re-parent painted QWebEngineView across projects.
- Writes confined to configured project roots.
- Never silently overwrite concurrent changes.
- Existing Cockpit must degrade gracefully when new optional pieces fail.

SEQUENCE:
0 Audit -> 1 Explorer -> 2 Read-only Monaco -> 3 Safe edit -> 4 Git/diff -> 5 Preview -> 6 Design workflow -> 7 Optional design MCP -> 8 Obsidian hardening -> 9 Optional OpenViking -> 10 diagnostics/soak.

DELEGATION:
- UI/frontend: Explorer/Editor/Preview UX
- core/backend: safe file service/watchers/git
- security/reviewer: path confinement/WebEngine bridge
- critic/design: design policy/review UX
- QA last: regression, Windows, conflicts, Unicode, large repos
- release/devops: local Monaco packaging + release gates

Avoid parallel branches that simultaneously restructure `ProjectTab`/`MainWindow`.

PER PHASE deliver:
- code,
- tests,
- architecture note,
- rollback note if persistent state changes,
- test evidence,
- known limitations.

Treat as feature work suitable for a 1.1.0 candidate under current SemVer policy unless current repo policy has changed. Keep Phase 10/2.0.0 legacy-deprecation semantics orthogonal.

FINAL VERIFY current equivalents of pytest, ruff, lint-imports, Takkub QA gate, UI smoke/soak.
Final report must list modules added/changed, boundaries, security controls, evidence, limitations, rollout/rollback.
