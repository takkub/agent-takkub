# Brain Data Model

## MemoryEvent

```python
@dataclass(slots=True)
class MemoryEvent:
    id: str
    event_key: str
    project: str
    scope: str
    kind: str
    content: str
    created_at: str

    role: str | None = None
    task_id: str | None = None
    execution_mode: str | None = None   # pane | subagent
    provider: str | None = None

    source: str | None = None
    confidence: float = 0.5
    status: str = "active"

    supersedes: str | None = None
    superseded_by: str | None = None

    tags: list[str] = field(default_factory=list)
    provenance: dict[str, object] = field(default_factory=dict)
```

## Memory kinds

```text
fact
requirement
decision
constraint
architecture
pattern
lesson
failure
outcome
known_issue
open_question
observation
```

## Trust

แยก:

```text
cockpit_measured
user_confirmed
lead_confirmed
agent_reported
external_untrusted
```

`DigestFacts` fields ควรได้ provenance `cockpit_measured`

agent note/headline ยังเป็น `agent_reported`

## ContinuationRecord

```python
@dataclass(slots=True)
class ContinuationRecord:
    id: str
    project: str
    task_id: str | None
    role: str
    execution_mode: str

    status: str

    summary: str
    completed: list[str]
    remaining: list[str]
    files_changed: list[str]
    decisions: list[str]
    blockers: list[str]
    tests_run: list[str]
    next_action: str | None

    provider: str | None = None
    pane_id: str | None = None
    session_generation: int | None = None
    capsule_path: str | None = None

    created_at: str = ""
```

## Why not HandoffRecord?

agent-takkub already uses the word handoff for file-based long-task delivery and shard consolidated handoff.

`ContinuationRecord` means semantic resume state only.

## Supersede

Never silently overwrite decision history.

```text
/api/v2  -> superseded_by -> /api/v3
```

Default retrieval returns active only.

## Idempotency

Recommended event key:

```text
hash(
  project,
  task_id,
  role,
  execution_mode,
  event_kind,
  lifecycle_marker,
  normalized_content
)
```
