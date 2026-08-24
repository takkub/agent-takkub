# Live Preview Final Spec

Modes:
- loopback URL
- approved local HTML/HTM artifact

## Security
- no privileged QWebChannel in arbitrary preview page
- URL mode same-origin
- local file containment
- external navigation blocked unless policy changes
- file URL normalized canonically

## Project state
```text
project -> PreviewState
```

### On active project switch
- if state exists: display
- else: hide/empty dock

### Background project publishes
Default:
- store state
- do NOT replace active project's visible Preview
- show a badge/notification if helpful

### Project close
- remove its preview state
- if displayed: hide/clear PreviewHost

### Header
Display:
- project
- artifact title/status
- target
- device mode

## Device modes
desktop/tablet/mobile.
