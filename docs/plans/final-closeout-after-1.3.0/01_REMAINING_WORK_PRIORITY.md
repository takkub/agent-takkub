# Remaining Work Priority

## P0 / Immediate
### #376 delivery correctness
Account-pending/verifying screen must block task paste even when a `>` prompt-looking footer is visible.

Exit:
- synthetic regression
- targeted tests
- real agy verification if reproducible
- no regression for other providers

## P1 / Must-have hardening
### OpenViking strict project isolation
Wrong-project knowledge must be rejected before Context Builder injection.

### Context/token gating
Do not call Brain/Graft/OpenViking/Design tools all at once for trivial tasks.

## P2 / Product completeness
### Settings UI
- Knowledge overview
- OpenViking
- Design Tools
- Context Debug

### Real integration validation
- 21st MCP
- Figma
- Penpot
- OpenViking server

### Real GUI acceptance
- Monaco
- Diff
- Preview
- A/B project switching
- Ask Agent

## P3 / Ops
- rollback drill
- observability polish
- field soak
