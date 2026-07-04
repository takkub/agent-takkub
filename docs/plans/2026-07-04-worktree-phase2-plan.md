# Worktree isolation — Phase 2 plan (#81)

**Date:** 2026-07-04
**Status:** P2.1–P2.4 + Tier 2c SHIPPED overnight 2026-07-04 (`4aa315b` config,
`450e8c5` link engine, `2c28259` ports, `6da3c74` worktree CLI, `93a12e7`
routing DAG; `00f869c` cross-platform fix CI-caught). **Remaining: P2.5
acceptance e2e (needs the user: real web project + cockpit restart) and the
token limiter (deliberately after P2.5 measurements).**
Phase 1 (`8ecc770`) + Phase 1.5 Multi-mode wiring (`563cd62`) shipped earlier, CI green.
**Prereq reading:** `docs/plans/2026-07-03-external-idea-integration-roadmap.md` (Tier 3 section
carries the Phase-1 design, the live-e2e findings, and the agent-orchestrator blueprint).

Phase 1 delivered build-only isolation: a pane gets its own worktree + branch,
commits there, and the Lead merges via proposal. What it deliberately could NOT
do: run a dev server inside the worktree (no `node_modules`/`.env`), so browser
QA still happens post-merge in the main tree. Phase 2 removes that limit for
real web projects, carefully — these are exactly the blind spots (env
propagation, Windows file locks, port collisions) the 2-model cross-check
flagged as the expensive part.

## Work items (mirrored in the session task list)

| # | Item | Depends on | Risk note |
|---|---|---|---|
| P2.1 | Env-propagation config schema — per-project `symlinks:[...]` + `postCreate:[...]`, opt-in; no config = Phase-1 bare worktree | — | schema only, zero runtime risk |
| P2.2 | Link + postCreate engine in `worktree_manager` — Windows dirs = NTFS **junction** (no admin), files = copy, macOS = symlink; postCreate bounded + off-main-thread; link failure → warn + bare worktree | P2.1 | ⚠️ removal must delete the LINK never the target (junction-aware tests mandatory) |
| P2.3 | Per-worktree dev-server port allocator + `TAKKUB_WT_PORT`/`PORT` env inject; probe-bind to skip taken ports | P2.2 | re-opens the file-lock blind spot → dev-server-in-worktree stays opt-in per assign; safe_remove may rightfully refuse while a server holds handles |
| P2.4 | `takkub worktree list / merge --role / clean [--force]` — Lead merge assist replacing the raw 3-command proposal; still propose-then-fire | — (parallel) | lead-token gated like other lead-only commands |
| P2.5 | **Acceptance gate:** live e2e on a real Next.js project — 2 features fanned out isolated, own dev servers, QA per branch, sequential Lead merges; measure wall-clock vs sequential, conflicts, crashes, disk | P2.1–P2.4 | findings fixed in-tree, same loop as Phase-1 e2e |

Parallel tracks (separate from Phase 2, in the task list too):
- **Tier 2c** — lightweight dependency DAG in `routing_planner` ("frontend depends
  on backend schema → sequence" + verify-fail classification). Value rose after
  Multi×worktree: a wrong parallel split now costs a worktree each.
- **Token queue / rate limiter** (blind spot #5) — deliberately AFTER P2.5's
  measurements prove RPM/TPM saturation; design-sketch-then-propose.

## Doctrine carried over from Phase 1 (unchanged)
- Merge is ALWAYS a Lead proposal behind user confirm — no auto-merge at any phase.
- A dirty worktree is never force-deleted by an automatic path (2-tier destroy).
- Every failure degrades to shared-cwd + a Lead warning; `--isolation worktree`
  must never be worse than a plain assign.
- Cross-platform: every platform-specific branch has its sibling (win32/darwin),
  CI matrix must stay green on both.
