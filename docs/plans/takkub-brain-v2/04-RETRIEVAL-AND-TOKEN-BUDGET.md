# Retrieval + Token Budget

## Important change from V1

Do NOT scan full sessions/archive with BM25 on every assignment.

The project is actively optimizing:
- spawn cost
- repeated cache cost
- role-file context
- large-file reads

Brain must not reintroduce permanent context bloat.

## Two-tier retrieval

### HOT PATH

Runs for each assignment.

Inputs:
- exact ContinuationRecord
- active Project Brain current-state
- current role L1 memory
- task identifiers

No full session scan.

Target:
- deterministic
- bounded
- low latency
- safe on cockpit lifecycle path

### COLD PATH

Explicit:

```bash
takkub brain search "..."
```

Sources:
- project events
- L2 role archive
- sessions
- task history
- old continuations

Use existing BM25 implementation where practical.

## Suggested V1 injection budget

Start conservative and benchmark:

```text
total Brain assignment block: ~2,500–4,000 chars
exact continuation:          up to ~1,800 chars
project constraints/decisions: ~1,200 chars
role lessons:                  ~800 chars
```

These are starting design limits, not measured provider token counts.

Expose config only if needed; avoid dozens of tuning flags in V1.

## Priority

```text
exact same-task continuation
> hard project constraint
> active project decision
> relevant architecture
> same-role current lesson
> historical note
```

## Pull-on-demand rule

If memory is not necessary to safely begin the assignment:

```text
do not preload it
```

The agent can search later.

## No duplicate injection

If current task already contains a confirmed constraint verbatim/near-verbatim:
- don't add it again from Brain

If Project Brain and Role Memory contain same lesson:
- prefer structured active Project Brain event

## Main-thread rule

Never do:
- whole runtime tree walk
- whole session corpus rebuild
- unbounded JSONL parse
- embedding
- network call

inside a periodic Qt/main-thread tick.

Materialize `current-state.json` on write so hot reads stay small.
