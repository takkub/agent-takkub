# Pane + Subagent Continuity

## Pane mode

```text
assign
 -> Brain hot context
 -> composed assignment
 -> provider rewrite / task pointer logic
 -> DeliveryManager
 -> pane
 -> done
 -> Continuation/Outcome capture
```

## Subagent mode

```text
assign --mode subagent
 -> Brain hot context
 -> composed task capsule
 -> native child
 -> subagent-done
 -> Continuation/Outcome capture
```

## One semantic model

Brain should not care whether executor is pane or native child except for metadata.

```text
execution_mode = pane | subagent
```

## Provider switch

Example:

```text
backend / codex pane
  fails or is intentionally replaced
      |
ContinuationRecord
      |
backend / claude pane
      |
same task + continuation + active project constraints
```

Provider identity is provenance, not task meaning.

## Same pane, new task

A pane survives and gets a new assign:

```text
old task context must NOT remain the only source
```

New assignment must rebuild BrainContextPack.

## Crash recovery

If no explicit semantic continuation exists:

fallback order:

```text
current Task Ledger
last_assigned_task
DigestFacts / measured task state when available
recent role outcome
```

Recovered record must be marked lower confidence.

## File-based task handoff vs semantic continuation

Do not conflate:

```text
_task_handoff_pointer
  = delivery optimization for long task text

ContinuationRecord
  = semantic state for future executor
```
