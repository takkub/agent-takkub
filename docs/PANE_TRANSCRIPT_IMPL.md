# Pane Transcript Capture — Implementation Notes

**Date:** 2026-05-20  
**Feature:** Tee raw PTY bytes to a transcript file per pane spawn so codex/gemini/future TUI pane output survives the 2.5s auto-close after `takkub done`.

---

## What was implemented

### 1. `PtySession.spawn()` — new `transcript_path` kwarg

`src/agent_takkub/pty_session.py`

- `__init__` initialises `self._transcript = None`
- `spawn(argv, cwd, env, transcript_path=None)` — if `transcript_path` given, opens the file `"wb"` after the process is alive. Open failure → logs a warning, sets `_transcript = None`, **does not raise** (PTY still runs)
- `_on_bytes(data)` — after `bytesIn.emit(data)`, writes + flushes to `_transcript` if set. Any write error → sets `_transcript = None` (stop trying, don't block PTY)
- `terminate()` — closes and clears `_transcript` before returning

### 2. `Orchestrator.spawn()` — compute path and pass to session

`src/agent_takkub/orchestrator.py`

New module-level helper `_build_transcript_path(project_ns, role_name) -> str`:
- Returns `runtime/sessions/<YYYY-MM-DD>/<project>/<role>-<HHMMSS>.transcript.log`
- Creates the parent directory (`parents=True, exist_ok=True`)
- Called at all three spawn call sites: claude, codex, gemini

At each call site:
```python
_t_path = _build_transcript_path(project_ns, role_name)
pane._transcript_path = _t_path          # stored for done() to pick up
session.spawn(..., transcript_path=_t_path)
```

### 3. Decision log — `## Transcript` section

`_render_decision_note(project, role, note, now, transcript_path=None)`

When `transcript_path` is set, appends to the markdown body:

```markdown
## Transcript

Raw byte stream (with ANSI): `runtime/sessions/<date>/<project>/<role>-<HHMMSS>.transcript.log`

ดูดิบ: `cat runtime/sessions/...`  
ดูแบบมีสี: `less -R runtime/sessions/...`
```

Path is relative to `REPO_ROOT` via `pathlib.Path.relative_to()`. Falls back to absolute if the transcript lives outside the repo.

`_save_decision_note(project, role, note, now, transcript_path=None)` — forwards `transcript_path` to `_render_decision_note`.

`done()` — reads `transcript_path = getattr(pane, "_transcript_path", None)` and passes it through.

### 4. `.gitignore`

No change needed — `runtime/` is already in `.gitignore` and covers all files under `runtime/sessions/` including `*.transcript.log`.

### 5. Tests

`tests/test_pane_transcript.py` — 7 unit tests across 3 classes, no real PTY spawns (winpty is Windows-only and slow):

| Class | Test | What it verifies |
|---|---|---|
| `TestTranscriptOpen` | `test_transcript_handle_set_on_valid_path` | file handle is opened and not closed |
| `TestTranscriptOpen` | `test_transcript_stays_none_when_path_is_none` | `None` path → no file opened |
| `TestOnBytesTranscriptTee` | `test_bytes_written_to_transcript` | `_on_bytes` writes + flushes raw bytes |
| `TestOnBytesTranscriptTee` | `test_no_transcript_no_write` | `None` handle → no crash, no file |
| `TestOnBytesTranscriptTee` | `test_write_error_nulls_transcript` | OSError → `_transcript` set to `None` |
| `TestTerminateClosesTranscript` | `test_terminate_closes_and_clears` | handle closed + `_transcript = None` |
| `TestTerminateClosesTranscript` | `test_terminate_no_transcript_is_safe` | `None` handle → `terminate()` safe |

---

## File layout after done event

```
runtime/sessions/2026-05-20/agent-takkub/
├── codex-143022.transcript.log    ← raw PTY bytes (this PR)
├── codex-143022.md                ← decision log (references transcript)
├── gemini-151045.transcript.log
└── gemini-151045.md
```

To inspect a transcript:
```bash
less -R runtime/sessions/2026-05-20/agent-takkub/codex-143022.transcript.log
```
