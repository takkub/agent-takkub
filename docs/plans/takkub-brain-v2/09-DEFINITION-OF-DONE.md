# Definition of Done

Brain V1 is ready only if:

## Core
- [ ] persistent local Project Brain
- [ ] current-state materialization
- [ ] supersede
- [ ] redact
- [ ] secret policy
- [ ] fail-open

## Pane mode
- [ ] first assign gets context
- [ ] reassign to already-alive pane gets NEW context
- [ ] long task uses existing delivery behavior
- [ ] provider capability behavior unchanged

## Subagent mode
- [ ] capsule receives Brain context
- [ ] subagent_done captured once
- [ ] shard results don't pollute Project Brain
- [ ] wait/inbox semantics unchanged

## Continuity
- [ ] provider switch can continue same task
- [ ] restart can continue same task
- [ ] stale continuation rejected
- [ ] autoresume park/wake doesn't duplicate memory

## Retrieval
- [ ] hot retrieval bounded
- [ ] cold BM25 search works
- [ ] no cross-project leak
- [ ] active decision wins
- [ ] superseded excluded

## Truth
- [ ] DigestFacts reused for measured outcome metadata
- [ ] agent prose tagged separately
- [ ] Brain does not parse Lead digest as source of truth

## Context
- [ ] assignment-time, not spawn-only
- [ ] hard cap
- [ ] no whole-history preload
- [ ] untrusted wrapper

## Quality
- [ ] targeted tests green
- [ ] QA full suite green
- [ ] ruff green
- [ ] format green
- [ ] import-linter green
- [ ] Windows CI green
- [ ] macOS CI green
- [ ] performance/token measurement documented
