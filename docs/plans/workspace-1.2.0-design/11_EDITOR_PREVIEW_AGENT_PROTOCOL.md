# Editor / Preview / Agent Protocol

Suggested events:
```text
workspace.file.opened
workspace.file.saved
workspace.file.conflict
workspace.file.disk_changed
workspace.preview.opened
workspace.preview.updated
workspace.design.published
workspace.design.approved
workspace.design.revision_requested
```

Design publish action concept:
```text
publish_design_artifact(project_id, artifact_path, title, mode)
```
Cockpit validates -> ensures Preview -> opens/focuses -> records non-sensitive state.

Approve:
- mark approved,
- structured notice to Lead,
- optional Brain durable design decision,
- Obsidian only if durable-knowledge policy passes.

Revise:
- send artifact id/path + feedback + constraints to Designer.
Do not resend giant HTML if artifact reference is enough.
