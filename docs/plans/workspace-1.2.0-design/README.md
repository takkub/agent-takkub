# Takkub Workspace & Design Upgrade — 1.1.0 Candidate

Repo: https://github.com/takkub/agent-takkub
Baseline checked: 2026-08-23
Observed main anchor: `19de510a03b64d0fbdd6d69bc8997ae224d84c84`
Known package version around this baseline: `1.0.87`

## Goal
Turn Takkub from an Agent Cockpit into a lightweight Agent Development Workspace without rewriting V2.

Deliverables:
- Collapsible Project Explorer
- Embedded Monaco Editor
- Safe save/conflict handling
- Git changes + diff
- Per-project Live Preview
- Design Director workflow
- Design artifact auto-preview
- Approve / Revise
- Agent ↔ Editor ↔ Preview integration
- Optional design MCP integrations
- Clear Brain / Obsidian / OpenViking / Graft boundaries

## Non-negotiable
- Do not rewrite V2.
- Do not remove/replace Graft.
- Do not replace Brain V2 or Conversation V2.
- OpenViking is optional and external/sidecar-oriented.
- Do not vendor OpenViking AGPL code into Takkub MIT repo.
- No local-LLM requirement.
- Do not build a full VS Code clone.
- No heavy FS/git/network work on Qt main thread.
- Never re-parent a painted QWebEngineView between projects.
- Never silently overwrite a file changed by another process/agent.
