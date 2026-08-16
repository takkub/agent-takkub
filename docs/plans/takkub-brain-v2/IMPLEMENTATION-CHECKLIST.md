# Implementation Checklist

## Audit
- [ ] current HEAD recorded
- [ ] current version recorded
- [ ] assign pane path mapped
- [ ] assign subagent path mapped
- [ ] done path mapped
- [ ] subagent_done path mapped
- [ ] DigestFacts boundary mapped
- [ ] provider transformation order mapped
- [ ] task pointer order mapped
- [ ] import contracts checked

## Core
- [ ] brain models
- [ ] RUNTIME_DIR paths
- [ ] project validation
- [ ] events.jsonl
- [ ] current-state.json
- [ ] ContinuationRecord
- [ ] idempotency
- [ ] supersede
- [ ] redact
- [ ] secret filter

## CLI
- [ ] brain remember
- [ ] brain search
- [ ] brain show
- [ ] brain trace
- [ ] brain redact

## Hot retrieval
- [ ] exact continuation
- [ ] project constraints
- [ ] project decisions
- [ ] role L1
- [ ] dedup
- [ ] hard cap
- [ ] no deep runtime scan

## Assignment integration
- [ ] pane new spawn
- [ ] pane already alive
- [ ] pane reassign
- [ ] provider no-file-read
- [ ] long task
- [ ] subagent capsule
- [ ] shard

## Completion
- [ ] done ok
- [ ] done fail
- [ ] subagent done
- [ ] subagent fail
- [ ] DigestFacts reuse
- [ ] no notification-based truth
- [ ] no duplicate capture

## Continuity
- [ ] provider switch
- [ ] process restart
- [ ] stale continuation
- [ ] task supersede
- [ ] autoresume no-duplicate

## Cold search
- [ ] project events
- [ ] role L2
- [ ] sessions
- [ ] old tasks/continuations
- [ ] benchmark

## Security
- [ ] trust wrapper
- [ ] secret block/redact
- [ ] prompt injection
- [ ] project isolation
- [ ] path traversal

## Final gate
- [ ] targeted tests
- [ ] full QA suite
- [ ] ruff
- [ ] format
- [ ] import-linter
- [ ] Windows
- [ ] macOS
- [ ] token measurement
- [ ] performance measurement
