# Performance & Qt Hard Rules

1. No recursive repo scan on Qt main thread.
2. No heavy git status/diff on Qt main thread.
3. No OpenViking/network operation on Qt main thread.
4. No painted QWebEngineView reparent across projects.
5. Per-project Preview ownership.
6. Prefer one Monaco Editor WebView per project, with internal file tabs; not one WebEngine per file.
7. Integrate hidden editor/preview with keepalive/suspension where safe.
8. Debounce watchers and Git refresh.
9. Bound file/diff size.
10. Diagnostics: watcher backlog, bridge queue, preview/Monaco failure, tree scan time.
