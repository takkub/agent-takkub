# Bug / Gap Register

## P0
### BUG-001 File Preview URL/path normalization
Controller stores local filesystem path.
WebEngine navigation sees `file://...`.
Raw string equality can block its own file navigation.

Fix canonical local-file comparison through QUrl -> toLocalFile -> Path.resolve.

## P1
### BUG-002 Shared Preview not rebound on project switch
One global Preview WebView is correct for RAM, but visible content must track active project.

### BUG-003 Project close leaves Preview state
Explicitly clear controller state when project closes.

### BUG-004 Editor save may lose POSIX executable/mode bits
Preserve `stat.S_IMODE`.

### BUG-005 Invalid UTF-8 can be silently transformed
Never `errors="replace"` for editable source buffers.

### BUG-006 Revise routes only to Lead
Send structured feedback to live Designer as well; Lead remains orchestration/audit fallback.

## P2
### BUG-007 Deleted Git change tries to open missing file before diff
Implement diff-only flow.

### BUG-008 Rename diff loses old path
Keep `old_path` from porcelain-v2 rename record.

### GAP-009 Multi-root project Git tracks first root only
Support multiple distinct git repositories.

### GAP-010 Explorer Ask Agent disabled
Implement file-level Ask Agent.

### GAP-011 Git ignore implementation not fully Git-compatible
Use Git as source-of-truth where available.

### GAP-012 Real browser/GUI coverage incomplete
Visual acceptance for Monaco highlighting, diff, HTML Preview, URL Preview, viewport presets.

## Future completeness
### GAP-013 OpenViking adapter absent
### GAP-014 21st.dev real client absent
### GAP-015 Figma real client absent
### GAP-016 Penpot real client absent
### GAP-017 unified Design Context source not formalized
### GAP-018 retrieval observability could be improved
