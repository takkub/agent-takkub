# Context / Token Efficiency

Goal: make Takkub smarter while using fewer or more relevant tokens.

## Context Gate

### Small task
Examples:
- change button color
- rename field
- fix spacing

Use:
- current file/selection
- minimal project rules
- Storybook only if component choice is relevant

Do NOT automatically call:
- OpenViking
- Figma
- 21st
- broad Brain recall
- broad Graft graph

### Medium task
Examples:
- refactor feature
- fix cross-file bug

Use:
- Brain scoped recall
- Graft structural lookup
- relevant files
- conversation summary

### Large/new feature
Examples:
- new workflow
- architecture
- new UI section

Use:
- Brain
- Conversation
- Graft
- OpenViking
- Storybook
- Designer/reference tools when UI work exists

## Suggested budgets

Small:
~2k–4k injected context

Medium:
~4k–8k

Large:
~6k–12k, adaptive to model/context window

These are policy targets, not hard universal limits.

## Required trace

For each assignment expose:
- source
- item count
- tokens
- latency
- dedup count
- scope rejects

If a small task used 15k+ context, flag it as inefficient.
