# Fresh-spawn task delivery without a model-driven Read hop

Date: 2026-07-24
Scope: spawn engine + first-task delivery only

## Outcome

A fresh Claude-backed pane now receives its complete first task through the
file already passed to Claude with `--append-system-prompt-file`. The PTY sends
only a fixed, short trigger that starts the first turn. The model therefore sees
the task before inference and does not spend its first turn deciding to call
Read on the task handoff file.

Assignments to an already-running pane are unchanged: long tasks still use the
short task-file pointer. Providers without a confirmed file-backed
append-system-prompt mechanism also keep that pointer flow.

This targets audit item 2 in
`docs/qa-reports/2026-07-24-token-audit.md`: 301/312 new sessions used the
pointer-to-Read flow, consuming 17,872,549 tokens in the first round-trip over
the seven-day sample.

## Delivery design

1. `_assign_dispatch` now finishes all task composition before `spawn()`:
   session goal, Codex notice, verify-role appendix, and optional planner
   wrapper.
2. The full composed task is still materialised as the normal handoff file.
   That file remains the reliable fallback and remains visible through
   `last_assigned_task_file`.
3. If the effective provider exposes `ProviderSpec.system_prompt_flag`, the
   task is staged in `PaneState` before spawn. The staging state survives the
   spawn gate and FIFO arbiter without adding a large payload to either queue.
4. The Claude branch renders a pane-scoped system-prompt copy next to the role
   context and appends an explicitly one-shot task block.
5. After the native session attaches, `_send_when_ready` submits only:

   `Start the current task from the one-shot system-prompt block now.`

6. If prompt preparation fails, the attached pane receives the prebuilt task
   pointer exactly once. No assignment is silently dropped.

The one-shot block contains both machine markers and plain-language scope:

- `<!-- takkub-current-spawn-task:start -->`
- heading `Current task for this spawn (one-shot)`
- text saying it is not a standing instruction and must not be reused on a
  later respawn/resume unless that spawn contains a new block
- `<!-- takkub-current-spawn-task:end -->`

## Stale-task and concurrency safety

The role source file is not used as the mutable task carrier. Each pane gets a
deterministic copy named like `CLAUDE.spawn-<pane-hash>.md`, where the hash is
derived from project + role. This has three properties:

- two shards/projects sharing the same base-role staging directory cannot
  overwrite each other's current task;
- every native spawn rewrites its pane-scoped copy from the stable role source;
- a respawn/resume with no new assignment rewrites the copy without the task
  block, so the old task cannot remain in the system prompt.

A final-gate retry reuses the immutable copy for the same pending native spawn,
avoiding one leaked file per 50 ms retry. Once a native spawn succeeds, the
transient task payload/path is cleared from `PaneState`. Crash replay continues
to use `last_assigned_task` and the existing resume-aware rules; it does not
re-add the old task as a system instruction.

## Provider capability audit

`system_prompt_flag` now means specifically: a CLI option that accepts a file
path and appends that file to the interactive session's system prompt. A
positional or string user-prompt argument is not treated as equivalent because
putting a multi-kilobyte task in argv reintroduces Windows command-length and
escaping risks.

| Provider | Checked surface | Result | Delivery |
|---|---|---|---|
| Claude Code 2.1.218 | `claude --help` | `--append-system-prompt-file` is supported | Fresh-task preload wired |
| Codex CLI 0.145.0 | `codex --help` | only positional `[PROMPT]`; no file-backed append-system-prompt flag | Existing pointer flow |
| Antigravity/agy 1.1.5 | `agy --help` | `--prompt-interactive` accepts a string; no system-prompt file flag | Existing pointer flow |
| OpenCode 1.18.4 | `opencode --help` | `--prompt` accepts a string; no system-prompt file flag | Existing pointer flow |
| Kimi CLI 1.49.0 | `kimi --help` | `--prompt` is a user string; `--agent-file` replaces a whole agent spec | Existing pointer flow |
| Cursor CLI | binary unavailable locally; official parameter reference checked | positional initial prompt only; no file-backed append-system-prompt flag documented | Existing pointer flow |

Cursor source:
<https://docs.cursor.com/en/cli/reference/parameters>

The unsupported status is explicit both here and beside each provider's
`system_prompt_flag=None` in `provider_spec.py`.

## Behaviour kept unchanged

- Mid-session assignments still use `_task_handoff_pointer`.
- No multi-kilobyte task is pasted into a PTY or placed in argv.
- Full composed task text remains in `last_assigned_task` for crash replay.
- Plan/fan-out tasks preload the fully wrapped planner task on a fresh Claude
  pane and retain the same `plan_fanout` bookkeeping.
- Non-Claude provider argv remains unchanged.
- Task-file write or system-prompt preparation failure degrades to the previous
  delivery path.

## Targeted verification

Tests cover:

- one-shot marker rendering and removal on the next spawn;
- fresh Claude preload + tiny trigger (no task pointer);
- prompt-file failure fallback exactly once;
- mid-session pointer preservation;
- deferred spawn retention without an early pointer;
- provider capability matrix;
- existing task handoff, auto-respawn replay, spawn gate, plan/fan-out,
  unconfirmed-delivery, and non-Claude provider argv regressions.

Commands used (with this worktree's `src` forced ahead of the editable install):

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -m pytest tests/test_spawn_task_delivery.py tests/test_task_handoff.py tests/test_orchestrator_auto_respawn_replay.py -q
python -m pytest tests/test_spawn_gate.py -q
python -m pytest tests/test_qa_plan_fanout.py tests/test_delivery_unconfirmed.py -q
python -m pytest tests/test_spawn_codex_argv.py tests/test_opencode_provider.py tests/test_kimi_provider.py tests/test_cursor_provider.py -q
python -m ruff check src/agent_takkub/spawn_engine.py src/agent_takkub/orchestrator.py src/agent_takkub/provider_spec.py tests/test_spawn_task_delivery.py
python -m ruff format --check src/agent_takkub/spawn_engine.py src/agent_takkub/orchestrator.py src/agent_takkub/provider_spec.py tests/test_spawn_task_delivery.py
```
