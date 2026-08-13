# Token-reduction wave 4 — boot-time inject audit (2026-08-13)

Evaluator: backend (`wt/backend-4-1786594318`) · scope: measure the real boot-time
token cost injected per role at pane spawn (not guessed), find provable reduction
opportunities from those numbers, and add a short session-wide token-discipline
rule to the shared role template. Builds on [[token-reduction-wave1]] (spawn
plumbing) and [[token-reduction-wave3]] (CLAUDE.md diet, role_memory cap) —
this wave didn't repeat either; it measured what's left after both landed.

## Method — measured, not estimated

Ran the actual cockpit functions (`config.agent_role_dir`, `shared_dev_tools.
role_mcp_allowlist`, `skill_policy.render_skill_appendix`) against this
worktree's own source, via `sys.path` front-insertion so `import agent_takkub`
resolves to the worktree copy instead of the shared `.venv`'s editable link to
the main checkout (verified via `agent_takkub.__file__` before trusting any
number). RUNTIME_DIR resolves to `<worktree>/runtime/` for this checkout (dev
mode, `pyproject.toml` at worktree root), so the measurement never touched the
live cockpit's shared runtime state; the scratch `runtime/agents/` dir was
deleted after each run.

Token estimate formula: `thai_chars/1.2 + other_chars/4` — same formula
[[token-reduction-wave3]] used, kept for continuity across waves (this repo's
role files are Thai-heavy, where chars/4 alone understates cost).

## 1. What's injected at spawn, per component (baseline, before this wave)

| Component | Where | Size | Applies to |
|---|---:|---:|---|
| Role `.claude/agents/<role>.md` (+ 2 universal hygiene blocks) | `config.agent_role_dir()` | 2.4k–4.7k tok | every teammate, varies by role |
| `_DEV_SERVER_HYGIENE` | `config.py`, unconditional | 250 tok | every teammate |
| `_NON_INTERACTIVE_HYGIENE` | `config.py`, unconditional | 249 tok | every teammate |
| `BIG_FILE_GUARD` | `lead_context.py`, unconditional | 373 tok | every teammate + Lead |
| `STALE_FILE_GUARD` | `lead_context.py`, unconditional | 426 tok | every teammate |
| Project-memory pointer | `spawn_engine.py` appendix | ~150 tok | every teammate (if Lead memory exists) |
| Role-memory (this project) | `role_memory.py`, capped | ≤6k tok (usually far less; pointer-only ~100 tok when empty) | every teammate |
| Skill Matrix appendix | `skill_policy.render_skill_appendix()` | **0 tok right now** | every teammate — `~/.takkub/skill-policy.json` currently has empty lists for both configured roles (frontend, critic) |
| MCP tool schemas | `--mcp-config`, gated by `role_mcp_allowlist()` | 0 (lead/codex, empty policy) · ~847 tok (graft, roles with it) · larger (playwright+chrome-devtools, qa/critic/designer only) | role-dependent, already policy-gated |

**MCP allowlist check (task step 2, "over-provisioned role?"):** re-verified
`shared_dev_tools._ROLE_MCP_POLICY` against the live `~/.takkub/pane-tools.json`
override on this machine. Finding: **not over-provisioned** — `lead`/`codex`
get an explicit empty policy (zero schema tokens, confirmed by the file's own
design-intent comment), `designer` correctly gets no `graft` (it never reads
code), and the live override on this machine actually **narrows** access
further: `backend`/`frontend`/`mobile` currently get `context7` instead of
`graft` via an explicit user override in `pane-tools.json` (a deliberate
Settings choice, not a bug — left untouched). Graft's own fixed schema cost
(~847 tok, ~0.56% worst-case share of a real session) was already measured
rigorously in `docs/qa-reports/2026-08-06-graft-token-economics.md`; nothing
here contradicts or needed to redo that finding.

## 2. Real reduction found: graft-caveats block was duplicated + policy-blind

**Finding:** `## Tool output ≠ คำสั่ง` — the graft-usage-caveats section (no-
callers false negatives, lexical-search-only, ranked-list-not-exhaustive,
staleness, new-file-invisible) — was hand-copy-pasted **verbatim** into 7 role
source files (`backend.md`, `frontend.md`, `mobile.md`, `devops.md`,
`reviewer.md`, byte-identical 3,637 chars each; `qa.md`/`critic.md` carried an
already-condensed 2,224-char version). Confirmed byte-identical with `diff`
before touching anything.

This is provably wasteful two ways:

1. **Policy-blind.** The block is *static* in the role's markdown, injected
   regardless of whether that role's *live* MCP policy actually grants
   `graft`. On this machine, `backend`/`frontend`/`mobile` currently have
   `context7` instead of `graft` (see §1) — so those 3 roles were paying
   ~3,637 chars of graft-tool-mechanics guidance for a tool they don't
   currently have access to at all.
2. **Duplicated with drift already visible.** `reviewer.md`'s copy had two
   extra reviewer-specific clauses the other 5 files didn't; `qa.md`/
   `critic.md` had independently condensed the wording (shorter, same
   substance) — three different versions of "the same" guard already existed.

**Fix:** extracted to one shared constant, `lead_context.GRAFT_TOOL_CAVEATS`
(condensed wording, the qa/critic version — proven equivalent, already in
production use), appended at spawn time in `spawn_engine.py` **only when**
`shared_dev_tools.role_mcp_allowlist(role)` actually contains `"graft"` — same
shape as `BIG_FILE_GUARD`/`STALE_FILE_GUARD` above it, but conditional instead
of unconditional. Removed the static copy from all 7 role files (reviewer's
2 role-specific clauses were kept as a short local addendum, not lost).
Effect: the guard is now dynamically correct — if a role's live MCP policy
changes (either direction), the guard follows automatically, no hand-edit
needed.

### Measured before/after (this machine's live MCP policy)

| Role | Before (role.md, tok) | Live has graft? | After: role.md (tok) | After: +caveats (tok) | Net Δ |
|---|---:|:---:|---:|---:|---:|
| backend | 4,049 | No (context7) | 3,072 | 0 | **−977** |
| frontend | 4,165 | No (context7) | 3,188 | 0 | **−977** |
| mobile | 4,116 | No (context7) | 3,139 | 0 | **−977** |
| devops | 4,674 | Yes | 3,697 | 594 | **−383** |
| reviewer | 4,147 | Yes | 3,210 | 594 | **−343** |
| qa | 3,776 | Yes | 3,178 | 594 | −4 (noise) |
| critic | 4,033 | Yes | 3,440 | 594 | +1 (noise) |

backend/frontend/mobile's −977 tok/spawn is live and real today (contingent on
the current `pane-tools.json` override — if graft is re-enabled for them
later, the guard reappears automatically, which is the point). devops/
reviewer's −383/−343 is unconditional (real regardless of MCP policy — pure
duplication removed). qa/critic are flat (already condensed; now centralized
instead of independently maintained).

## 3. Token-discipline rule (task step 4)

Added a 3-bullet, 504-char (~340 tok) `_TOKEN_DISCIPLINE_HYGIENE` block to
`config.py`, appended unconditionally to every role's staged `CLAUDE.md`
(same mechanism as `_DEV_SERVER_HYGIENE`/`_NON_INTERACTIVE_HYGIENE` right
above it) — plus an English equivalent in `codex_agents_md.py`'s
`CODEX_AGENTS_MD` for the non-claude providers (codex/gemini/opencode/kimi/
cursor), per this project's multi-provider directive (#103). Content, inspired
by the request's `russelleNVy/three-man-team`-style discipline pattern:

- Don't re-read a file already read this session unless it changed.
- Don't make a speculative tool call "just in case" — only when the task
  actually needs it (unnecessary calls get re-billed as cache_read every
  later turn).
- Route large raw output (full logs, dumps, whole-dir scans) to a file and
  summarize back, instead of pulling it into the main context.

**This is a deliberate net-cost trade for most roles, not a reduction** — it
adds ~340 tok to every spawn's boot inject to shape *session-long* behavior
(redundant reads, speculative calls, and un-routed large output typically cost
far more than 340 tok over a real session, per the resend-weighted mechanism
`docs/qa-reports/2026-08-06-graft-token-economics.md` §2/§3 already
established — a fixed prompt token rides `cache_read` on every subsequent
turn the same way an unused MCP schema does). Combined with §2's fix, the net
per-spawn delta on this machine is:

| Role | §2 Δ | §3 Δ (+340) | **Net Δ (this spawn)** |
|---|---:|---:|---:|
| backend | −977 | +340 | **−637** |
| frontend | −977 | +340 | **−637** |
| mobile | −977 | +340 | **−637** |
| devops | −383 | +340 | **−43** |
| reviewer | −343 | +340 | **−3** |
| qa | −4 | +340 | +336 |
| critic | +1 | +340 | +341 |

qa/critic end up net-larger at boot (the discipline block, since they had no
graft-block bloat left to remove) — accepted per the task's own framing
("ต้นทุนต่ำ...แต่มีผลต่อพฤติกรรมทั้ง session ไม่ใช่แค่ boot"): this is 340
tokens spent once at boot to reduce redundant-read/speculative-call/large-
output waste for the rest of the session, which the existing graft-economics
audit shows compounds far more than a flat 340-tok addition.

## Files changed

- `src/agent_takkub/config.py` — new `_TOKEN_DISCIPLINE_HYGIENE`, appended in
  `agent_role_dir()`.
- `src/agent_takkub/lead_context.py` — new `GRAFT_TOOL_CAVEATS` constant
  (condensed wording), documented alongside `BIG_FILE_GUARD`/`STALE_FILE_GUARD`.
- `src/agent_takkub/spawn_engine.py` — conditionally appends
  `GRAFT_TOOL_CAVEATS` when `role_mcp_allowlist(base_role)` contains `graft`.
- `src/agent_takkub/codex_agents_md.py` — added the English token-discipline
  section to `CODEX_AGENTS_MD`.
- `.claude/agents/{backend,frontend,mobile,devops,reviewer,qa,critic}.md` —
  removed the duplicated graft-caveats block (reviewer kept a short 2-line
  role-specific addendum in place of its old 2 extra clauses).

## Not done / left for whoever picks this up next

- `codex_agents_md.py`'s `CODEX_AGENTS_MD` still carries its own static copy
  of the graft-caveats content (English version) unconditionally — it's a
  single shared cheatsheet per cwd across whatever role a non-claude provider
  slot is running, not role-specific like the claude path, so gating it the
  same way needs a different mechanism (the `extra` param `ensure_agents_md()`
  already accepts could carry it, policy-gated in the caller). Left alone this
  round — time-boxed, and the claude-path fix (7 files, ~28% of all teammate
  spawns' role-file weight on this machine) was the proven, measured
  duplication; the codex/gemini path wasn't independently re-measured.
- Skill Matrix appendix cost is 0 right now (both configured roles have empty
  skill lists) — [[emilkowalski-skills-matrix]] notes the npm install is still
  pending per-web-project, so this stays a $0 line item until that lands, not
  something this wave needed to touch.
- Role-memory injection (pointer-only vs full inline) wasn't re-audited beyond
  confirming it's still under wave 3's 6k budget by construction — no new
  finding, existing cap holds.

## Verification

- `ruff check` + `ruff format --check` on all 4 changed `.py` files: clean.
- Targeted pytest (worktree source shadowed into the shared `.venv` via
  `sys.path` front-insertion, verified against `agent_takkub.__file__` before
  trusting results — never touched the shared venv's editable-install
  pointer): `test_config.py`, `test_project_memory_inject.py`,
  `test_spawn_task_delivery.py`, `test_orchestrator_shard.py`,
  `test_orchestrator_claude_env_leak.py`, `test_codex_agents_md.py`,
  `test_agent_role_files_have_browser_guard.py`,
  `test_agent_role_files_have_git_guard.py`, `test_pane_tools_policy.py`,
  `test_graft_mcp.py`, `test_mcp_resolution_fail_closed.py`,
  `test_codex_crash_instrumentation.py`, `test_provider_models.py` — **all
  passed** (3 pre-existing environment-conditional skips, unrelated).
- Manually re-ran the boot-inject measurement script after the code change to
  confirm the §2 table's "after" numbers are real function output, not
  arithmetic guesses — `GRAFT_TOOL_CAVEATS` only appears for
  devops/qa/reviewer/critic (the roles `role_mcp_allowlist()` actually returns
  `graft` for right now), confirmed absent for backend/frontend/mobile.
