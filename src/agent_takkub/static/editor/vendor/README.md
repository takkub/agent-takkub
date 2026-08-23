# Monaco vendor drop (devops packaging — #365 phase 2)

Vendored: `monaco-editor@0.56.0` (npm), AMD `min/vs` build only — no CDN, no
npm dependency of `agent-takkub` itself (`04_MONACO_EDITOR_SPEC.md`: "bundle
Monaco locally; no production CDN dependency"). `min/LICENSE` is Monaco's own
MIT license text, shipped alongside.

```
static/editor/vendor/monaco-editor/min/vs/loader.js
static/editor/vendor/monaco-editor/min/vs/...            (trimmed min/vs/, see below)
static/editor/vendor/monaco-editor/min/LICENSE
```

## Size

Stock `monaco-editor@0.56.0` `min/vs/` is **24 MB** uncompressed. Trimmed to
**5.1 MB** (~106 files removed) by dropping only pieces that are either
provably dead for our plain-AMD-loader `<script src="loader.js">` +
`require(['vs/editor/editor.main'])` consumption path, or gate an
IntelliSense feature this read-only viewer never surfaces in its UI:

- **`language/{css,html,json,typescript}/*.worker.js`** (~7.6 MB) — these are
  AMD-importable copies of the four language-service workers, meant for
  bundler consumers that `require('monaco-editor/esm/vs/language/.../x.worker')`
  directly. Grepped the whole `min/vs` tree: nothing here ever references
  those module ids — `tsMode-*.js` and friends only depend on the tiny
  `*.worker-<hash>.js` URL wrapper modules (~200B each, kept). Zero
  functional risk; these files are simply never touched by our loader path.
- **`nls/`** (~1.7 MB: locale bundles + `.d.ts`) — `nls.messages-loader.js`
  only fetches `vs/nls/lang/<locale>` when `require.config({"vs/nls": {...}})`
  names a non-English locale; our loader.js call never sets that key, so
  English (baked into `editor-*.js`) is always used. Zero functional risk.
- **`assets/{ts,css,html,json}.worker-<hash>.js`** (~8.9 MB) — the *real*
  runtime workers for the four rich language modes' semantic
  validation/diagnostics (squiggly markers, hover, completion, formatting).
  This viewer is `readOnly: true` with no problems panel, hover UI, or
  autocomplete surfaced anywhere, so that feature set is genuinely unused.
  Dropping them does **not** remove syntax highlighting — Monarch-based
  tokenization (the actual source of the colors you see) is registered
  separately via `basic-languages/monaco.contribution.js` and runs
  synchronously in the main thread, independent of the worker. What's lost:
  semantic/hover/completion features we don't expose anyway. Missing worker
  scripts fail async (404 on `new Worker(url)`, caught by Monaco's own
  worker-client error handling) — non-fatal, but **not verified in a real
  browser** (devops tooling is barred from installing Playwright/Chromium;
  only static/parse-based checks were possible here). **QA: please confirm
  ts/js/css/html/json syntax highlighting still renders correctly** when
  browser-testing the editor page — if it doesn't, restore these 4 files from
  a fresh `npm pack monaco-editor@0.56.0` under `min/vs/assets/`.
  `assets/editor.worker-*.js` + `assets/editorWebWorkerMain-*.js` (the
  generic core editor worker, used by the diff view) are kept.
- **Basic-language chunks for languages outside the target set** — kept only
  the lazy-loaded top-level chunk for `typescript, javascript, python,
  markdown, yaml, shell, css, html` (json has no separate chunk — it's
  owned entirely by the rich mode). Every other `basic-languages` id (rust,
  go, java, …) stays *registered* (via `basic-languages/monaco.contribution.js`,
  a few KB, always present) but its syntax-highlighting chunk is absent, so
  opening a file in one of those languages 404s that one lazy chunk and
  falls back to no highlighting for that file only — same graceful-degrade
  shape `index.html` already uses for a fully-missing bundle. If a language
  gets added to `editor_widget.py`'s `LANG_BY_EXT` later, re-copy its chunk
  from a fresh `npm pack monaco-editor@<pinned version>` (filename is
  `<langid>-<hash>.js` at the top of `min/vs/`).

Ceiling / upgrade path if this ever needs to shrink further: a custom
`esbuild`/`monaco-editor-webpack-plugin` build with `languages: [...]`
filtering can drop the TypeScript compiler bundle itself
(`language/typescript` rich-mode registration, ~6.5 MB) — out of scope for
this packaging pass (would mean vendoring a build step, not just npm-pack
output).

## Bumping the version

Re-run: `npm pack monaco-editor@<version>`, extract, re-apply the same
keep/drop lists above (or re-derive them — grep for each dropped file's
module id across the new `min/vs` tree before assuming the same files are
still dead in a newer release), replace this directory, update the pinned
version in this file's first line and in the devops done-report / CHANGELOG.

## Fallback (already wired, unaffected by anything above)

If this directory is ever emptied again, `static/editor/index.html` degrades
gracefully: the AMD loader script 404s, `index.html` falls back to a plain
read-only `<pre>` viewer (still respects containment, size cap, and the
open/close/reveal/open-externally bridge calls — only the Monaco-specific
bits, syntax highlighting and the diff view, are unavailable).
