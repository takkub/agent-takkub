---
title: Codex cross-check: QA shard fan-out
date: 2026-06-03
reviewer: codex
---

# QA shard fan-out cross-check

Scope: blind spots when changing pane registry keys from `role.name` to an instance key like `qa#1`, while keeping role behavior mapped to base role `qa`.

## Summary

The `qa#n` key can work, but the implementation must keep two names explicit everywhere:

- `pane_key`: instance identity and routing key, e.g. `qa#1`. Use this for `_panes_by_project`, `_pane_state`, `_idle_state`, done files, transcript names, UI pane dictionaries, close/unregister, and direct `send --to qa#1`.
- `base_role`: behavior/config identity, e.g. `qa`. Use this for `.claude/agents/<role>.md`, provider lookup, role metadata, default cwd preference, QA Chrome env, MCP policy, model tier, and static role checks.

Any code that silently uses `qa#1` as base role will usually not crash; it will fall back to generic defaults. That is the dangerous failure mode.

## Side-effect inventory

1. `src/agent_takkub/orchestrator.py:981-982` - `register_pane()` stores `pane.role.name` as the registry key.
   - Risk: if `main_window` constructs the pane with base role `qa`, the second shard overwrites the first. If it constructs the pane with `qa#1`, behavior metadata is no longer the canonical QA role unless separately patched.
   - Fix: register by explicit pane key. Either pass `pane_key` through `paneRequested/register_pane`, or make `AgentPane` carry both `pane_key` and `role`.

2. `src/agent_takkub/main_window.py:844-871` - `_ensure_teammate_pane()` uses `role_name` for both tab dict key and `by_name(role_name)`.
   - Risk: `by_name("qa#1")` returns `None`, so QA shards become custom gray roles instead of QA. Downstream `pane.role.name` becomes `qa#1` if created as custom, which breaks role doc/provider/MCP lookups unless fixed there too.
   - Fix: split `pane_key="qa#1"` and `base_role="qa"`. Store `tab.teammate_panes[pane_key]`, but call `by_name(base_role)` and create a display role labelled like `QA#1` while preserving base behavior.

3. `src/agent_takkub/orchestrator.py:1014` and `src/agent_takkub/config.py:219` - current `validate_name(..., "role")` likely rejects `#`.
   - Risk: enabling `#` globally would also permit role-file names like `.claude/agents/qa#1.md`.
   - Fix: validate shard key with a dedicated parser: validate `base_role` using existing `validate_name`, require suffix regex `#[1-9][0-9]*`, and never feed the full key to role-file path construction.

4. `src/agent_takkub/orchestrator.py:1059` and `src/agent_takkub/config.py:97-114` - cwd boundary/default cwd use `role_name`.
   - Risk: `default_cwd_for_role("qa#1")` misses `_ROLE_PATH_PREFS["qa"]` and silently falls back to the first project path.
   - Fix: use `base_role` for `_cwd_within_project()` and `default_cwd_for_role()` decisions; use `pane_key` only for identity.

5. `src/agent_takkub/orchestrator.py:1136` and `src/agent_takkub/provider_config.py:195` - provider selection uses full role string.
   - Risk: a configured provider for `qa` is not applied to `qa#1`; forced roles like `codex#1` or `gemini#1` would not launch the intended binary.
   - Fix: call `effective_provider_for(base_role)`, while keeping `TAKKUB_ROLE=pane_key` for CLI routing.

6. `src/agent_takkub/orchestrator.py:1148`, `1188`, `1278-1286` and `src/agent_takkub/config.py:214-229` - role staging and role markdown path use full role string.
   - Risk: Claude shards look for `runtime/agents/qa#1/CLAUDE.md` sourced from `.claude/agents/qa#1.md`; no role instructions are appended, so the shard runs without QA specialist guidance.
   - Fix: `agent_role_dir(base_role)` and `.claude/agents/<base_role>.md`; transcript/session identity can still use `pane_key`.

7. `src/agent_takkub/orchestrator.py:1323-1325`, `1088`, `1151`, `1195` - `TAKKUB_ROLE` is set from `role_name`.
   - Risk: this is correct only if `role_name` is the pane key. If implementation changes `role_name` to base role internally, every shard calls `takkub done` as `qa`, collapsing done/routing.
   - Fix: explicitly set `TAKKUB_ROLE=pane_key`, plus add `TAKKUB_BASE_ROLE=qa`, `TAKKUB_SHARD=1`, `TAKKUB_SHARD_TOTAL=N`.

8. `src/agent_takkub/orchestrator.py:1362` - QA Chrome `CHROME_BIN` auto-detection checks `role_name == "qa"`.
   - Risk: `qa#1` misses `CHROME_BIN`, so `mb-start-chrome` may fail only in shards.
   - Fix: check `base_role == "qa"`.

9. `src/agent_takkub/orchestrator.py:1416-1418` and `_teammate_tier()` at `src/agent_takkub/orchestrator.py:293-299` - model tier lookup uses full role string.
   - Risk: `qa#1` loses QA/reviewer-specific tier settings and falls back to default.
   - Fix: `_teammate_tier(base_role)`.

10. `src/agent_takkub/orchestrator.py:1459` and `src/agent_takkub/shared_dev_tools.py:151-172` - default plugins and role-filtered MCP config use full role string.
    - Risk: `qa#1` does not match the QA MCP policy and may fall back to the master MCP config or miss browser-specific config, depending on variant availability.
    - Fix: use `base_role` for `_default_plugin_dirs()` and `shared_mcp_config_path_for_role()`.

11. `src/agent_takkub/orchestrator.py:1519-1532`, `1554-1571`, `1708-1767`, `3466-3524` - recent-exit, transcript, session exit, and auto-respawn keys use `role_name`.
    - Risk: this should remain `pane_key`; stripping here would make one shard's crash/respawn state affect another shard.
    - Fix: do not split for instance lifecycle state. Add tests that `qa#1` and `qa#2` have independent `_recent_exits`, transcripts, respawn attempts, and `_pane_state`.

12. `src/agent_takkub/orchestrator.py:1812-1854` - `assign()` accepts one role and writes state/sends task to that same key.
    - Risk: adding `--shards N` in CLI only is not enough unless the server/orchestrator receives per-shard pane keys and per-shard env/task metadata. Also, `requires_commit` and `auto_chain` are currently per pane key, not per logical fan-out group.
    - Fix: have `assign(..., shards=N)` create `qa#1..qa#N`, stamp group id and shard env, and track group aggregate separately from per-pane state.

13. `src/agent_takkub/cli.py:142-156` and parser at `src/agent_takkub/cli.py:569-588` - CLI has no `--shards`, and `cmd_assign()` sends one `role`.
    - Risk: Lead cannot express fan-out through the intended surface. If implemented as a loop in CLI, aggregate state in the orchestrator will be blind unless a group id is included.
    - Fix: add `--shards N` and send it to `cli_server`; prefer server/orchestrator expansion so all shards share one aggregate record.

14. `src/agent_takkub/cli.py:172-173`, `src/agent_takkub/cli_server.py:189-192`, and `src/agent_takkub/orchestrator.py:2400-2413` - done is keyed by the caller's `TAKKUB_ROLE`.
    - Risk: if shard panes receive `TAKKUB_ROLE=qa`, all shard done events target a single `qa` pane and aggregation cannot distinguish shards.
    - Fix: keep `TAKKUB_ROLE=pane_key`. Aggregate by `group_id`, not by base role.

15. `src/agent_takkub/orchestrator.py:2496-2503` - auto-chain fires when no pending `s.auto_chain` remains.
    - Risk: this currently works as "wait until all panes tagged auto-chain are done", but it has no timeout and no concept of fan-out partial completion. A hung shard means no handoff forever.
    - Fix: add aggregate deadline, e.g. `assign_group.deadline = now + timeout`; on timeout, inject one Lead handoff summarizing done/missing shards and mark the group closed or degraded.

16. `src/agent_takkub/orchestrator.py:2507` and `src/agent_takkub/orchestrator.py:2263-2305` - done schedules close by role key.
    - Risk: this should remain `pane_key`. If close strips to base role, the first done shard could close the wrong pane or fail to unregister the suffixed pane.
    - Fix: never strip for `close()`, `unregister_pane()`, tab pane removal, or registry pop.

17. `src/agent_takkub/orchestrator.py:2819-2827` - status only treats exact roles `qa`, `critic`, `designer` as screenshot roles.
    - Risk: `takkub status` for `qa#1` will not show latest screenshots.
    - Fix: check `base_role in ("qa", "critic", "designer")`.

18. `src/agent_takkub/orchestrator.py:2840-2847` - status done-event matching uses `f.name.startswith(f"{role}-")`.
    - Risk: filenames like `2026...-qa#1.md` or `qa#1-...` need consistent naming; current prefix check may fail depending on `_save_decision_note()` filename format.
    - Fix: standardize done note filenames on `pane_key` and make status match that exact pane key. Do not aggregate by base role in this view.

19. `src/agent_takkub/main_window.py:2113-2119` - Claude process count uses `effective_provider_for(role)` on registry keys.
    - Risk: `qa#1` may be counted under the wrong provider, affecting update/restart decisions for live Claude panes.
    - Fix: use `effective_provider_for(base_role)`.

20. `src/agent_takkub/agent_pane.py:127`, `170`, `186`, `543-570`, `584` - `AgentPane` uses `role.name` for object name, signals, font setting key, export filename, and stylesheet selector.
    - Risk: if `role.name` remains base role, UI signals collide and exports/settings collide. If `role.name` becomes `qa#1`, CSS selector `#pane_qa#1` is invalid/ambiguous because `#` starts an id selector fragment.
    - Fix: add `pane_key` to `AgentPane`; use escaped/sanitized key for Qt object/CSS, pane key for signals/export identity, and base role only for display color/label.

21. `src/agent_takkub/orchestrator.py:3797-3804` - internal pane signal handlers route by `role_name`.
    - Risk: these signals must carry `pane_key`; carrying base role causes spawn/close/input to hit the wrong pane.
    - Fix: wire `AgentPane.spawnRequested/closeRequested/inputBytes` to emit pane key, not base role.

## Q3: dynamic Chrome port and `.chrome_port`

Gemini's `--port 0` suggestion is better than `9222 + shard`, but writing one `.chrome_port` per project cwd is racy when multiple QA shards share the same working directory:

- `qa#1` starts Chrome A and writes `.chrome_port=43121`.
- `qa#2` starts Chrome B and overwrites `.chrome_port=43155`.
- `qa#1` later runs `mb` and accidentally connects to Chrome B.

This is a silent cross-shard browser collision, exactly the class of bug fan-out is trying to avoid.

Recommended fix:

- Use shard-specific port files, e.g. `.chrome_port.qa1` or better `.takkub/chrome/qa-1.port`.
- Export `CHROME_PORT_FILE` or `MB_CHROME_PORT_FILE` into each pane.
- Make `mb-start-chrome --port 0 --port-file "$CHROME_PORT_FILE"` write atomically: write to temp file, then rename.
- Make every later `mb` command read the same env var, not the default `.chrome_port`.
- Also isolate Chrome user data dir per shard, e.g. `.takkub/chrome/qa-1-profile`, because debug-port isolation alone does not prevent profile lock/cookie/cache interference.

If patching `mb` is not feasible, the task prompt must force every shard command to pass the explicit port returned by its own starter; relying on a shared cwd file is not safe.

## Q4: aggregate completion timeout

Waiting for all N `done` events without a deadline can deadlock handoff:

- a shard can crash before calling `done`;
- a shard can sit idle at a prompt after finishing but forget `done`;
- a shard can be rate-limited or stuck in a browser command;
- Lead may never receive the consolidated result because the aggregate waits forever.

The existing idle reminder helps humans notice forgotten `done`, but it does not satisfy aggregate semantics. Auto-chain currently waits until no remaining `auto_chain` pane state exists (`src/agent_takkub/orchestrator.py:2496-2503`); that is an unbounded wait.

Recommended fix:

- Introduce an explicit fan-out group record: `group_id`, `base_role`, `expected_keys`, `done_by_key`, `failed_by_key`, `created_at`, `deadline`.
- On each shard `done`, update the group and inject the consolidated handoff only when complete.
- Add a timer check for expired groups. On timeout, inject one Lead handoff with `complete: false`, list done shards, missing shards, last known state/stall minutes, and transcript paths.
- Choose a default timeout long enough for smoke work, e.g. 30-45 minutes, plus `--shard-timeout` for Lead override.
- After timeout, mark the group closed/degraded so a late shard does not trigger a second full handoff; late done can be appended as a follow-up notice.

## Test checklist

- Spawn `qa#1` and `qa#2`; both panes appear and neither overwrites the other.
- Both shards load QA role instructions from `.claude/agents/qa.md`.
- `TAKKUB_ROLE` differs per shard while `TAKKUB_BASE_ROLE=qa`.
- `CHROME_BIN`, browser MCP config, and QA model/provider policy apply to shards.
- `takkub send --to qa#1`, `takkub done` from `qa#1`, close/unregister, and idle reminder all target only `qa#1`.
- `takkub status` shows screenshots and done events per shard.
- One shard done plus one hung shard produces a timeout handoff, not an infinite wait.
