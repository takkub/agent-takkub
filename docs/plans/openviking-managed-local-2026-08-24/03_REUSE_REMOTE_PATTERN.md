# Reuse Remote Pattern

Yes: copy the architectural separation of the existing Remote feature:
- config
- diagnostics
- settings
- web/runtime lifecycle

But DO NOT copy:
- public tunnel
- public bind
- remote exposure

OpenViking is localhost-only.

Recommended manager API:
`ensure_installed/start/stop/restart/health/open_studio/update/repair/remove`.
