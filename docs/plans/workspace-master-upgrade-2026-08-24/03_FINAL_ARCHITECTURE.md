# Final Architecture

```text
                         MainWindow / Cockpit
                               |
                     ProjectNav / Workspace
                +--------------+---------------+
                |                              |
         Project Explorer                Shared Docks
         (per project)                  (app-wide WebViews)
                |                       /             \
         files + changes           Monaco Editor    Preview
                |                       |             |
                +----------- Workspace Services -----+
                                      |
           +--------------------------+---------------------------+
           |                          |                           |
       Orchestrator             Cognitive Layer             Capability Hub
           |                    /      |      \                    |
     Conversation V2         Brain   Graft   OpenViking          MCP/Skills
                                 \     |      /                    |
                                  Context Builder ----------------+
                                         |
                               Provider Agent Context
```

## WebEngine rule

- ONE Monaco WebView app-wide.
- ONE Preview WebView app-wide.
- Never re-parent a painted QWebEngineView across project containers.
- Switch content/state, not the widget.
- Lazy create / destroy.
- Discard when hidden where safe.

## Project-aware Preview invariant

PreviewController:
`project_id -> PreviewState`

PreviewHost:
displays exactly the active project's state.

A background project may update its stored state but must not masquerade as the active project.
