# Monaco Editor Final Spec

## Existing core
- one app-wide WebView
- local Monaco
- multiple tabs
- Ctrl+S
- diff
- conflict handling
- Ask Agent selection

## Required hardening

### Encoding
Editable:
- UTF-8
- UTF-8 BOM

Invalid UTF-8:
- read-only
- explicit unsupported encoding banner
- no normal save

### File metadata
Preserve POSIX permission/mode bits on atomic replace.
Do not promise ACL/xattr preservation unless implemented and tested.

### Conflict
Keep:
- mtime_ns
- size
- SHA256
- explicit Keep Mine
- no silent overwrite

### Save
- same-dir temp
- flush/fsync
- mode restore
- replace
- fresh stat/hash state

### Safety
- path confinement
- size cap
- binary detection
- device/special-file rejection
