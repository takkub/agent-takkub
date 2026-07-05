# Skill Audit & Recommendation Report: mattpocock/skills

**Date:** 2026-07-05  
**Auditor Role:** `gemini analyst`

---

## 1. Executive Summary

This report performs a comprehensive inventory and analysis of the 38 skills defined in the [mattpocock/skills](https://github.com/mattpocock/skills) repository. These skills were evaluated against the existing skills in our current workspace environment across:
- **Project Repo local skills** (`.claude/skills/` in this repository)
- **Global User skills** (`~/.claude/skills/`)
- **Plugin cache skills** (`~/.claude/plugins/cache/` containing `superpowers`, `agent-skills`, and `pordee`)

### Key Statistics
- **Total Evaluated Skills:** 38
- **Unique Skills:** 22
- **Partial Overlap Skills:** 9
- **Duplicate Skills:** 7
- **Recommended for Installation:** 5

---

## 2. Inventory of Existing System Skills

The current workspace environment is already equipped with several skill directories:

### A. Plugin Cache (`~/.claude/plugins/cache/`)
#### 1. `superpowers` (14 Skills)
- `brainstorming` (model-invoked, 2649 tokens)
- `dispatching-parallel-agents` (model-invoked, 1604 tokens)
- `executing-plans` (model-invoked, 617 tokens)
- `finishing-a-development-branch` (model-invoked, 1760 tokens)
- `receiving-code-review` (model-invoked, 1569 tokens)
- `requesting-code-review` (model-invoked, 701 tokens)
- `subagent-driven-development` (model-invoked, 3131 tokens)
- `systematic-debugging` (model-invoked, 2465 tokens)
- `test-driven-development` (model-invoked, 2464 tokens)
- `using-git-worktrees` (model-invoked, 1994 tokens)
- `using-superpowers` (model-invoked, 1351 tokens)
- `verification-before-completion` (model-invoked, 1037 tokens)
- `writing-plans` (model-invoked, 1521 tokens)
- `writing-skills` (model-invoked, 5634 tokens)

#### 2. `agent-skills` (23 Skills)
- `api-and-interface-design` (model-invoked, 2564 tokens)
- `browser-testing-with-devtools` (model-invoked, 2991 tokens)
- `ci-cd-and-automation` (model-invoked, 2752 tokens)
- `code-review-and-quality` (model-invoked, 3556 tokens)
- `code-simplification` (model-invoked, 3374 tokens)
- `context-engineering` (model-invoked, 2636 tokens)
- `debugging-and-error-recovery` (model-invoked, 2560 tokens)
- `deprecation-and-migration` (model-invoked, 2243 tokens)
- `documentation-and-adrs` (model-invoked, 2178 tokens)
- `doubt-driven-development` (model-invoked, 4099 tokens)
- `frontend-ui-engineering` (model-invoked, 2651 tokens)
- `git-workflow-and-versioning` (model-invoked, 2569 tokens)
- `idea-refine` (model-invoked, 2023 tokens)
- `incremental-implementation` (model-invoked, 2173 tokens)
- `interview-me` (model-invoked, 3578 tokens)
- `performance-optimization` (model-invoked, 2853 tokens)
- `planning-and-task-breakdown` (model-invoked, 1733 tokens)
- `security-and-hardening` (model-invoked, 2793 tokens)
- `shipping-and-launch` (model-invoked, 2431 tokens)
- `source-driven-development` (model-invoked, 2034 tokens)
- `spec-driven-development` (model-invoked, 1926 tokens)
- `test-driven-development` (model-invoked, 3691 tokens)
- `using-agent-skills` (model-invoked, 2264 tokens)

#### 3. `pordee` (2 Skills)
- `pordee` (model-invoked, 983 tokens)
- `pordee-stats` (model-invoked, 149 tokens)

### B. Global User Skills (`~/.claude/skills/`)
- `graphify` (model-invoked, 11929 tokens) - knowledge graph builder
- `pms-task` (model-invoked, 2602 tokens) - hosted PMS tracker management
- `ship-to-itch` (model-invoked, 739 tokens) - itch.io store publisher

### C. Local Project Skills (`.claude/skills/` in this repo)
- `debug-mantra` (model-invoked, 1182 tokens) - four-mantra debugging process
- `management-talk` (model-invoked, 3216 tokens) - status reports formatter for execs
- `post-mortem` (model-invoked, 3351 tokens) - root-cause analysis documentation
- `scrutinize` (model-invoked, 1163 tokens) - outsider-perspective plan/PR reviews

---

## 3. Cloned Repo Skills Inventory & Matrix

Below is the complete audit list and comparative classification matrix for all 38 skills from `mattpocock/skills`.

| # | Skill Name | Function / Description | Trigger | Size (Tokens) | Matrix Status | Existing Counterpart / Overlap Context |
|---|------------|------------------------|---------|---------------|---------------|----------------------------------------|
| 1 | `ask-matt` | Ask which skill or flow fits your situation. A router over the skills in this repo. | user-invoked | 1750 | **PARTIAL** | `using-agent-skills` (agent-skills) / `using-superpowers` (superpowers) |
| 2 | `claude-handoff` | Hand the current conversation off to a fresh background agent that picks up the work immediately. | user-invoked | 320 | **UNIQUE** | None |
| 3 | `code-review` | Review the changes since a fixed point (commit, branch, tag, or merge-base) along two axes — Standards (does the code follow this repo's documented coding standards?) and Spec (does the code match what the originating issue/PRD asked for?). Runs both reviews in parallel sub-agents and reports them side by side. Use when the user wants to review a branch, a PR, work-in-progress changes, or asks to "review since X". | model-invoked | 1663 | **PARTIAL** | `code-review-and-quality` (agent-skills) / `scrutinize` (local project) |
| 4 | `codebase-design` | Shared vocabulary for designing deep modules. Use when the user wants to design or improve a module's interface, find deepening opportunities, decide where a seam goes, make code more testable or AI-navigable, or when another skill needs the deep-module vocabulary. | model-invoked | 1520 | **PARTIAL** | `api-and-interface-design` (agent-skills) |
| 5 | `design-an-interface` | Generate multiple radically different interface designs for a module using parallel sub-agents. Use when user wants to design an API, explore interface options, compare module shapes, or mentions "design it twice". | model-invoked | 841 | **UNIQUE** | None |
| 6 | `diagnosing-bugs` | Diagnosis loop for hard bugs and performance regressions. Use when the user says "diagnose"/"debug this", or reports something broken/throwing/failing/slow. | model-invoked | 2117 | **DUPLICATE** | `debug-mantra` (local project) / `debugging-and-error-recovery` (agent-skills) / `systematic-debugging` (superpowers) |
| 7 | `domain-modeling` | Build and sharpen a project's domain model. Use when the user wants to pin down domain terminology or a ubiquitous language, record an architectural decision, or when another skill needs to maintain the domain model. | model-invoked | 820 | **UNIQUE** | None |
| 8 | `edit-article` | Edit and improve articles by restructuring sections, improving clarity, and tightening prose. Use when user wants to edit, revise, or improve an article draft. | user-invoked | 188 | **UNIQUE** | None |
| 9 | `git-guardrails-claude-code` | Set up Claude Code hooks to block dangerous git commands (push, reset --hard, clean, branch -D, etc.) before they execute. Use when user wants to prevent destructive git operations, add git safety hooks, or block git push/reset in Claude Code. | model-invoked | 577 | **UNIQUE** | None |
| 10 | `grill-me` | A relentless interview to sharpen a plan or design. | user-invoked | 36 | **DUPLICATE** | `interview-me` (agent-skills) / system slash command `/grill-me` |
| 11 | `grill-with-docs` | A relentless interview to sharpen a plan or design, which also creates docs (ADR's and glossary) as we go. | user-invoked | 61 | **UNIQUE** | None |
| 12 | `grilling` | Grill the user relentlessly about a plan or design. Use when the user wants to stress-test a plan before building, or uses any 'grill' trigger phrases. | model-invoked | 185 | **DUPLICATE** | `interview-me` (agent-skills) / `brainstorming` (superpowers) |
| 13 | `handoff` | Compact the current conversation into a handoff document for another agent to pick up. | user-invoked | 219 | **UNIQUE** | None |
| 14 | `implement` | Implement a piece of work based on a PRD or set of issues. | user-invoked | 107 | **DUPLICATE** | `incremental-implementation` (agent-skills) / `executing-plans` (superpowers) |
| 15 | `improve-codebase-architecture` | Scan a codebase for deepening opportunities, present them as a visual HTML report, then grill through whichever one you pick. | user-invoked | 1345 | **UNIQUE** | None |
| 16 | `loop-me` | Grill me about specs for the workflows I want to build, within this workspace. | user-invoked | 631 | **DUPLICATE** | `interview-me` (agent-skills) |
| 17 | `migrate-to-shoehorn` | Migrate test files from `as` type assertions to @total-typescript/shoehorn. Use when user mentions shoehorn, wants to replace `as` in tests, or needs partial test data. | model-invoked | 697 | **UNIQUE** | None |
| 18 | `obsidian-vault` | Search, create, and manage notes in the Obsidian vault with wikilinks and index notes. Use when user wants to find, create, or organize notes in Obsidian. | model-invoked | 377 | **UNIQUE** | None |
| 19 | `prototype` | Build a throwaway prototype to answer a design question. Use when the user wants to sanity-check whether a state model or logic feels right, or explore what a UI should look like. | model-invoked | 745 | **UNIQUE** | None |
| 20 | `qa` | Interactive QA session where user reports bugs or issues conversationally, and the agent files GitHub issues. Explores the codebase in the background for context and domain language. Use when user wants to report bugs, do QA, file issues conversationally, or mentions "QA session". | model-invoked | 1232 | **UNIQUE** | None |
| 21 | `request-refactor-plan` | Create a detailed refactor plan with tiny commits via user interview, then file it as a GitHub issue. Use when user wants to plan a refactor, create a refactoring RFC, or break a refactor into safe incremental steps. | model-invoked | 677 | **PARTIAL** | `planning-and-task-breakdown` (agent-skills) / `writing-plans` (superpowers) |
| 22 | `research` | Investigate a question against high-trust primary sources and capture the findings as a Markdown file in the repo. Use when the user wants a topic researched, docs or API facts gathered, or reading legwork delegated to a background agent. | model-invoked | 198 | **UNIQUE** | None |
| 23 | `resolving-merge-conflicts` | Use when you need to resolve an in-progress git merge/rebase conflict. | model-invoked | 229 | **PARTIAL** | `git-workflow-and-versioning` (agent-skills) |
| 24 | `scaffold-exercises` | Create exercise directory structures with sections, problems, solutions, and explainers that pass linting. Use when user wants to scaffold exercises, create exercise stubs, or set up a new course section. | model-invoked | 897 | **UNIQUE** | None |
| 25 | `setup-matt-pocock-skills` | Configure this repo for the engineering skills — set up its issue tracker, triage label vocabulary, and domain doc layout. Run once before first use of the other engineering skills. | user-invoked | 1807 | **PARTIAL** | `context-engineering` (agent-skills) |
| 26 | `setup-pre-commit` | Set up Husky pre-commit hooks with lint-staged (Prettier), type checking, and tests in the current repo. Use when user wants to add pre-commit hooks, set up Husky, configure lint-staged, or add commit-time formatting/typechecking/testing. | model-invoked | 564 | **PARTIAL** | `ci-cd-and-automation` (agent-skills) |
| 27 | `tdd` | Test-driven development. Use when the user wants to build features or fix bugs test-first, mentions "red-green-refactor", or wants integration tests. | model-invoked | 796 | **DUPLICATE** | `test-driven-development` (agent-skills / superpowers) |
| 28 | `teach` | Teach the user a new skill or concept, within this workspace. | user-invoked | 2374 | **UNIQUE** | None |
| 29 | `to-issues` | Break a plan, spec, or PRD into independently-grabbable issues on the project issue tracker using tracer-bullet vertical slices. | user-invoked | 828 | **PARTIAL** | `planning-and-task-breakdown` (agent-skills) |
| 30 | `to-prd` | Turn the current conversation into a PRD and publish it to the project issue tracker — no interview, just synthesis of what you've already discussed. | user-invoked | 754 | **PARTIAL** | `spec-driven-development` (agent-skills) |
| 31 | `triage` | Move issues and external PRs through a state machine of triage roles — categorise, verify, grill if needed, and write agent-ready briefs. | user-invoked | 1628 | **UNIQUE** | None |
| 32 | `ubiquitous-language` | Extract a DDD-style ubiquitous language glossary from the current conversation, flagging ambiguities and proposing canonical terms. Saves to UBIQUITOUS_LANGUAGE.md. Use when user wants to define domain terms, build a glossary, harden terminology, create a ubiquitous language, or mentions "domain model" or "DDD". | user-invoked | 1220 | **UNIQUE** | None |
| 33 | `wayfinder` | Plan a huge chunk of work — more than one agent session can hold — as a shared map of investigation tickets on your issue tracker, and resolve them one at a time until the way to the destination is clear. | model-invoked | 2113 | **UNIQUE** | None |
| 34 | `wizard` | Generate an interactive bash wizard that walks a human through a manual procedure — third-party setup, a one-off migration, an A→B state transition — opening URLs, capturing values, confirming each step, and writing .env files and GitHub Actions secrets. | user-invoked | 1036 | **UNIQUE** | None |
| 35 | `writing-beats` | Writing, exploit — assemble raw material into a journey of beats, grounding each term before a beat leans on it. | user-invoked | 1216 | **UNIQUE** | None |
| 36 | `writing-fragments` | Writing, explore — mine raw fragments, no structure yet. | user-invoked | 890 | **UNIQUE** | None |
| 37 | `writing-great-skills` | Reference for writing and editing skills well — the vocabulary and principles that make a skill predictable. | user-invoked | 2245 | **DUPLICATE** | `writing-skills` (superpowers) |
| 38 | `writing-shape` | Writing, exploit — shape raw material into an article, paragraph by paragraph. | user-invoked | 1482 | **UNIQUE** | None |

---

## 4. Recommended Skills for Installation

These 5 premium skills are recommended for installation to provide unique or high-value architectural capabilities mapping specifically to the role of `gemini` (analyst/reviewer/architect):

### Recommended Package Summary

#### 1. `domain-modeling` (UNIQUE)
- **Why it is recommended:** Active discipline of building and maintaining a project's ubiquitous language (glossary in CONTEXT.md) and ADRs directly as decisions are made.
- **Installation Directory:** `~/.claude/skills/domain-modeling`
- **Assigned Teammate Role:** `reviewer, gemini (as analyst/architect)`
- **Role instruction string:**
  ```markdown
  Use the domain-modeling skill to maintain CONTEXT.md and ADRs.
  ```

#### 1. `grill-with-docs` (UNIQUE)
- **Why it is recommended:** Integrates plan stress-testing (grilling) with real-time updates to domain models and ADR documentation, creating a tight feedback loop.
- **Installation Directory:** `~/.claude/skills/grill-with-docs`
- **Assigned Teammate Role:** `gemini (as analyst/architect), reviewer`
- **Role instruction string:**
  ```markdown
  Use grill-with-docs to interview the user and update domain documents.
  ```

#### 1. `ubiquitous-language` (UNIQUE)
- **Why it is recommended:** Extracts glossary terms directly from conversation history, identifying ambiguities and saving to UBIQUITOUS_LANGUAGE.md for early alignment.
- **Installation Directory:** `~/.claude/skills/ubiquitous-language`
- **Assigned Teammate Role:** `gemini (as analyst), reviewer`
- **Role instruction string:**
  ```markdown
  Use ubiquitous-language to extract glossaries from conversation history.
  ```

#### 1. `codebase-design` (PARTIAL (High value differential))
- **Why it is recommended:** Provides a robust, shared vocabulary for designing 'deep modules' (maximizing leverage and locality at private and public seams).
- **Installation Directory:** `~/.claude/skills/codebase-design`
- **Assigned Teammate Role:** `reviewer, codex`
- **Role instruction string:**
  ```markdown
  Use codebase-design to identify seams and design deep modules.
  ```

#### 1. `design-an-interface` (UNIQUE)
- **Why it is recommended:** Leverages parallel sub-agents to design a module interface multiple different ways ('design it twice') to compare tradeoffs before coding.
- **Installation Directory:** `~/.claude/skills/design-an-interface`
- **Assigned Teammate Role:** `reviewer, codex`
- **Role instruction string:**
  ```markdown
  Use design-an-interface to explore alternative module designs using parallel agents.
  ```


---

## 5. Rejected Skills & Rationales

The remaining 33 skills are not recommended for installation due to duplication or mapping to out-of-scope work (such as blogging/creative writing, specific course structuring, or operations fully covered by existing agents):

| Skill Name | Status | Rationale for Rejection (Single-line) |
|------------|--------|----------------------------------------|
| `ask-matt` | **PARTIAL** | Duplicated/overlapped by `using-agent-skills` (agent-skills) / `using-superpowers` (superpowers). Overlaps with system meta-skills that help discover and run skills, but operates specifically as a user-invoked router command over the skills in the cloned repo. |
| `claude-handoff` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `code-review` | **PARTIAL** | Duplicated/overlapped by `code-review-and-quality` (agent-skills) / `scrutinize` (local project). Overlaps with general multi-axis code reviews, but specifically runs parallel sub-agents (Standards vs Spec) relative to a specific git baseline. |
| `diagnosing-bugs` | **DUPLICATE** | Duplicated/overlapped by `debug-mantra` (local project) / `debugging-and-error-recovery` (agent-skills) / `systematic-debugging` (superpowers). We already have a highly structured 4-step debug-mantra discipline and multiple plugin debugging skills. |
| `edit-article` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `git-guardrails-claude-code` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `grill-me` | **DUPLICATE** | Duplicated/overlapped by `interview-me` (agent-skills) / system slash command `/grill-me`. Duplicated by built-in system slash command and interview-me, which do the same relentless plan stress-testing. |
| `grilling` | **DUPLICATE** | Duplicated/overlapped by `interview-me` (agent-skills) / `brainstorming` (superpowers). Model-invoked plan stress-testing; duplicated by interview-me and brainstorming skills. |
| `handoff` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `implement` | **DUPLICATE** | Duplicated/overlapped by `incremental-implementation` (agent-skills) / `executing-plans` (superpowers). Duplicated by incremental-implementation and plan-execution skills that handle issue implementation. |
| `improve-codebase-architecture` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `loop-me` | **DUPLICATE** | Duplicated/overlapped by `interview-me` (agent-skills). User interview workflow to gather specs for workspace tasks, duplicated by interview-me. |
| `migrate-to-shoehorn` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `obsidian-vault` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `prototype` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `qa` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `request-refactor-plan` | **PARTIAL** | Duplicated/overlapped by `planning-and-task-breakdown` (agent-skills) / `writing-plans` (superpowers). Overlaps with planning, but focuses specifically on refactoring via interview and creating tiny commits/GitHub issue. |
| `research` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `resolving-merge-conflicts` | **PARTIAL** | Duplicated/overlapped by `git-workflow-and-versioning` (agent-skills). Overlaps with git versioning, but outlines a very specific step-by-step conflict resolution procedure. |
| `scaffold-exercises` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `setup-matt-pocock-skills` | **PARTIAL** | Duplicated/overlapped by `context-engineering` (agent-skills). Bootstrap skill to set up repository config specifically for the other skills in this set. |
| `setup-pre-commit` | **PARTIAL** | Duplicated/overlapped by `ci-cd-and-automation` (agent-skills). Overlaps with CI/CD automation, but focuses strictly on Husky and lint-staged hooks. |
| `tdd` | **DUPLICATE** | Duplicated/overlapped by `test-driven-development` (agent-skills / superpowers). Fully duplicated by two existing robust test-driven development skills. |
| `teach` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `to-issues` | **PARTIAL** | Duplicated/overlapped by `planning-and-task-breakdown` (agent-skills). Overlaps with task breakdown, but formats tasks specifically as vertical tracer-bullet slices/issues on a tracker. |
| `to-prd` | **PARTIAL** | Duplicated/overlapped by `spec-driven-development` (agent-skills). Overlaps with spec writing, but focuses on direct synthesis from chat history to PRD without user interview. |
| `triage` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `wayfinder` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `wizard` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `writing-beats` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `writing-fragments` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
| `writing-great-skills` | **DUPLICATE** | Duplicated/overlapped by `writing-skills` (superpowers). Duplicated by writing-skills in superpowers, which sets conventions and patterns for skill design. |
| `writing-shape` | **UNIQUE** | Out of scope for this engineering and analyst agent role (Writing, exploit — shape raw material into an article, parag...). |
