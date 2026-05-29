# System Review - agent-takkub cockpit

Date: 2026-05-29  
Reviewer: Codex

## 1. Correctness

### High - `--requires-commit` can deadlock teammate completion

`takkub assign --requires-commit` stores a per-pane gate in `Orchestrator.assign()` and `Orchestrator.done()` rejects completion while `git status --porcelain` is dirty (`src/agent_takkub/orchestrator.py:1440`, `src/agent_takkub/orchestrator.py:1461`, `src/agent_takkub/orchestrator.py:1880`). The rejection text tells the agent to "commit before calling takkub done" (`src/agent_takkub/orchestrator.py:1907`).

That conflicts with the planted Codex/Gemini instructions, which explicitly forbid agents from running `git commit` and say Lead owns commits (`src/agent_takkub/codex_agents_md.py`, `src/agent_takkub/gemini_md.py`). A teammate given `--requires-commit` and a dirty tree cannot comply with both rules: it cannot call `done`, and it must not commit. This can leave panes permanently active until Lead harvests or force-closes them.

Suggested fix: rename/rework this gate to `--requires-clean-tree` only for tasks that truly should not edit files, or change the completion path to notify Lead with "changes pending, Lead must commit" instead of rejecting `done`. If the intent is "must save work", the current version-control policy already says `done` is the save signal.

### Medium - Done notices are not durable despite the documented durability path

Peer CC messages are persisted via `_save_pending_cc()` / `_load_pending_cc()` (`src/agent_takkub/orchestrator.py:1583`, `src/agent_takkub/orchestrator.py:1595`). Done notices are only held in memory (`src/agent_takkub/orchestrator.py:627`) and queued when Lead is absent (`src/agent_takkub/orchestrator.py:1938`), then flushed only if the same process later spawns Lead (`src/agent_takkub/orchestrator.py:1671`).

If a teammate calls `takkub done` while Lead is down and the cockpit process exits before Lead respawns, the notice is lost. This is especially risky because `done()` also clears the task bookkeeping and schedules pane close, so the original pane state may no longer be visible.

Suggested fix: mirror the CC implementation with `pending-done-notices-<project>.json`, load it on startup, and delete it only after successful flush.

### Medium - Local issue fallback can report success even when persistence failed

`_save_local_issues()` catches all exceptions and returns `None` (`src/agent_takkub/issues.py:132`). `new_issue()` appends an issue, calls `_save_local_issues()`, then returns a `local://issue/<n>` URL regardless of whether the file was written (`src/agent_takkub/issues.py:214`). `close_issue()` has the same pattern when saving local close state (`src/agent_takkub/issues.py:378`).

On a read-only project directory, disk-full condition, or permission error, `takkub issue new` can print `ok: created #N` for an issue that was never stored anywhere.

Suggested fix: make `_save_local_issues()` raise or return a boolean and surface a CLI error if the fallback cannot persist.

### Medium - `gh` issue operations have no timeout

The issue backend wraps `gh` with `subprocess.run()` but does not set a timeout (`src/agent_takkub/issues.py:43`). A stalled GitHub auth prompt, network hang, credential helper, or `gh` bug can block `takkub issue new/list/show/close` indefinitely inside an agent pane.

This violates the cockpit rule that panes should avoid long-running foreground commands and is user-visible because `takkub issue` is part of the recommended bug-check flow.

Suggested fix: add a bounded timeout, for example 20-30s for repo detection/list/view and 60s for create/close, and return a clear fallback/error path.

## 2. Architecture

### Orchestrator has become a broad lifecycle + policy + reporting god object

`orchestrator.py` owns pane spawning, provider-specific argv, env construction calls, routing delivery, pending queues, done persistence, vault mirroring, hot.md, daily digest, harvest, stall detection, auto-respawn, and broadcast workflows. Even with helper modules, the core file is large and state-heavy. The bug above where CC durability and done durability drifted is a symptom: two very similar queue patterns live separately and one lacks persistence.

Suggested extraction targets:

- `PendingQueueStore`: reusable durable queue for CC, done notices, auto-chain handoffs.
- `ProviderSpawner`: Claude/Codex/Gemini argv/env/planted-file handling.
- `SessionArtifacts`: transcript path, decision notes, hot.md/daily digest, harvest scan.
- `PaneWatchdog`: idle, stuck, stall, auto-respawn policy.

This would reduce coupling between unrelated behavior and make future review easier.

### CLI and orchestrator duplicate role-gate policy

The CLI blocks Lead-only and teammate-only commands (`src/agent_takkub/cli.py:23`, `src/agent_takkub/cli.py:70`), and the server repeats similar checks with token verification (`src/agent_takkub/cli_server.py:25`, `src/agent_takkub/cli_server.py:89`). Defense in depth is good, but the command sets can drift.

Suggested fix: move command policy names into a shared module used by both client and server, while keeping server-side token enforcement as the authoritative security layer.

## 3. Security

### High - Lead runs with full inherited environment and bypassed permissions

Teammate panes use `_build_pane_env()` allowlisting (`src/agent_takkub/pane_env.py:78`), but Lead uses `os.environ.copy()` (`src/agent_takkub/orchestrator.py:1017`) and every Claude pane runs with `--dangerously-skip-permissions` (`src/agent_takkub/orchestrator.py:1087`). Lead also injects the capability token into that environment (`src/agent_takkub/orchestrator.py:1030`).

This means any secret in the cockpit process environment is available to Lead's tools and potentially to project hooks/MCPs loaded for the Lead session. The tests intentionally preserve this behavior, but it is still the largest remaining env-injection risk.

Suggested fix: treat Lead as privileged but still use an allowlist plus explicit opt-in extras (`TAKKUB_LEAD_ENV_ALLOW=...`). At minimum, document that launching cockpit from a shell with cloud/API tokens exposes those tokens to Lead.

### Medium - Transcript/status surfaces can leak secrets

Every PTY byte stream is written to `runtime/sessions/<date>/<project>/<role>-<time>.transcript.log` (`src/agent_takkub/orchestrator.py:523`, `src/agent_takkub/pty_session.py:161`). `takkub status` reads the transcript file and returns the last non-empty lines (`src/agent_takkub/orchestrator.py:2227`), and post-compact briefs can inject transcript tails back into Lead (`src/agent_takkub/orchestrator.py:2392`).

If a pane prints tokens, `.env` contents, OAuth URLs, stack traces with headers, or command output containing credentials, those values become durable local artifacts and can be re-injected into another agent context.

Suggested fix: add a lightweight redaction pass before status/brief injection and consider a runtime setting to disable transcript persistence for sensitive projects.

### Medium - User MCP allowlist includes a likely secret-bearing database entry by name

`ensure_user_mcps()` defaults to copying `postgres-pms` because it is in `_USER_MCP_DEFAULT_ALLOW` (`src/agent_takkub/shared_dev_tools.py:202`). The secret detector is bypassed for allowlisted names; it only blocks non-allowlisted entries with secret-looking env/header keys (`src/agent_takkub/shared_dev_tools.py:333`).

If `postgres-pms` contains a DSN, password, or token under a non-obvious key, cockpit will write it into `runtime/shared-mcp.json` and role variants. The code is careful about HTTP bearer `pms`, but database MCPs are also sensitive.

Suggested fix: run `_has_secrets()` for allowlisted entries too and either redact/skip on match or require a specific opt-in like `TAKKUB_INCLUDE_POSTGRES_PMS=1`.

## 4. Maintainability

### Planted Codex and Gemini instructions are duplicated

`codex_agents_md.py` and `gemini_md.py` maintain near-identical long markdown strings with small differences. The current duplication already misses one Codex-specific override section in Gemini: Codex has the inline `[ROLE: ...]` override clarification; Gemini does not. This may be intentional, but the bodies are similar enough that future protocol changes will likely drift.

Suggested fix: render both files from a shared template with provider-specific name, filename, marker, and extra sections.

### Comments/tests mention old Lead guard behavior

Several comments and tests describe removed or bypassed Lead write guards while the current code intentionally uses `--dangerously-skip-permissions` and soft policy only (`src/agent_takkub/orchestrator.py:1080`, `src/agent_takkub/lead_context.py:15`, `tests/test_lead_write_guard.py:5`). This makes security posture harder to reason about because historical intent and current behavior are mixed.

Suggested fix: update docs/tests comments to state the current posture plainly: Lead is trusted, has broad env, and write boundaries are prompt policy rather than enforcement.

## 5. Blind Spot

### Provider-disable state is only a routing hint, not an execution gate

`routing_planner.classify()` respects `disabled_providers` for proposals and one-shot routing, and `toggle_provider()` broadcasts status (`src/agent_takkub/routing_planner.py:284`, `src/agent_takkub/orchestrator.py:1822`). But `Orchestrator.spawn()` / `assign()` still spawn Codex or Gemini directly when the role/provider mapping resolves that way (`src/agent_takkub/orchestrator.py:861`, `src/agent_takkub/orchestrator.py:901`).

If the user disables Gemini because auth is broken or because it should not be used for a session, Lead can still explicitly run `takkub assign --role gemini ...`, and a role mapped to Gemini can still spawn. This is not a security bypass by itself, but it is a product semantics gap: "disabled" sounds like a hard capability toggle.

Suggested fix: decide and document semantics. If disabled means "do not propose automatically", rename UI text to "hide from routing". If disabled means "unavailable", enforce it in `spawn()` and `assign()` with a Lead-visible error.

### Harvest and stall detection trust file mtimes as progress

Stall detection treats transcript mtime, screenshot directory mtime, and last send as progress (`src/agent_takkub/orchestrator.py:2132`). Harvest scans broad configured project paths by mtime (`src/agent_takkub/orchestrator.py:2287`). These are pragmatic, but noisy processes can keep a pane looking alive without meaningful progress, and generated files from unrelated tools can be harvested as a teammate's work.

Suggested improvement: when possible, include explicit pane-origin markers: transcript growth with agent prompt state, artifact paths written by known QA/export helpers, or a small per-pane heartbeat/progress event rather than raw project-wide mtime.

