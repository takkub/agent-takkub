# Remote Reply Pipeline Cross-Check

Date: 2026-07-07
Role: codex cross-check reviewer
Scope: `src/agent_takkub/remote/notify.py`, `src/agent_takkub/remote/http_server.py`

Targeted verification run:

```text
pytest -q tests/test_remote_notify.py tests/test_remote_http_server.py
..........................................................               [100%]
```

## Findings

### High: JSONL file creation after first resync is never retried

- Location: `src/agent_takkub/remote/notify.py:176-179`
- Impact: A Lead pane can have a known `session_uuid` before Claude has created/flushed `<config_dir>/projects/*/<uuid>.jsonl`. `_resync()` resolves once on `statusChanged`; if the glob returns `None`, no `_Tail` is stored. The poll timer only calls `_poll_all()` over existing tails, so it never retries `_resolve_jsonl()`. Unless another unrelated `statusChanged` fires later, all Lead assistant replies for that session are missed.
- Why this matters for the fixed pipeline: the new source is JSONL, and the live proof only covered appending after a tail already existed. This edge case can still reproduce "mobile Lead reply does not pop" on fresh spawn/resume timing.
- Repro:
  1. Create `LeadNotifier`.
  2. Set `_pane_state[_exit_key("proj", "lead")].session_uuid = "uuid-1"` and put `"lead"` in `_panes_by_project["proj"]`.
  3. Emit `statusChanged` while no `uuid-1.jsonl` exists. `_resync()` reaches lines 176-179 and continues without storing a tail.
  4. Create `projects/<encoded>/uuid-1.jsonl` and append an assistant text record.
  5. Let the 500 ms poll run or call `_poll_all()`: no event is pushed because `_tails` is still empty.
- Suggested fix: keep unresolved `(project_ns, session_uuid)` in state and retry resolution during each poll, or have `_poll_all()` call `_resync()` before polling. When first resolving a previously missing file, use the live-tail offset policy carefully; see the next finding.

### Medium: Evicted SSE handlers are not reliably woken when their queue is full

- Location: `src/agent_takkub/remote/http_server.py:177-186`; same pattern in `close_all()` at lines 220-226
- Impact: `register()` pops the oldest client from `_clients`, then tries `evicted.put_nowait((None, None))`. If that queue is full, the sentinel is dropped. The evicted handler remains alive, drains any already queued events to the old socket, then blocks on `q.get(timeout=15s)` forever/repeatedly because the queue is no longer registered and will not receive future pushes. This does not double-unregister or corrupt `_clients`, but it defeats the "wake its handler" guarantee for the exact stale/reloaded clients eviction is meant to clear.
- Repro:
  1. Register `_MAX_SSE_CLIENTS` clients.
  2. Fill the first client's queue to `_SSE_QUEUE_MAXSIZE` with `push()` calls or direct queue puts.
  3. Register one more client.
  4. The oldest queue is removed from `_clients`, but `put_nowait((None, None))` raises `queue.Full` and is ignored. `oldest.get_nowait()` returns an ordinary event, not `(None, None)`, and the old handler is not forced closed.
- Suggested fix: use the same drop-oldest loop used by `push()` before inserting the sentinel, or under a helper like `_force_wake(q)` that drains one item on `queue.Full` and retries. Apply it to both eviction and `close_all()`.

### Low: EOF-at-discovery can lose a record if discovery lands mid-line

- Location: `src/agent_takkub/remote/notify.py:180-187` and `src/agent_takkub/remote/notify.py:207-218`
- Impact: `_resync()` initializes a new tail at `path.stat().st_size`. That is safe if the file is between complete JSONL records, and if bytes are appended after the stat, the next poll reads them. It is not safe if Claude is currently writing a JSON object and the file does not yet end in `\n`: the offset can land in the middle of the future line. When the newline arrives, `_poll_one()` reads only the suffix, attempts `json.loads()` on malformed JSON, catches `ValueError`, and permanently drops the assistant record.
- Repro:
  1. Create `uuid-1.jsonl` containing the first half of an assistant JSON object with no newline.
  2. Emit `statusChanged`; `_resync()` stores `offset` at the half-line EOF and `partial=b""`.
  3. Append the rest of the JSON object plus `\n`.
  4. `_poll_one()` reads only the second half, fails JSON parse at lines 215-218, and no SSE `lead` event is pushed.
- Suggested fix: when initializing a new tail at EOF, check whether the file ends with `\n`. If not, seek back to the byte after the previous newline and seed `partial` with the existing partial bytes, or defer tail creation until the first complete line boundary. This preserves live-only semantics for completed backlog while not splitting an in-flight record.

## Cross-Check Notes

- Offset race between `stat()` and storing `_Tail`: bytes appended after `stat()` but before `_Tail` creation are not missed; the stored offset is the older size, so the next poll reads those bytes. The unsafe case is specifically statting a file whose current EOF is already inside an incomplete JSONL record.
- Eviction double-unregister: no correctness bug found. Eviction removes the queue from `_clients`, and the handler's `finally` calls `unregister(q)`, which is idempotent because it filters by object identity.
- Multi-project isolation: no leak found in the reviewed path. `_tails` is keyed by `project_ns`, each `_Tail` has its own `offset` and `partial`, and `push()` filters by the client ticket's project namespace.
- Partial-line reassembly: normal split-line cases are handled correctly. Multiple complete records in one read are all processed; a trailing incomplete line is retained in `tail.partial` until a later poll supplies the newline.
- Session respawn/resume: uuid changes correctly drop old tails before polling stale files. The remaining risk is the new session's first JSONL file missing at resync time, or the EOF-mid-line case above.
- 500 ms polling window: no loss found for multiple assistant records written in one poll interval. `_poll_one()` reads the full byte delta and iterates every newline-delimited record.

## Summary

Blocker: 0
High: 1
Medium: 1
Low: 1
