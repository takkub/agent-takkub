# Target Architecture

```text
                             TAKKUB COCKPIT
                                  |
                           Project Workspace
                 +----------------+----------------+
                 |                                 |
        Project Explorer                     Workspace Tabs
                 |                                 |
       file tree / changes        +----------------+------------------+
                 |                |        |        |        |         |
                 |              Lead    Agents   Editor   Preview   Review
                 |                                  |        |
                 +-----------------------+----------+--------+
                                         |
                                  Workspace Services
                                         |
              +--------------------------+--------------------------+
              |             |             |            |           |
           Graft          Brain V2     Conversation   Git       Capability
              |             |             |            |           |
        Code structure   Agent memory  Session state  Diff      MCP/skills
                                                       |
                                                   Obsidian
                                                       |
                                                OpenViking (optional)
```

Suggested modules:
```text
project_explorer.py
editor_widget.py
editor_service.py
file_watch_service.py
project_file_index.py
git_changes_service.py
preview_widget.py
preview_controller.py
design_workspace.py
design_actions.py
static/editor/index.html
static/editor/monaco/...
```

UI classes are views. Safe filesystem writes belong in services, not `ProjectTab`.
