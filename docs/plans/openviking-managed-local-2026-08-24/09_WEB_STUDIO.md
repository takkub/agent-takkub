# Local Web Studio

OpenViking's pip/pipx server provides Web Studio at:
`http://127.0.0.1:<port>/studio`

Recommended v1:
[ Open Studio ] → local browser.

Optional v2:
embed in a fixed QWebEngine dock called `Knowledge Studio`.

Rules:
- loopback only
- no privileged QWebChannel
- one fixed WebView
- no painted-WebView reparenting.
