# Codex Review: Recent Commits

Reviewed commits: `a6e2603`, `86fe144`, `ca06e2b`, `6112984`, `ab1ff5f`

Verification run:

```powershell
pytest tests/test_orchestrator_env_allowlist.py tests/test_orchestrator_auto_respawn_replay.py -q
```

Result: `14 passed`

## a6e2603

### Findings

no findings

## 86fe144

### Findings

#### med: `done()` leaves the replay cache live until delayed close, so a post-done crash can replay an already-finished task

`src/agent_takkub/orchestrator.py:1794` and `src/agent_takkub/orchestrator.py:1735`

`done()` marks the pane as done and schedules `close()` 2.5 seconds later, but `_last_assigned_task` is only cleared inside `close()`. If the agent process exits unexpectedly in that 2.5 second window, `_on_session_exit()` treats it as an unexpected exit, schedules auto-respawn, and `_auto_respawn()` replays the cached task. That can restart work after the teammate already reported completion.

Suggested fix: clear `self._last_assigned_task.pop(key, None)` in `done()` at the same point where `_idle_state`, `_blocked_on_lead`, and `_auto_respawn_attempts` are cleared. Keep the existing clear in `close()` as a defensive cleanup. Add a regression test for `assign -> done -> unexpected exit before delayed close -> _auto_respawn` asserting `_send_when_ready` is not called.

## ca06e2b

### Findings

no findings

## 6112984

### Findings

#### high: audit doc marks env allowlist as fully fixed even though Claude teammate panes still inherit the full cockpit environment

`docs/security-audit-2026-05-21.md:33` and `src/agent_takkub/orchestrator.py:1120`

The doc says all three spawn paths were changed from `os.environ.copy()` to `_build_pane_env()`, but the main Claude spawn path still does `env = os.environ.copy()`. That path is used for non-lead Claude teammates, so `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GH_TOKEN`, `AWS_*`, and other cockpit env secrets still leak into the most common teammate provider. The security audit status `5/5 clean` is therefore incorrect.

Suggested fix: update the Claude spawn path to use `_build_pane_env()` for teammates and only preserve full environment inheritance for Lead if that is required. The safe shape is:

```python
env = os.environ.copy() if role_name == LEAD.name else _build_pane_env()
```

Then inject `TAKKUB_ROLE`, `TAKKUB_PROJECT`, and `TAKKUB_LEAD_TOKEN` exactly as today, keeping `TAKKUB_LEAD_TOKEN` Lead-only. Update the audit doc only after that code path is covered by tests.

## ab1ff5f

### Findings

#### high: env allowlist fix misses the Claude teammate spawn path, leaving the original secret leak open

`src/agent_takkub/orchestrator.py:1000`, `src/agent_takkub/orchestrator.py:1044`, and `src/agent_takkub/orchestrator.py:1120`

The commit switched Gemini and Codex provider branches to `_build_pane_env()`, but the default Claude branch still uses `os.environ.copy()` before spawning. Because most specialist roles are Claude-backed unless remapped through provider config, the original leak vector remains open for the default teammate case. The tests only exercise `_build_pane_env()` directly; they do not assert the env passed to `PtySession.spawn()` for a Claude teammate.

Suggested fix: change the Claude branch to build a filtered env for `role_name != LEAD.name`, while allowing Lead to retain the full env if needed for user-level tools. Add a spawn-level regression test that monkeypatches `PtySession.spawn`, sets representative secret vars in `os.environ`, spawns a non-lead Claude role, and asserts the captured `env` excludes those secrets.

#### med: `_PANE_ENV_ALLOWLIST` includes `TAKKUB_LEAD_TOKEN`, which can re-expose the Lead capability token if it is present in the cockpit process environment

`src/agent_takkub/orchestrator.py:78` and `src/agent_takkub/orchestrator.py:91`

`_build_pane_env()` preserves any allowlisted key from `os.environ` before provider-specific code overwrites role/project values. If the cockpit is ever launched from an environment that already contains `TAKKUB_LEAD_TOKEN` (for example from a Lead pane shell, a wrapper, or a stale developer environment), Codex/Gemini teammates receive that token because it is allowlisted. The server also has a role gate, but a leaked token still weakens the two-layer design because a teammate can open the local TCP socket directly and spoof `from: "lead"` if it has the token value.

Suggested fix: remove `TAKKUB_LEAD_TOKEN` from `_PANE_ENV_ALLOWLIST`. Start from a token-free pane env and inject `env["TAKKUB_LEAD_TOKEN"] = self._lead_token` only in the `role_name == LEAD.name` branch. Add a direct `_build_pane_env()` test proving a parent `TAKKUB_LEAD_TOKEN` is dropped, plus provider-spawn tests proving Codex/Gemini/Claude teammates do not receive it.
