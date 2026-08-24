# MASTER PROMPT — Managed Local OpenViking

Inspect CURRENT main first.

Goal:
OpenViking is a Takkub-managed LOCAL HTTP service.

Requirements:
- no Docker
- no manual terminal start
- localhost only
- isolated managed venv
- one-click install
- auto-start with Cockpit when enabled
- owned process auto-stop
- existing external local server detected but never killed
- health/restart/backoff
- setup wizard
- update/repair/remove
- Open Studio
- fail-open if unavailable
- preserve current strict project-scope logic
- preserve Brain/Conversation/Graft responsibilities
- no public tunnel
- do not touch Phase 10/#362

Use existing Remote module as an architectural pattern only, not its public exposure behavior.

Final report:
HEAD/version, files, install flow, lifecycle, UI evidence, real local health/studio/index/search, failure/rollback, Windows/macOS tests, CI, confirmation Docker unnecessary, confirmation #362 untouched.
