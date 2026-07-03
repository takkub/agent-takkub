# External-idea integration roadmap (post cross-check)

**Date:** 2026-07-03
**Status:** Tier 1 SHIPPED (ui-ux-pro-max, design-scoped). Tier 2+ awaiting sign-off.
**Inputs:** 5 repos scouted + a 2-model adversarial cross-check (codex + gemini,
both saved under `docs/reviews/2026-07-03-crosscheck-*.md`).

This doc is the de-risked plan the overnight session produced. Only the token-
meter spark fix was *built* tonight (safe, explicit ask). Everything below needs
a design decision before code, because the cross-check surfaced real blind spots
that would cause rework if built blind.

---

## 1. Repos evaluated

| repo | what it is | verdict |
|---|---|---|
| cc-wf-studio | visual workflow designer → cross-tool export | idea-only (validate/preview); different layer |
| pro-workflow | Claude Code plugin: correction-memory + gates + worktrees (hook-heavy) | mine ideas, don't install (expensive) |
| ui-ux-pro-max-skill | UI/UX design skill (KB + BM25, lazy) | **best fit / cheapest** — Tier 1 |
| agent-orchestrator | parallel agents in isolated git worktrees + CI/PR/conflict feedback routing | closest peer; validates worktree + feedback-routing |
| AgentSkillOS | skill-tree + LLM-retrieval + DAG over 200K skills | corpus is overkill; the small DAG idea is worth keeping |

---

## 2. Cross-check verdict (codex + gemini converged)

Two independent models agreed on two corrections to the first-draft plan:

**Error 1 — "worktree = token~0 easy win" was wrong.** The git op is cheap, but
the *engineering* cost is the highest of all candidates. Demote to Tier 3 pilot.

**Error 2 — feedback-routing + a lightweight dependency DAG were cut/deferred,
but they are prerequisites** for parallel work to be usable. Promote to Tier 2.

Confirmed-correct cuts: Electron rewrite, 23-agent adapter layer, SQLite/CDC,
200K skill-tree corpus, visual canvas, cross-tool export.

### Blind spots to bake into any Tier-2/3 design
1. **Worktree env propagation** — `node_modules` / `.env` / secrets are NOT copied
   into a worktree → parallel agents crash on boot. Needs a dependency/env link
   strategy (symlink/hardlink node_modules; propagate local env) — expensive on
   Windows NTFS + file locks (dev servers hold handles → prune/merge crash).
2. **Worktree cwd coupling** — cwd is referenced in many places (`spawn_engine`,
   `config`, `token_meter`, `chatlog_scanner`, AGENTS.md, `role_memory`). A
   worktree needs a consistent `main→worktree` mapping for all three providers.
3. **Non-git / monorepo / dirty-tree** — must fall back to shared cwd + warn.
4. **Observability of isolation** — user won't see work in the main tree; needs
   an "isolated worktree" status chip + a diff/link back to main.
5. **API rate limits** — 4 parallel `claude` instances saturate RPM/TPM → need a
   central token queue / rate-limiter before scaling fan-out.
6. **Memory = trust boundary** — user corrections may contain secrets/private
   URLs → redact + show before persist; store provenance (date/source/scope).
7. **Prompt precedence** — CLAUDE.md vs Lead ctx vs role prompt vs role_memory vs
   AGENTS.md can conflict → define precedence before adding a memory layer.
8. **Provider parity** — test Claude + codex + gemini spawn branches, not just Claude.
9. **Evaluation metric** — measure wall-clock / rework rate / merge conflicts /
   pane crash-respawn / token-per-completed-task before declaring "worth it."

---

## 3. Revised plan

```
Tier 1  ui-ux-pro-max → design roles              (needs a small new default-scoping hook)
Tier 2  feedback-routing MVP  +  self-correction (suggested-rule)  +  lightweight DAG
Tier 3  worktree isolation pilot (opt-in, clean-git, no auto-merge, + env/dep strategy)
```

### Tier 1 — ui-ux-pro-max scoped to design roles  ✅ SHIPPED
- **Install identifiers:** marketplace `nextlevelbuilder/ui-ux-pro-max-skill`,
  plugin `ui-ux-pro-max@ui-ux-pro-max-skill`.
- **Correction to the "gap" note:** `lead_context._ROLE_PLUGIN_POLICY` already
  provides built-in per-role default scoping (roles → allowed marketplaces).
  No new mechanism was needed — the earlier "no per-role default scoping" worry
  was wrong. `pane_tools_policy.py` sits on top as the user-override layer.
- **What shipped:** added the marketplace to `config._SAFE_PLUGINS`, a
  `RecommendedPlugin` entry (so `takkub provision` installs it), and
  `_ROLE_PLUGIN_POLICY` entries for `frontend`/`critic`/`designer` only, so
  backend/devops/qa/lead never pay its context. Skill is lazy (loads on UI/UX
  requests, local BM25 — no hook tax). Tests: `test_plugin_policy` +
  `test_plugin_installer`.

### Tier 2a — feedback routing MVP
- Route a failing verify/CI/test result (and merge conflicts) back to the pane
  that produced the change, for an autonomous fix pass — an extension of the
  existing `verify` / `auto_chain` / `done_notice` loop (low blast radius).
- Start with: QA/verify failure → reassign the originating role with the failure
  excerpt. Add an event-log line (who/why) for observability.

### Tier 2b — self-correction memory (suggested-rule flow)
- Build on existing `role_memory` (already caps 16 KB / 120 entries, inline tail
  200 lines). Do NOT auto-write. Capture a user correction → propose a "suggested
  rule" the Lead/user approves before it persists. Needs: classifier, dedupe,
  scope (project/role), expiry, provenance, redaction, and a review surface.
- Define prompt precedence first (blind spot #7).

### Tier 2c — lightweight dependency DAG
- NOT a generic DAG engine and NOT the 200K corpus. A small typed route graph in
  `routing_planner.py` (rule-table + tests, matching the existing style):
  `implement → verify → classify-failure → reassign (same role / qa / devops /
  reviewer)`, and a "frontend depends on backend schema → sequence, don't
  parallelise" rule so Multi-mode fan-out doesn't cause integration failures.

### Tier 3 — worktree isolation (pilot)
- Opt-in feature flag, Multi-mode fan-out only, clean single-git-root only, cap
  2–4 shards, **no auto-merge** — emit a patch/diff proposal the Lead merges
  behind a confirm. Must solve blind spots #1–#4 first. Highest effort; do last.

---

## 4. Recommended first execution step
Tier 1 (ui-ux-pro-max), *including* the small per-role default-scoping hook in the
pane injector — it's the cheapest high-ROI item and unblocks lean design-role
tooling. Then Tier 2a (feedback routing) as the highest-ROI parallel enabler.

## 5. Open decisions for the owner
1. Ship ui-ux-pro-max in the default `RECOMMENDED` provision set? (outward-facing)
2. Add per-role default plugin scoping to the pane injector? (enables lean Tier 1)
3. Feedback-routing MVP scope: verify-fail only, or also CI + merge conflicts?
4. Self-correction: suggested-rule-approve flow confirmed (no silent auto-write)?
