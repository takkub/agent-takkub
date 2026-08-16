# Implementation Phases

## Phase 0 — Re-audit current HEAD

Required every time before coding.

Record:
- HEAD SHA/version
- assign pane path
- assign subagent path
- task_id / ledger identity
- done path
- subagent_done path
- DigestFacts creation point
- task capsule creation point
- provider task transformation order
- task handoff pointer order
- role memory injection point
- import-linter contracts

Deliverable:
`docs/audit/takkub-brain-v1-current-head.md`

## Phase 1 — Core storage

- models
- paths
- local event store
- current-state materialization
- idempotency
- supersede
- redact
- secret filter
- tests

No lifecycle integration yet.

## Phase 2 — CLI/manual Brain

Add:
```text
brain remember
brain search
brain show
brain trace
brain redact
```

Prove persistence across restart.

## Phase 3 — HOT retrieval

Implement only:
- exact continuation
- active current-state
- current role L1
- bounded ranking
- dedup

No deep session scan on assign.

## Phase 4 — Assignment integration

Integrate once at common assignment composition boundary.

Tests:
- pane fresh spawn
- pane already running
- pane reassignment
- provider without file-read
- long task
- subagent capsule
- shard subagent

## Phase 5 — Completion/continuation

Integrate:
- `done`
- `done --fail`
- `subagent_done`
- semantic task replacement
- provider/session replacement where meaningful

Reuse DigestFacts.

## Phase 6 — Cold BM25 search

Extend existing BM25 corpus for manual search:
- project Brain events
- role L2
- sessions
- task history
- historical continuations

Benchmark before putting any part on hot path.

## Phase 7 — Promotion rules

Conservative only:
- user/Lead confirmed -> project
- verified repeated lesson -> candidate
- shard scan result -> task scope first
- no unrestricted autonomous promotion

## Phase 8 — stabilization

- full suite once at QA batch gate
- targeted tests during dev
- ruff
- format
- import-linter
- Windows/macOS matrix
- performance benchmark
- token/context measurement
