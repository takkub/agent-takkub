# UI Examples

## Knowledge

```text
┌───────────────────────────────────────────────────────┐
│ Settings › Knowledge & Design                        │
├──────────────┬────────────────────────────────────────┤
│ Knowledge    │ Brain        ● Healthy   391 records  │
│ OpenViking   │ Obsidian     ● Connected 181 indexed │
│ Design Tools │ Graft        ● Ready     6 projects   │
│ Context Debug│ OpenViking   ● Connected 1,284 docs   │
└──────────────┴────────────────────────────────────────┘
```

## OpenViking

```text
Status             ● Connected
Mode               [ Hybrid ▼ ]
Strict Project     [✓]
Include Global     [✓]
Result Limit       [ 8 ]
Timeout            [ 4.0s ]

[ Test ] [ Sync Active Project ] [ Re-index ]
```

## Design Tools

```text
Storybook   ● Detected
21st.dev    ● MCP Ready
Figma       ● Connected
Penpot      ○ Disabled

[ Test ] [ Permissions ]
```

## Context Debug

```text
Project: agent-takkub   Role: frontend

SOURCE          ITEMS  TOKENS   TIME
Conversation      1      602     3ms
Brain             4      945    10ms
Graft             3      731    21ms
OpenViking        5    1,842    91ms

Total: 4,120 / 6,000
Dedup: 3
Scope rejected: 4

[ View Context ] [ Retrieval Trace ] [ Copy Report ]
```

Wrong-project result example:

```text
OpenViking result #7
project_id: another-project
score: 0.94
decision: REJECTED
reason: project scope mismatch
```
