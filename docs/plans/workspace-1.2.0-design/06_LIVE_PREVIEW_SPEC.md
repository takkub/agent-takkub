# Live Preview

Per-project `Preview` tab.

Modes:
1. URL: `http://127.0.0.1:<port>` for Next/Vite/etc.
2. File: approved local HTML/design review artifact.

Proposed CLI shape (adapt to current parser conventions):
```bash
takkub preview --url http://127.0.0.1:3000
takkub preview --file docs/design-review/dashboard.html
takkub preview close
takkub preview status
```

Controls:
Desktop / Tablet / Mobile / Refresh / Open externally / Approve / Revise.

CRITICAL WebEngine rule:
Each project owns its Preview QWebEngineView. Never use one global view and re-parent it after first paint.

Security:
- no privileged QWebChannel on arbitrary remote pages,
- approved artifact roots only,
- explicit external navigation policy,
- no secret injection.
