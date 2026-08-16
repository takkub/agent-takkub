# Changes from the Previous ZIP

## Keep

Still valid:
- Project Brain is the biggest missing layer
- JSONL local-first store
- role memory remains existing source
- BM25 remains useful
- supersede/redaction
- memory as untrusted data
- no vector DB in V1
- no external Oracle runtime
- fail-open integration

## Change

### OLD
`Brain Context Builder at spawn`

### NEW
`Brain Context Builder at assignment`

Spawn can still load role baseline, but task-relevant Brain context must be rebuilt every assignment.

---

### OLD
Only pane-centric lifecycle examples

### NEW
One semantic lifecycle for:
- pane
- subagent

---

### OLD
Structured HandoffRecord

### NEW
`ContinuationRecord`

Avoid naming collision with agent-takkub's existing long-task file handoff.

---

### OLD
Deep BM25 sources could be considered on spawn

### NEW
Hot/cold split:
- hot = current-state + exact continuation + L1
- cold = BM25 archive/session/history on demand

---

### OLD
10k–16k chars suggested context budget

### NEW
Start around 2.5k–4k chars total Brain block and benchmark.

Reason: current agent-takkub is actively reducing repeated context/token cost.

---

### OLD
Outcome capture conceptually from done

### NEW
Explicitly reuse `DigestFacts` and keep agent note provenance separate.

---

### OLD
Potential hooks around notifications

### NEW
Never use Lead digest/inbox as Brain source of truth.
Capture at authoritative completion boundary.
