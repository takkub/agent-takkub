# Design Workflow Protocol

```text
User request
 -> Lead
 -> Designer
 -> design context/reference lookup
 -> artifact publish
 -> Preview for correct project
 -> user Approve / Revise
```

## Revise
- artifact -> revision_requested
- Lead gets coordination/audit notice
- live Designer gets actionable structured feedback
- if no Designer: Lead fallback
- new revision should publish a new version/artifact where appropriate

## Approve
- artifact -> approved
- Lead notice
- optional Brain durable decision
- optional Obsidian mirror only if durable-worthy
- frontend implementation continues

Do not send entire HTML in agent message if artifact reference/path is enough.
