# Context Merge Policy

Priority example:

1. current task/user instruction
2. explicit project decisions
3. current conversation summary
4. current code structure from Graft
5. relevant curated knowledge/resources from OpenViking
6. lower-confidence operational memories

Rules:
- exact project scope wins over global
- explicit/user-confirmed trust outranks inferred
- stale/superseded records excluded
- near duplicates collapse
- resources must cite provenance
- Context Builder owns token budget
- OpenViking never directly injects into panes
