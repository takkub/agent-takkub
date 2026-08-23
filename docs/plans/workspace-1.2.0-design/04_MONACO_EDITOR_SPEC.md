# Monaco Editor

Decision: bundle Monaco locally; no production CDN dependency.

V1:
- syntax highlight, line numbers, find/replace, bracket matching,
- multiple Monaco tabs inside one per-project Editor WebView,
- dirty marker,
- Ctrl+S/Cmd+S,
- read-only fallback for binary/large files,
- diff editor,
- cursor/scroll preservation.

Not V1:
- full LSP platform,
- debugger,
- extension marketplace,
- full VS Code IntelliSense parity.

Bridge concepts:
```text
openFile(path)
saveFile(path, expectedVersion, text)
requestDiff(path)
askAgent(path, startLine, endLine, selectedText, request)
openExternally(path)
revealInExplorer(path)
```

Conflict rule:
Track mtime_ns + size + recommended SHA-256. If disk version changed since load, never overwrite silently.
Show: `[Compare] [Reload disk] [Keep mine / overwrite]`.

Write atomically via same-directory temp + replace where safe.
