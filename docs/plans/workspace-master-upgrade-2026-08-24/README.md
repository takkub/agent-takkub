# Takkub Master Upgrade — Complete Pack

Target: `takkub/agent-takkub`
Baseline observed: v1.2.1
Prepared: 2026-08-24

This pack supersedes the earlier Workspace/Design and Remaining-Fixes packs.

## Objective

Finish the full Takkub workspace/design/context architecture in one coordinated roadmap:

- Explorer
- Monaco Editor
- safe file editing
- Git Changes/Diff
- Live Preview
- Design Director + Design Review
- Storybook/21st/Figma/Penpot integrations
- Brain / Conversation boundaries
- Obsidian hardening
- OpenViking optional context database
- Graft code intelligence
- Context Builder integration
- security
- diagnostics
- QA
- release/rollback

## Important

Do NOT blindly rewrite features that already exist. Lead must inspect current `main`,
mark each item as DONE / PARTIAL / MISSING / BUG, then only implement remaining work.

Phase 10 / V2 authority is a separate migration concern and must not be accidentally
changed by workspace/design fixes. Coordinate explicitly if both are active.
