# MASTER PROMPT — Takkub Brain V1 on current agent-takkub

You are Lead for `agent-takkub`.

Implement **Takkub Brain V1** using this pack as the product/architecture intent.

## Baseline note

This pack was audited against:

```text
main@0aee262a2b2648b248822e3bb587a49001b14166
version 1.0.68
2026-08-16
```

**Do not assume that is still HEAD.**

## First action: re-audit HEAD

Before editing source:

1. read current `CLAUDE.md`
2. read `docs/lead/role-and-workflow.md`
3. inspect current:
   - config.py
   - orchestrator.py
   - orchestrator_text.py
   - cli.py
   - cli_server.py
   - spawn_engine.py
   - task_ledger.py
   - task_delivery.py
   - digest_facts.py
   - role_memory.py
   - bm25_search.py
   - lead_context.py
   - lead_inbox.py
   - lead_wait.py
   - auto_resume.py
   - routing_planner.py
   - provider_spec.py
4. inspect tests:
   - test_task_handoff.py
   - test_subagent_mode.py
   - test_done_digest_facts_wiring.py
   - test_adaptive_digest_window.py

Write:
`docs/audit/takkub-brain-v1-current-head.md`

Do not code until exact current hook points are documented.

## Non-negotiable rules

1. Original implementation only. Do not copy Oracle/Arra/MAW source.
2. Orchestrator remains sole lifecycle controller.
3. Brain never owns pane/PTY/provider lifecycle.
4. Support both `mode=pane` and `mode=subagent`.
5. Task-relevant Brain context is **assignment-time**, not spawn-only.
6. Use `RUNTIME_DIR`; never hard-code a new home.
7. Do not write raw Brain files into the user's repo.
8. Preserve Role Memory.
9. Preserve Task Ledger semantics.
10. Preserve Task Delivery authority.
11. Reuse `DigestFacts`; do not rerun git probes for Brain outcome.
12. Do not use Lead digest/inbox flush as completion truth.
13. Brain is fail-open.
14. Memory is untrusted DATA.
15. Secret filtering is mandatory.
16. No vector DB/embeddings/graph DB in V1.
17. Hot retrieval must be bounded and lightweight.
18. Deep BM25/session search is cold/on-demand first.
19. autoresume park/wake is not a completion.
20. Follow current targeted-test/full-QA-gate policy.

## Architecture target

```text
assign
  |
BrainContextBuilder (HOT)
  |
composed assignment
  |
  +--> pane existing delivery path
  |
  +--> subagent capsule path

done / subagent_done
  |
authoritative completion
  |
BrainCapture
  |
events.jsonl + current-state + ContinuationRecord
```

## New semantic type

Use `ContinuationRecord`, not `HandoffRecord`.

The codebase already has file-based task handoff for long task delivery.

## Storage

```text
RUNTIME_DIR/brain/<project>/
    events.jsonl
    current-state.json
    PROJECT.md
    continuations/
```

## Assignment context

Priority:

```text
exact task continuation
hard project constraints
active project decisions
relevant architecture
current role L1 lessons
```

Keep the injected Brain block small.

Do not preload session/archive history merely because it exists.

## Retrieval

HOT:
- exact continuation
- materialized active state
- role L1

COLD:
- BM25 events/archive/sessions/task history

## Outcome capture

For clean pane completion, map `DigestFacts` into provenance.

Keep:
- measured facts
- agent summary

separate in trust level.

## Subagent

Brain must work when there is no pane.

The task capsule must carry the same relevant Brain context.

`subagent_done` must capture outcome once.

Shard findings remain task-scope unless verified/promoted.

## Review gates

For every phase:
- targeted tests
- ruff/format
- reviewer
- critic/security review where relevant

At final QA gate:
- one full suite
- import-linter
- Windows + macOS CI
- token/performance measurement

## Stop scope

Do not add:
- vector DB
- embedding API
- remote Brain daemon
- knowledge graph
- web Brain UI
- global cross-project memory
- external Oracle adapter

until V1 Definition of Done passes.

## Final report

Return:
- exact files changed
- lifecycle hooks used
- pane behavior
- subagent behavior
- data model
- persistence
- token/context measurement
- performance
- security
- tests
- known limitations
- V2 suggestions
