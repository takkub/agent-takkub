# Plan: Selective adoption of oh-my-agent (OMA) ideas

**Date:** 2026-05-21
**Status:** Approved direction (merge of own analysis + verify gate from antigravity)
**Inputs:**
- `docs/reviews/2026-05-21-oh-my-agent-temp-gemini.md` (big-picture, bullish)
- `docs/reviews/2026-05-21-oh-my-agent-temp-codex.md` (deep code-level, cautious)
- `~/.gemini/antigravity/brain/.../implementation_plan.md` (gemini's antigravity plan — 2 of 4 features merged in, 2 rejected per Max OAuth constraint)

---

## Executive summary

Both reviewers agree the OMA codebase is high quality and active. They disagree on **how** to benefit:

- **Gemini** → vendor-in or adopt as backend
- **Codex** → ignore as a direct dependency; only **steal ideas**

This plan sides with **Codex's stance** (no dependency, no vendoring) but extracts the high-value ideas **Gemini** identified — implemented natively in Python to fit agent-takkub's stack. Rationale: OMA is an installer/runtime that wants to **own** `.agents/`, which is exactly what agent-takkub already owns. Coupling them creates an authority conflict, and OMA's defaults (mutable-main downloads, full env inheritance, auto-approve flags) are not acceptable in a host cockpit.

**Net direction:** "Study and selectively extract" — write our own small modules inspired by OMA's best ideas, run them on agent-takkub's own data, ship as native cockpit features.

---

## Synthesis: where the reviews agree vs disagree

| Topic | Gemini | Codex | Plan's stance |
|---|---|---|---|
| Code quality | Excellent | Better than prompt-pack, complex | Quality is real, complexity is also real |
| `oma docs verify/sync` | Killer feature, steal | Useful but invasive when adopted via OMA | **Steal the idea, write our own** |
| TF-IDF skill audit | Brilliant for large agent libs | Not flagged | **Steal it** (low risk, pure analysis) |
| Session quota caps | Essential | Not flagged | **Build a smaller version** |
| Wholesale adoption | Yes (as backend) | No (high integration risk) | **No** (codex wins on this axis) |
| Vendoring OMA code | Selective | Only narrow modules behind adapter | **Skip vendoring entirely** — write native Python |
| Mutable-main downloads, auto-approve, env leakage | Not flagged | HIGH severity findings | **Use as defensive checklist for our own code** |

---

## Ideas to adopt (ranked by value/effort for agent-takkub)

### 1. TF-IDF skill boundary audit ⭐ (highest ROI)

**What:** Compute TF-IDF vectors of every role's `.md` instruction file (`.claude/agents/<role>.md`), then flag pairs with high cosine similarity → "frontend and designer overlap 78%".

**Why valuable for agent-takkub:**
- Default 7 roles + custom roles created at runtime → growing surface
- Lead's auto-routing decisions depend on roles having distinct purposes
- When user adds custom role (`data-eng`, `ml`, `security`), Lead may pick wrong role due to fuzzy overlap

**Implementation:** ~80 lines Python using `sklearn.feature_extraction.text.TfidfVectorizer` or pure-Python TF-IDF (avoid heavy dep). Output: `runtime/skill_audit.md` listing high-overlap pairs with similarity score.

**Acceptance:**
- CLI command: `takkub audit-skills` returns table of role pairs sorted by similarity
- Warns when similarity > 0.6
- Unit tests with synthetic role docs

**Risk:** zero — read-only analysis, no system mutation.
**Effort:** Small (~2-3 hr).

---

### 2. Agent-takkub self-audit (codex's findings applied to us) ⭐⭐

**What:** Run codex's HIGH-severity OMA findings as a checklist against agent-takkub's own code:

| Codex finding on OMA | Check in agent-takkub |
|---|---|
| Spawned agents inherit full parent env | `pty_session.py` env construction — do we leak ANTHROPIC_API_KEY, AWS_*, GH_TOKEN to teammate panes? |
| Default auto-approve flags (`--yolo`) | `.claude/agents/<role>.md` and orchestrator launch flags — are teammates spawned with `--dangerously-skip-permissions`? |
| Shell-string `execSync` injection | `subprocess.run(shell=True)` in Python — grep usage |
| Broad HOME-level deletion | Any code path that touches `~/...` outside `~/.takkub/` |
| Auto-star / network side-effects | Any unexpected network calls during startup |

**Why valuable:** Free security audit, blind spots that codex identified in OMA almost certainly exist in similar cockpit tools — including ours.

**Implementation:**
- Run `grep` for each pattern, document findings in `docs/security-audit-2026-05-21.md`
- Fix anything found (likely small diffs per item)
- Add regression test for env allowlist if we don't have one

**Acceptance:**
- Audit doc lists each check + status (clean / found / fixed)
- New tests covering env-leak boundary if a leak is found
- CLAUDE.md updated with "what we deliberately do not leak" section

**Risk:** zero — defensive only.
**Effort:** Small-medium (~3-4 hr depending on findings).

---

### 3. Docs reference verifier (lite version of `oma docs verify`)

**What:** Markdown link/reference checker that scans `docs/`, `CLAUDE.md`, `README.md` for broken references:
- File paths (`src/agent_takkub/foo.py:42`)
- Function/symbol names (`Orchestrator.toggle_provider`)
- Cross-doc links (`[[../second-brain/...]]`, relative paths)

**Why valuable for agent-takkub:**
- CLAUDE.md has tons of file path + line number refs (`cli_server.py:70` etc.)
- After refactor (rename, move, delete), refs go stale silently
- Lead reads stale CLAUDE.md → wastes time / makes wrong decisions

**Implementation:**
- Pure-Python markdown parser (regex is enough — don't pull `markdown` lib)
- For each `path:line` ref → verify file + line exists
- For each `Class.method` ref → grep src for symbol
- Output: `runtime/docs_drift.md` (or print table)
- CI integration: optional GitHub Actions step

**Acceptance:**
- CLI: `takkub docs-verify` returns exit 1 if broken refs found
- Catches at least 5 known stale refs in current docs (seed cases)
- Doesn't false-positive on valid refs

**Risk:** low.
**Effort:** Medium (~4-5 hr — regex tuning is the long part).

---

### 4. `takkub verify` — deterministic pre-done gate ⭐⭐ (from antigravity plan)

**What:** New CLI subcommand teammates run before `takkub done`. Detects project type and runs the right deterministic checks:

| Project signal | Checks run |
|---|---|
| `pyproject.toml` present | `pytest` (if tests/ exists) + `ruff check` + `ruff format --check` |
| `package.json` present | `npm test` (if test script defined) + `tsc --noEmit` (if tsconfig) + `eslint` (if .eslintrc) |
| both | run both stacks |
| neither | no-op, exit 0 with "no verifier configured" |

**Why valuable for agent-takkub:**
- Lead currently writes "เขียน test + รัน pytest + ruff" in every task spec. Verbose, easy to forget.
- One command `takkub verify` standardises the gate
- Optional: `projects.json` flag `require_verify: true` per project → orchestrator rejects `takkub done` until verify exits 0
- Teammates know exactly what passes locally before reporting done — fewer "passed on my machine" surprises

**Implementation:**
- New module: `src/agent_takkub/verify.py` — `detect_stack(cwd) → list[Check]`, `run_checks(checks) → VerifyResult`
- New CLI subcommand: `takkub verify` (exits 0/1 based on aggregate result)
- Optional orchestrator gate: if `projects.json.<project>.require_verify == true`, intercept `done` and require last verify to be green (within 60s window)

**Why NOT the full antigravity "Independent Verifier Loop":**
- Antigravity proposed state-machine auto-spawning reviewer pane on every done → too invasive
- Conflicts with `5e2c588` watchdog work (already closes done-protocol gaps)
- Manual pattern (Lead asks reviewer pane after done) works and stays flexible
- Deterministic verify is simpler, additive, doesn't fight existing flow

**Acceptance:**
- `takkub verify` exits 0 when all checks pass, 1 otherwise
- Detects pytest/ruff/eslint/tsc automatically
- Unit tests cover detect_stack for: python-only, node-only, mixed, empty
- Optional `require_verify` flag in `projects.json` documented but defaults to false

**Risk:** low — additive command, no state machine, off by default.
**Effort:** Medium (~4-5 hr).

---

### 5. Workflow file structure (deferred — nice-to-have)

OMA stores explicit workflow definitions: `.agents/workflows/orchestrate.md`, `plan.md`, `brainstorm.md`, etc. agent-takkub bakes equivalent logic into Lead's CLAUDE.md as auto-routing rules.

**Pros:** clearer separation, easier to update one workflow without touching CLAUDE.md
**Cons:** adds indirection; current pattern works fine; risks Lead reading wrong file

**Stance:** Skip for now. Revisit if Lead's CLAUDE.md exceeds 500 lines and routing rules become hard to maintain.

---

### 5. Session quota caps (deferred — needs user input)

**What:** Track tokens consumed per role per session, show in UI, optionally cap.

**Why deferred:** agent-takkub uses Claude Max OAuth (cost = 0), per saved memory `project_no_api_max_oauth.md`. Adding token-cost tracking would contradict that constraint. Only revisit if user adds an API-key provider in the future.

---

## Implementation phases

### Phase 1 — Skill boundary audit (1 commit)
- New module: `src/agent_takkub/skill_audit.py`
- CLI: `takkub audit-skills` (new subcommand)
- Tests: `tests/test_skill_audit.py` (~12 tests)
- Docs: append to README.md "diagnostic commands" section

### Phase 2 — Self-audit against codex's findings (1-3 commits)
- Audit doc: `docs/security-audit-2026-05-21.md`
- One commit per fix (if any found)
- Final commit closes the audit with summary

### Phase 3 — Docs reference verifier (1 commit)
- New module: `src/agent_takkub/docs_verify.py`
- CLI: `takkub docs-verify`
- Tests: `tests/test_docs_verify.py` (~15 tests)
- Fix any drift found in current `docs/` and `CLAUDE.md`

### Phase 4 (skip / deferred)
- Workflow file structure → revisit later
- Session quota caps → only if API-key provider added

---

## Total scope

- **3 new Python modules** (`skill_audit.py`, `docs_verify.py`, audit doc — no module)
- **2 new CLI subcommands** (`takkub audit-skills`, `takkub docs-verify`)
- **~27 new tests**
- **~12-15 hr total effort**
- **Zero external dependencies added** (use stdlib + existing `pytest`/`ruff`)

## Non-goals (explicit)

- ❌ Do not install OMA as a dependency
- ❌ Do not vendor OMA TypeScript code
- ❌ Do not run `oma install/update/link` anywhere
- ❌ Do not adopt OMA's `.agents/` directory structure (we have our own)
- ❌ Do not pull in `.agents/workflows/`, `.agents/skills/`, or `.agents/rules/` payload
- ❌ Do not add token-cost tracking (Max OAuth = $0)
- ❌ Do not touch `oh-my-agent-temp/` again — read-only reference, can be deleted after this plan ships

## Rollback

Each phase is one or more atomic commits. Revert by `git revert <hash>`. No state outside the repo is touched (no HOME files, no symlinks, no installer). The plan is intentionally low-blast-radius.

---

## Decision needed from user

1. **Approve / reject this direction** (study & extract, no vendoring)?
2. **Phase ordering** — Phase 1 → 2 → 3, or different priority?
3. **Where to put audit findings** — `docs/security-audit-<date>.md` or inline in TASKS.md?
4. **Delete `oh-my-agent-temp/`** after Phase 1 ships, or keep around as reference?
