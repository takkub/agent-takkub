# QA Test Plan

## Unit
- preview file URL normalization
- project preview state transitions
- project close cleanup
- strict UTF8/BOM
- mode preservation
- deleted diff
- rename old_path
- multi-repo aggregation
- context dedup
- OpenViking adapter error/fail-open

## Integration
- Explorer -> Editor
- Editor save -> HMR -> Preview
- external edit -> conflict
- Designer publish -> Preview
- Revise -> Designer
- Approve -> Lead
- project switch A/B Preview
- OpenViking query -> Context Builder

## Real GUI
- Monaco typing/highlight
- Diff view
- local HTML
- dev URL
- viewport presets
- repeated project switching

## Security
- traversal
- junction/symlink
- malicious preview navigation
- spoofed IPC project
- untrusted MCP content
- secret paths excluded from OV

## Performance
- 3 projects editor+preview
- 10k+ file project
- multi-repo project
- WebEngine soak
- repeated preview open/close
