# Second-opinion critique: Gemini provider-feature ideas

**Date:** 2026-07-10  
**Reviewer:** Codex  
**Scope:** 12 ideas in `2026-07-10-ideas-gemini-provider-features.md`, checked against the current repository, official provider documentation, and locally installed CLI help (`Claude Code 2.1.206`, `codex-cli 0.144.1`).

## Executive verdict

The strongest direction is **native hook adapters** (#2), followed by a narrowly scoped **Claude diagnostic safe mode** (#9). Most other proposals either duplicate capabilities Takkub already has, mistake conversation rollback for workspace rollback, or call PTY text injection “provider-agnostic” even though it is the least stable integration surface in this repository.

The main #103 finding is more fundamental: [`provider_spec.py`](../../src/agent_takkub/provider_spec.py) already understates current capabilities. It marks Codex and AGY `supports_slash_commands=False`, `supports_resume=False`, and `supports_hooks=False`, while current official docs expose Codex slash commands/resume/fork/hooks and AGY `/resume`, `/fork`, `/hooks`, plus CLI resume flags. A static boolean registry needs version-aware probing or it will keep becoming stale.

### Evidence baseline

- Claude's `--fallback-model` is real. The official changelog says v2.1.166 added up to three ordered fallbacks and interactive-session support, although the current CLI reference and local `--help` still say “print only”; this documentation inconsistency means Takkub should smoke-test behavior instead of assuming the help string is authoritative. Takkub already passes a single fallback in [`spawn_engine.py`](../../src/agent_takkub/spawn_engine.py). ([Claude CLI reference](https://code.claude.com/docs/en/cli-usage), [official changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md))
- Claude `/cd`, `/doctor`, `/rewind`, and `--safe-mode` are real, but they do different things from several claims below. `/doctor` diagnoses installation/configuration; it is not a prompt-rules optimizer. `/rewind` can restore Claude checkpoints; AGY `/rewind` is documented as conversation-history rollback only. ([Claude commands](https://code.claude.com/docs/en/commands), [AGY CLI reference](https://antigravity.google/docs/cli-reference))
- `additionalContext` is no longer Claude-only. Claude and Codex both document it for lifecycle hooks. AGY has native hooks too, but with a different schema (`injectSteps` around invocation; `PostToolUse` itself returns `{}`). Therefore a provider adapter is viable; raw PTY stdin simulation is unnecessary and unsafe. ([Claude hooks](https://code.claude.com/docs/en/hooks), [Codex hooks](https://learn.chatgpt.com/docs/hooks), [AGY hooks](https://www.antigravity.google/docs/hooks))
- Codex is already an interactive TUI with slash commands, approvals, resume/fork, keymaps, and sandbox settings. Takkub's xterm bridge already forwards xterm-generated key escape sequences directly to the PTY, and teammate panes already have an unlock button. ([Codex developer commands](https://developers.openai.com/codex/cli/reference), [`terminal_widget.py`](../../src/agent_takkub/terminal_widget.py), [`agent_pane.py`](../../src/agent_takkub/agent_pane.py))

## Idea-by-idea review

| Idea # | Verdict | Solo-dev effort → value | Feature truth, #103 gap, and specific reason |
|---:|:---:|:---:|---|
| 1 | **maybe** | Low–Medium → Medium | **Feature is real but mostly already implemented.** Takkub already passes one Claude `--fallback-model` for Lead and teammates. The current Claude changelog supports an ordered chain of up to three; extending the existing setting to a list is reasonable only if a single fallback is observably insufficient. **#103 gap:** not provider-agnostic. Codex and AGY do not expose an equivalent documented fallback chain. “Catch rate-limit exit codes and restart” is unreliable because interactive CLIs often remain alive and render a rate-limit state instead of exiting; restarting also loses non-Claude conversation state under today's registry. Do not implement a generic exit-code fallback engine. **Condition:** Claude-only chain, bounded to 2–3 validated model IDs, with a smoke test against the installed version. |
| 2 | **yes, re-scope** | Medium → High | **The core feature is valid, but the proposed portability mechanism is outdated.** Claude, Codex, and AGY now all have native lifecycle hooks. Claude and Codex support model-visible `additionalContext`; AGY supports invocation-step injection with a different contract. Build one cockpit diagnostic producer plus three small output adapters. **Do not inject into PTY stdin:** it can collide with a user's partially typed prompt, queue behind an active turn, or trigger a slash command. Start with only failed test/lint commands, dedupe by hash, cap text/lines, and pass a file pointer for large output. **#103:** capability is multi-provider, implementation is explicitly provider-specific schema adaptation—not “simulate the same hook through stdin.” |
| 3 | **no / cut** | High → Low | Takkub already has hop pipelines, auto-chain, shard fan-out, spawn staggering, and capacity queues in [`pipeline_executor.py`](../../src/agent_takkub/pipeline_executor.py). A second Python “OpenAI SDK style” orchestrator for dozens of agents duplicates that control plane, increases loop/deadlock states, and is aimed at enterprise-scale refactors rather than a solo cockpit. **#103 gap:** not fully provider-agnostic: readiness, crash recovery, resume, hooks, rate limits, task completion, and model/session identity still differ by provider. Keep the declarative pipeline executor and improve its provider adapters instead. |
| 4 | **maybe, redesign** | Medium → Medium | Persistent goals are useful, but writing transient task boundaries into `CLAUDE.md`/`AGENTS.md`/`GEMINI.md` is the wrong lifetime and can dirty a user's repo or leak one task's restriction into later sessions. Codex now has native `/goal`; the others do not expose an identical durable-goal contract. **#103 gap:** file discovery and precedence differ; `context_strategy` only describes today's injection mechanism, not semantic equivalence. **Condition:** store a cockpit goal under `runtime/`, attach it to every new assignment/done handoff, and inject provider-specifically at session/turn boundaries. Never mutate project rules files for an ephemeral goal. |
| 5 | **no / cut** | Medium–High → Low | Claude `/cd` is real (v2.1.169+) and preserves its prompt cache. Codex documents `--cd` at startup, not a matching mid-session `/cd`; AGY offers `/add-dir` and workspace-scoped conversation history. Restarting non-Claude panes is not transparent because Takkub currently declares them non-resumable even though their CLIs now support resume. The claimed token saving is niche for a cockpit whose panes are intentionally rooted to a project/worktree; agents can already run `cd subdir && command`. **#103:** strongly Claude-bound. Prefer cross-provider `--add-dir` support (new idea A3) and fix resume adapters first. |
| 6 | **no / cut** | Medium → Low | **Claim conflates two features.** Claude `/doctor` checks installation/runtime/configuration health; it does not identify “instructions already implicit” or optimize prompt rules. A heuristic that deletes or recommends deleting supposedly redundant instructions is subjective and can remove the exact project-specific constraint that prevents mistakes. **#103 gap:** scanning filenames is provider-neutral, but rule loading, precedence, nested discovery, and implicit defaults are not. A factual rule-size/duplication report could be safe, but automated “implicit instruction” judgment is not worth maintaining for a solo developer. |
| 7 | **maybe, after resume adapters** | Medium–High → Medium | Native conversation forks now exist in all three ecosystems: Claude `/branch`/`--fork-session`, Codex `/fork`/`codex fork`, and AGY `/fork`. But the proposed git-worktree spawn only forks files, not the model conversation. Conversely, AGY explicitly warns that `/fork` clones the thread, not the git checkout. **#103 gap:** “fully provider-agnostic” is false; conversation IDs, fork commands, session storage, and workspace cloning must be adapted per provider. **Condition:** first implement reliable provider-native resume/fork IDs; then compose native conversation fork + Takkub worktree isolation as one feature. For a solo dev, cap this at two branches and provide a cleanup view. ([AGY conversation management](https://antigravity.google/docs/cli-conversations)) |
| 8 | **no / cut** | Medium → Very Low | Agents already receive their own command failures and normally explain/fix them. PTY output does not expose a trustworthy cross-provider “command exited non-zero” event; regexes can confuse quoted logs, old scrollback, or a nested tool. Automatic follow-up text can interrupt users and create loops. **#103:** not provider-agnostic despite sharing stdout/stderr. If extra correction is needed, implement it through native `PostToolUse` hooks under #2, where Codex even documents non-zero Bash results explicitly. |
| 9 | **yes, narrow** | Low → Medium | `claude --safe-mode` is real on the installed 2.1.206 and disables custom rules, skills, plugins, hooks, MCP, commands, agents, themes, and keybindings while keeping auth/model/built-ins/permissions. That is useful for recovering a broken Claude setup. But a global “safe teammate” toggle would also disable cockpit role/context and completion hooks, so offer **Open diagnostic Claude pane in safe mode**, visibly labeled, rather than silently applying it to normal work. **#103 gap:** Claude-only. Codex/AGY temporary configs are not equivalent and must not be marketed as safe mode without documented vendor support. |
| 10 | **maybe, redesign** | High → Medium | A permission UI can be valuable only after Takkub offers non-bypass execution profiles; today Windows Codex uses `--dangerously-bypass-approvals-and-sandbox`, while Claude/AGY are also spawned with skip-permission flags. Regex-parsing TUI prose is too fragile for a security boundary. **#103:** all providers now offer structured native interception, but via different surfaces: Claude hooks/permission handling, Codex `PermissionRequest`, AGY `PreToolUse` decisions. **Condition:** first add provider-native safe/approval profiles, then bridge structured hook events to the GUI. Never approve based solely on matched PTY text. |
| 11 | **no / cut** | Low apparent, High risk → Low | AGY `/rewind` is real, but official docs define it as rolling back **conversation history**, not restoring the working tree. Therefore “Workspace Recovery” is a false claim. A fallback using `git stash`/`checkout` is not equivalent and can discard or hide unrelated user changes, especially in a shared dirty worktree. **#103:** Claude checkpoint rewind may include code; AGY's documented rewind does not; Codex has no documented `/undo` in the current command reference. Do not put a universal Rewind button over incompatible semantics. |
| 12 | **no / cut as already done** | Low remaining → Near-zero | Codex's interactive Rust TUI is real, but Takkub already launches Codex interactively. [`terminal_widget.py`](../../src/agent_takkub/terminal_widget.py) forwards xterm key sequences directly to the PTY, and [`agent_pane.py`](../../src/agent_takkub/agent_pane.py) provides an input-lock toggle so the operator can interact. The proposed “disable input filters” is therefore based on a false gap. **#103:** current `supports_slash_commands=False` for Codex is stale, but raw key passthrough is already provider-neutral. The remaining task is tests/capability metadata, not a roadmap feature. |

## Replacement ideas for cut items

### A1. Provider capability drift doctor

**Verdict:** yes · **Effort:** Low–Medium · **Value:** High

Add `takkub doctor providers` that records each binary version, runs safe help/subcommand probes, and compares observed capabilities with `ProviderSpec`: resume/fork, hooks, slash commands, add-dir, sandbox/approval, safe mode, structured output, and MCP. Show mismatches as warnings, never auto-edit config.

This is concrete and already justified by this review: the registry says Codex/AGY have no hooks/resume/slash commands while current CLIs do. It also catches documentation/help drift such as Claude's changelog saying interactive fallback while `--help` still says print-only. This directly advances #103 and prevents feature work from being designed on stale booleans.

### A2. Native resume/fork adapters and crash recovery

**Verdict:** yes · **Effort:** Medium · **Value:** High

Promote session identity to a provider adapter:

- Claude: existing `--session-id`, `--resume`, `--fork-session`.
- Codex: `codex resume <id>` / `codex fork <id>` and interactive `/resume`/`/fork`.
- AGY: `--conversation <id>`, `--continue`, `/resume`, `/fork`.

Persist only the provider, conversation ID, cwd/worktree, and last assignment in cockpit runtime state. On a pane crash, resume natively where supported; replay the assignment only when no resumable ID exists. This gives the solo developer more value than adding new orchestration layers and is a prerequisite for ideas #5 and #7. ([Codex developer commands](https://developers.openai.com/codex/cli/reference), [AGY conversation management](https://antigravity.google/docs/cli-conversations))

### A3. Cross-provider additional workspace roots (`--add-dir`)

**Verdict:** yes · **Effort:** Low–Medium · **Value:** Medium–High

All three installed CLIs expose `--add-dir`; Claude and AGY also document `/add-dir`. Add `add_dir_flag` plus repeatability/path-validation semantics to `ProviderSpec`, then let a pane opt into adjacent shared libraries without changing its primary cwd or restarting its identity.

This solves the practical monorepo/multi-repo case behind idea #5 with a smaller and genuinely portable surface. Restrict paths to operator-selected directories, display them in the pane header, and preserve them across crash resume.

### A4. Per-pane autonomy profile instead of universal bypass

**Verdict:** yes · **Effort:** Medium · **Value:** High

Replace the single implicit “dangerous” startup shape with three explicit cockpit profiles: `fast/bypass`, `workspace-safe`, and `ask`. Map them natively:

- Claude: `--permission-mode` / `--dangerously-skip-permissions`.
- Codex: `--sandbox` plus `--ask-for-approval`.
- AGY: `--sandbox`, execution `--mode`, or `--dangerously-skip-permissions` as documented by the installed CLI.

Show the active profile in every pane. This is a better prerequisite to a permission dialog than parsing TUI prompts, and it gives a solo developer a clear choice between speed and containment per task.

### A5. Structured one-shot execution channel for reviews and diagnostics

**Verdict:** maybe · **Effort:** Medium · **Value:** Medium

For bounded review/diagnostic jobs, use structured modes where the provider actually supports them: Claude `-p --output-format stream-json` and Codex `exec --json` (newline-delimited events). Keep AGY on its documented print contract unless/until it exposes a stable structured format. Normalize only a small event set: assistant text, command/tool result, failure, completion, and usage when available.

This reduces PTY scraping for automatable one-shot tasks without replacing interactive panes. It must remain capability-gated; forcing AGY's text output into a fake shared schema would recreate the #103 problem.

## Recommended priority

1. **A1 capability drift doctor** — low cost and prevents more incorrect roadmap assumptions.
2. **A2 resume/fork adapters** — closes the largest current provider gap and improves crash recovery.
3. **#2 native diagnostic hooks** — implement one bounded failure-diagnostics use case across native schemas.
4. **A4 autonomy profiles** — security/UX foundation before any permission interception UI.
5. **A3 additional roots** — small portable win; then reconsider whether `/cd` is still needed.
6. **#9 Claude diagnostic safe mode** — easy recovery feature, explicitly vendor-specific.

Defer #7 and #10 until their prerequisites exist. Cut #3, #5, #6, #8, #11, and #12 from the roadmap as currently framed.
