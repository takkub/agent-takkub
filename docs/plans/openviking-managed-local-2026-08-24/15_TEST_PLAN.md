# Test Plan

Unit:
- install state
- process ownership
- port selection
- URL override
- restart backoff
- redaction
- Windows process paths

Integration:
- create managed venv
- install OpenViking
- start server
- `/health`
- `/studio`
- index/search
- project isolation
- stop cleanly
- external process not killed

Platforms:
Windows primary + macOS + Linux CI where practical.
