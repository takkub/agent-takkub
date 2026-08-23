# Proposed CLI Examples

Adapt names/flags to current parser conventions before coding.

```bash
takkub preview --url http://127.0.0.1:3000
takkub preview --file docs/design-review/dashboard.html
takkub editor open src/app/page.tsx
takkub editor reveal src/app/page.tsx
takkub design publish --file docs/design/dashboard.html --title "Dashboard v2"
takkub preview status
```
Prefer IPC to the running Cockpit, not a second UI process.
