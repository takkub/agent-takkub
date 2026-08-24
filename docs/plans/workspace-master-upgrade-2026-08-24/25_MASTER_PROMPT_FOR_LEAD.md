# MASTER PROMPT — COMPLETE TAKKUB UPGRADE

You are Lead for `takkub/agent-takkub`.

This package is the authoritative coordinated roadmap for finishing the Workspace + Design + Knowledge architecture.

## FIRST: inspect current main

Do not assume v1.2.1 is still current.

1. Record HEAD SHA/version.
2. Read current issue/release state.
3. Recheck every bug/gap in `02_BUG_REGISTER.md`.
4. Create a matrix:
   - DONE
   - PARTIAL
   - MISSING
   - BUG STILL PRESENT
   - ALREADY FIXED
5. Do not reimplement already-fixed work.

## Order

### Batch A — release blockers
- Preview file:// normalization.
- project-aware Preview.
- Preview cleanup on project close.
- editor permission preservation.
- strict UTF-8/BOM safety.
- revise -> Designer routing.

Run targeted tests and reviewer.

### Batch B — Git correctness/completeness
- deleted diff
- rename old_path
- multi-root repos

### Batch C — Explorer
- Ask Agent
- Git ignore parity

### Batch D — Design integrations
- Storybook remains first-party project source.
- implement real 21st integration.
- implement optional Figma integration.
- implement optional Penpot integration.
- all through Capability Hub/PermissionEngine.

### Batch E — OpenViking
- optional external sidecar
- pinned compatibility
- health/doctor
- shadow/read/hybrid modes
- resource/Obsidian indexing
- Context Builder merge
- provenance/latency/token traces
- never replace Brain/Conversation/Graft
- never vendor AGPL source

### Batch F — visual/production QA
- real QWebEngine
- Windows/macOS
- soak/RAM
- CI/release

## Architectural constraints

- Takkub is control plane.
- V2 Brain and Conversation remain canonical.
- Graft remains code structure.
- Obsidian remains human curated knowledge.
- OpenViking is machine retrieval/index.
- Capability Hub owns design MCPs.
- Context Builder is the ONLY final context merge/injection policy.
- One Monaco WebView app-wide.
- One Preview WebView app-wide.
- Never reparent a painted WebView.
- No heavy work on Qt main thread.
- No local LLM requirement.
- Do not modify Phase 10/V2 authority #362 unless separately authorized and necessary.

## Development discipline

- For concurrent work touching the same UI/lifecycle files, serialize merges.
- Security/reviewer pass after file-write, Preview, MCP, OpenViking changes.
- QA validates real user flows, not only stubs.
- Do not mark acceptance complete from unit tests when criterion is visual/runtime.
- Do not change expected tests merely to pass.

## Final deliverable

Produce:
1. current-state delta matrix,
2. commits/phases completed,
3. remaining gaps,
4. test/CI evidence,
5. visual QA evidence,
6. security review,
7. RAM/soak evidence,
8. final architecture diagram,
9. release version recommendation,
10. explicit statement whether V2 authority/Phase 10 was touched.
