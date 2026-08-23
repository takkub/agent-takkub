# Current Baseline Audit

Reuse current strengths instead of rebuilding:

- `ProjectTab` already owns one project's pane `QTabWidget` and keepalive behavior.
- `TerminalWidget` already uses `QWebEngineView` + `QWebChannel` and clickable paths.
- `design_review_html.py` already creates self-contained visual review HTML with screenshots.
- Graft already provides structural code intelligence and live resync.
- V2 provider/account/routing/conversation/brain/scheduler/storage/UI phases already exist.

Gaps this epic fills:
- no embedded project file tree,
- no embedded source editor,
- no conflict-safe edit workflow,
- no first-class project Live Preview,
- no explicit Design Director + Approve/Revise workflow.
