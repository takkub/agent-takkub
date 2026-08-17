# Takkub Brain V2 Architecture

## Core Principle

Brain เป็น knowledge/continuity layer ไม่ใช่ orchestrator ตัวที่สอง

```text
                      Lead / User
                          |
                        assign
                          |
                Assignment Context Builder
                  /          |          \
         current task   Brain hot set   continuation
                  \          |          /
                    composed assignment
                          |
              +-----------+-----------+
              |                       |
         mode=pane              mode=subagent
              |                       |
      existing delivery          task capsule
              |                       |
            Agent               native child
              |                       |
          takkub done        takkub subagent-done
              \                       /
               \                     /
                Completion Boundary
                       |
              +--------+--------+
              |                 |
          Task Ledger       Brain Capture
              |                 |
         Lead Inbox          Memory Events
```

## New package

```text
src/agent_takkub/brain/
├── __init__.py
├── models.py
├── paths.py
├── store.py
├── local_store.py
├── current_state.py
├── retrieval.py
├── context.py
├── continuation.py
├── capture.py
├── policy.py
├── secrets.py
└── render.py
```

## Responsibilities

### models.py
Pure dataclasses/enums:
- MemoryEvent
- BrainHit
- ContinuationRecord
- BrainContextPack
- MemoryKind
- MemoryScope

### paths.py
- `RUNTIME_DIR / "brain"`
- per-project validation
- no direct home-directory assumptions

### local_store.py
- JSONL event journal
- atomic materialized state
- idempotent append
- corruption tolerance

### current_state.py
Small hot set:
- active project constraints
- active project decisions
- architecture facts
- unresolved questions
- continuation pointers

This is what assignment hot path reads.

### retrieval.py
Two levels:

**HOT**
- exact continuation
- active project state
- current role L1
- small bounded set

**COLD**
- BM25 across archive/session/history
- invoked explicitly / on-demand
- not run on every Qt tick

### context.py
Builds the assignment memory section.

### continuation.py
Semantic task continuation:
- completed
- remaining
- changed files
- blockers
- tests
- next action

### capture.py
Single façade used by orchestrator lifecycle.

### policy.py
- admission
- confidence
- promotion
- supersede
- trust
- scope

## Storage

```text
RUNTIME_DIR/
└── brain/
    └── <project>/
        ├── events.jsonl
        ├── current-state.json
        ├── PROJECT.md
        └── continuations/
            └── <task-key>.json
```

Never write raw Brain data into the user's project repository.
