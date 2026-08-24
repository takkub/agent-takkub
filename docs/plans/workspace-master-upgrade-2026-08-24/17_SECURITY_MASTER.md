# Security Master Checklist

## Editor
- project-root containment
- symlink/junction escape
- special/device file rejection
- size cap
- binary handling
- strict encoding
- conflict-safe writes
- atomic replace
- permission preservation

## Preview
- no privileged bridge
- loopback URL only unless explicitly expanded later
- same-origin navigation
- local artifact containment
- canonical file URL mapping
- popup/new-window default deny
- clear project identity

## IPC
- preserve pane token gates for preview/design writes
- derive project/role from token where possible
- no spoofed from_project

## MCP
- Capability Hub permission controls
- external design/reference data untrusted
- no bypass around cmd_guard/PermissionEngine

## OpenViking
- sidecar boundary
- secrets excluded
- curated allowlist
- API key stored through existing secret mechanism
