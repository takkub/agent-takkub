"""The Content-Security-Policy every published report is served with.

One constant, in its own module, because two sides need the same value and
must not import each other: `remote/http_server.py` sends it on the `/r/`
route, and `report_builder.check_report_file` (`takkub report check`, which
the CLI imports and which may run in a build without `remote/`) serves the
report under it so the check sees what the reader will see (#1030).

#367 Remote Reports: a report is a standalone document. `publish()` /
`validate_standalone_html` (`remote/reports.py`) reject the external-reference
shapes they know to look for at store time, but that regex check is a
secondary gate — this header is what actually stops a report from reaching a
third-party origin at render/fetch time (`default-src 'self'` catches whatever
the store-time check misses, via directive fallback). That is why inline
script and style can be allowed here, unlike the PWA shell's own policy.

#1030: a report that does its work in the page (OCR, parsing an upload) runs a
Worker built from a blob: URL and compiles WebAssembly it carries inline.
Neither reaches another origin, so they are allowed: `worker-src blob:` for
the worker, `blob:` in `script-src` for the `importScripts` a blob worker
does, `'wasm-unsafe-eval'` for WebAssembly only (JS `eval` stays blocked), and
`connect-src 'self' blob: data:` so the page can read back its own embedded
bytes.
"""

from __future__ import annotations

REPORT_CSP_HEADER = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' blob:; "
    "worker-src 'self' blob:; connect-src 'self' blob: data:; object-src 'none'; "
    "base-uri 'none'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'"
)
