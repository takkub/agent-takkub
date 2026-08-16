# Brain V2 Test Plan

## Storage
- restart persistence
- Thai/Unicode
- corrupt JSONL tail
- duplicate event
- concurrent append
- supersede
- redaction
- path traversal
- secret rejection

## Assignment
- pane not spawned yet
- pane already alive
- second assignment to same pane
- long task pointer path
- provider with no agent file-read
- provider override
- subagent capsule
- subagent shard

## Completion
- pane done
- pane failed
- subagent done
- subagent failed
- close/supersede
- duplicate completion request
- DigestFacts reused, no duplicate git probes

## Continuation
- same task new pane
- provider switch
- app restart
- stale task continuation rejected
- old role assignment not resumed into new task
- autoresume park/wake creates no duplicate continuation

## Retrieval
- hard constraint always beats historical note
- active decision beats superseded
- same task continuation first
- role L1 relevance
- no cross-project leak
- Thai
- code identifiers
- dedup

## Token / performance
- Brain block hard cap
- 10k events does not imply 10k-event hot parse
- current-state read bounded
- cold search benchmark separately
- no archive scan in Qt periodic tick

## Security
- injected "ignore Lead" remains untrusted text
- raw API key not persisted
- agent headline does not become cockpit-measured fact
- external/tool output cannot become hard constraint automatically

## Regression
Follow current project test-tier rule:
- targeted tests during implementation
- one full QA batch gate before merge/push
- Windows + macOS CI
- import-linter
