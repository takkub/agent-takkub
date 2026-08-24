# สรุป Master Plan

เป้าหมายสุดท้าย:

```text
                               TAKKUB
                                  |
                           Orchestrator
                                  |
        +-------------------------+--------------------------+
        |                         |                          |
   Workspace/UI              Cognitive Layer            Capability Hub
        |                         |                          |
  Explorer/Editor           Brain / Graft / OV        Skills / MCP / Plugins
  Preview/Design                 |
        |                  Context Builder
        |                         |
        +-------------------------+
                                  |
                    Claude / Codex / Gemini / ...
```

## ระบบข้อมูล

```text
Conversation V2 = session/checkpoint/current conversation
Brain V2        = operational memory/decision/project facts
Obsidian        = human-readable durable curated knowledge
OpenViking      = optional machine retrieval/index over knowledge/resources
Graft           = structural code graph/intelligence
Capability Hub  = skills/MCP/plugins/permissions
Context Builder = final scope/trust/budget/dedup policy
```

กฎสูงสุด:
**ข้อมูลหนึ่งประเภทมี canonical owner หนึ่งตัว**
ระบบอื่นอ่าน/index/reference ได้ แต่ไม่สร้าง competing source-of-truth เอง

## Workspace

```text
Project Sidebar
  └ Project Explorer
       ├ files
       └ Git Changes

Workspace
  ├ Agent panes
  ├ Monaco Editor
  ├ Diff
  └ Live Preview
       ├ Desktop
       ├ Tablet
       └ Mobile
```

## Design

```text
Request
 -> Design Director
 -> references/design system
 -> design artifact
 -> Preview
 -> Approve / Revise
 -> Frontend
 -> Live Preview
 -> Design Reviewer
 -> QA
```
