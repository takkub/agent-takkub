# Project Explorer

Required UX:
- left `QSplitter` panel, resizable/collapsible,
- remember width/collapsed state,
- roots come from project config,
- lazy directory expansion,
- Git status badges,
- context menu: Open in Takkub / Open externally / Reveal / Copy path / Ask Agent,
- double-click opens Editor,
- existing file tab focuses instead of duplicating.

Default hidden/ignored:
`.git`, `node_modules`, `.next`, `dist`, `build`, `coverage`, `runtime`, `venv`, `.venv`, `__pycache__`.
Respect Git ignore semantics first.

Security:
- canonical absolute path,
- must remain under allowed configured roots,
- reject traversal/symlink escape/special devices,
- size limit before editor load.

Performance:
- no recursive scan on Qt main thread,
- lazy load,
- debounced refresh,
- background git status.
