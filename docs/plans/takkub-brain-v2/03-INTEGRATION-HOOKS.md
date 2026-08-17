# Integration Hooks for 1.0.68

## Rule 1 — assignment-time integration

Do not inject Brain only in `spawn_engine.py`.

Reason:
- pane may already exist
- the same pane can receive multiple assignments
- subagent has no pane

Target concept:

```text
Orchestrator.assign(...)
    |
    +-- resolve project/role/mode
    +-- create ledger assignment
    +-- build BrainContextPack
    +-- compose task + memory section
    |
    +-- pane path -> existing _assign_dispatch / delivery
    |
    +-- subagent path -> existing capsule generation
```

Lead must find exact current common boundary during Phase 0.

## Rule 2 — Brain context goes through existing provider delivery logic

Brain must not decide:
- pointer vs inline
- provider ready gate
- boot marker
- paste timing
- task delivery identity

Compose bounded Brain memory into the assignment before the existing delivery transformation.

This preserves ProviderSpec behavior such as `supports_agent_file_read`.

## Rule 3 — completion capture must cover BOTH completion paths

At minimum:

```text
Orchestrator.done(...)
Orchestrator.subagent_done(...)
```

Prefer extracting/reusing a shared internal completion façade if current architecture allows it cleanly.

Do not capture from:
- digest flush
- Lead UI rendering
- inbox read
- wait poll

Those are observation/delivery surfaces, not task truth.

## Rule 4 — reuse DigestFacts

For pane-mode clean done:

```text
DigestFacts
 -> Brain outcome provenance
```

Do not rerun git status/diff merely to populate Brain.

Structured cockpit facts:
- issue ref
- branch
- commits ahead
- uncommitted
- merge conflicts
- files touched/dirs
- report path

Agent headline:
- store separately as agent-reported context

## Rule 5 — subagent outcome

`subagent_done()` already updates:
- ledger
- wait
- inbox
- worktree finalize
- session decision note
- hot note

Brain should attach to the authoritative completion path once, with an idempotency key.

## Rule 6 — shards

Per-shard completion:
- task-scope outcome allowed
- do not auto-promote to project knowledge

After consolidated shard handoff / Lead confirmation:
- promote verified shared lesson if appropriate

This prevents 20 scan children from polluting Project Brain.

## Rule 7 — autoresume

Do NOT write continuation/outcome on:
- usage-limit park
- wake
- same task replay

Only write when:
- semantic task progress is explicitly captured
- task is replaced/superseded
- provider/session replacement requires continuation
- task finishes/fails/closes

## Rule 8 — Lead context

Lead startup may receive only a tiny Project Brain snapshot:
- active hard constraints
- current architecture decisions
- unresolved blockers

Everything else is pull-on-demand:

```bash
takkub brain search ...
takkub brain trace ...
```
