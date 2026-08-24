# Process Lifecycle

Startup:
- OpenViking disabled → zero-cost
- enabled → health current URL
- healthy existing process → use it, mark external/not-owned
- unavailable + managed install exists → spawn local server
- poll `/health` with bounded timeout
- failure → Takkub continues without OpenViking

Shutdown:
- if `started_by_takkub=True`, terminate gracefully then bounded kill
- external process is never killed

Crash:
- bounded restart backoff
- cap retries
- then disable for session and fail-open.
