# Monaco vendor drop (devops packaging — #365 phase 2)

This directory is empty in git. `static/editor/index.html` expects the AMD
build of `monaco-editor` (npm package `monaco-editor`, `min/` output) copied
in as:

```
static/editor/vendor/monaco-editor/min/vs/loader.js
static/editor/vendor/monaco-editor/min/vs/...            (the rest of min/vs/)
static/editor/vendor/monaco-editor/min/LICENSE
```

No CDN, no npm dependency of `agent-takkub` itself — this is a build-time
copy step into the wheel/npm package (`04_MONACO_EDITOR_SPEC.md`: "bundle
Monaco locally; no production CDN dependency"). Expected size ~5–10MB; if
that's too big for the wheel, trim `min/vs/basic-languages/*` down to the
languages `editor_widget.py`'s `LANG_BY_EXT` map actually names.

Until this is populated, the editor page degrades gracefully: the AMD
loader script 404s, `index.html` falls back to a plain read-only `<pre>`
viewer (still respects containment, size cap, and the open/close/reveal/
open-externally bridge calls — only the Monaco-specific bits, syntax
highlighting and the diff view, are unavailable).
