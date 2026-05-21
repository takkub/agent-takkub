# Claude CLI Resume Behavior Test Results

Date: 2026-05-21  
claude version: 2.1.146 (Claude Code)  
Tested by: qa agent  
Test cwd: `/tmp/takkub-claude-resume-test` (→ `C:/Users/monch/AppData/Local/Temp/takkub-claude-resume-test`)

---

## Test 1: `--session-id` with fresh UUID

**Command:**
```bash
cd /tmp/takkub-claude-resume-test
uuid1="e7b87bba-89ec-4eba-b134-25a7bb459ea1"
claude --session-id "$uuid1" -p "say exactly: TEST_ONE_COMPLETE"
```

**Result:** ✅ SUCCESS  
**Evidence:**
- Exit code: 0
- Output: `TEST_ONE_COMPLETE`
- Session file created: `~/.claude/projects/C--Users-monch-AppData-Local-Temp-takkub-claude-resume-test/e7b87bba-89ec-4eba-b134-25a7bb459ea1.jsonl` (114 549 bytes)
- File content tail: `{"type": "last-prompt", "lastPrompt": "say exactly: TEST_ONE_COMPLETE", "sessionId": "e7b87bba-89ec-4eba-b134-25a7bb459ea1"}`

**Conclusion:** `--session-id <uuid4>` accepted immediately. Session file named exactly `<uuid>.jsonl` under the encoded-cwd folder.

---

## Test 2: `--resume` with valid UUID (round-trip)

**Command:**
```bash
cd /tmp/takkub-claude-resume-test
claude --resume "$uuid1" -p "what was my previous message? Reply with just: PREV=[the exact text]"
```

**Result:** ✅ SUCCESS  
**Evidence:**
- Exit code: 0
- Output: `PREV=say exactly: TEST_ONE_COMPLETE` — model recalled exact previous prompt
- Session file size after: 177 488 bytes (grew from 114 549 → 177 488, same file, no new file created)

**Conclusion:** `--resume <uuid>` resumes conversation with full history intact. New turns are appended to the existing `.jsonl` file.

---

## Test 3: `--resume` with missing/invalid UUID

**Command:**
```bash
cd /tmp/takkub-claude-resume-test
bad_uuid="dad56c9c-8eee-4900-81e4-f5c18476774d"  # never used
claude --resume "$bad_uuid" -p "echo test 3"
```

**Result:** ✅ EXPECTED BEHAVIOR  
**Evidence:**
- Exit code: 1
- Stderr: `No conversation found with session ID: dad56c9c-8eee-4900-81e4-f5c18476774d`
- No fallback to fresh session
- No new session file created

**Conclusion:** Missing UUID → hard exit 1 with descriptive error. **No silent fallback.** Orchestrator must handle this case explicitly (treat as "first spawn → use `--session-id` not `--resume`").

---

## Test 4: UUID format strictness

### 4a — Non-UUID string

**Command:** `claude --session-id "not-a-uuid" -p "echo test 4a"`  
**Result:** ✅ REJECTED CORRECTLY  
- Exit code: 1  
- Stderr: `Error: Invalid session ID. Must be a valid UUID.`

### 4b — Uppercase UUID (dashed)

**Command:** `claude --session-id "BCF7BF4B-C6EB-4F80-9D0F-C02A6B53FABD" -p "say: UPPERCASE_OK"`  
**Result:** ⚠️ ACCEPTED (unexpected)  
- Exit code: 0  
- Output: `UPPERCASE_OK`  
- Session file stored as `BCF7BF4B-C6EB-4F80-9D0F-C02A6B53FABD.jsonl` (uppercase filename preserved)  
- **Note:** First test run timed out at 15s due to slow init, but completed normally at 60s. No rejection.

### 4c — UUID without dashes

**Command:** `claude --session-id "1234567890abcdef1234567890abcdef" -p "echo test 4c"`  
**Result:** ✅ REJECTED  
- Exit code: 1  
- Stderr: `Error: Invalid session ID. Must be a valid UUID.`

### 4d — UUIDv1

**Command:** `claude --session-id "83ab6733-54c3-11f1-b323-7008104e2cfc" -p "say: UUIDV1_OK"`  
**Result:** ✅ ACCEPTED  
- Exit code: 0  
- Output: `UUIDV1_OK`  
- UUIDv1 treated same as UUIDv4

**Conclusion:** Validation requires dashed UUID format (RFC 4122). Version (v1/v4) and case (upper/lower) are NOT validated — only dash-separated hex groups. Use lowercase UUIDv4 as canonical form to avoid uppercase filename confusion.

---

## Test 5: Cross-cwd `--resume`

**Command:**
```bash
# Session created in /tmp/takkub-claude-resume-test
# Attempting resume from /tmp/different-cwd
cd /tmp/different-cwd
claude --resume "$uuid1" -p "what was my very first message?"
```

**Result:** ❌ FAILS — THIS IS CRITICAL  
**Evidence:**
- Exit code: 1
- Stderr: `No conversation found with session ID: e7b87bba-89ec-4eba-b134-25a7bb459ea1`
- No new folder created for `/tmp/different-cwd`
- Same UUID resumed successfully when back in `/tmp/takkub-claude-resume-test` (control run ✅)

**Conclusion:** `--resume <uuid>` searches ONLY in `~/.claude/projects/<encoded-cwd>/`. **Changing cwd between spawn and respawn breaks resume.** Cross-cwd resume is NOT supported by the CLI.

---

## Summary

| Risk | Result | Details |
|---|---|---|
| `--session-id` new UUID accepted | ✅ Works | Creates `<uuid>.jsonl` in cwd-scoped folder |
| `--resume` missing UUID | ✅ Hard exit 1 | `"No conversation found"`, no silent fallback |
| UUID format strictness | ⚠️ Partial | Dashes required; case and version (v1/v4) not validated |
| Cross-cwd resume | ❌ FAILS | `--resume` is CWD-scoped — different cwd = "not found" |
| Uppercase UUID filename | ⚠️ Warning | Stored with uppercase filename — case-sensitive FS risk |

---

## Recommendation for orchestrator impl

- **Use lowercase UUIDv4 only** (`uuid.uuid4()` in Python → lowercase by default). Uppercase is accepted but stores uppercase filename — avoid for portability on case-sensitive Linux filesystems.

- **`--resume` is CWD-scoped — lock cwd per session.** The orchestrator MUST spawn and respawn using the exact same `--cwd` for a given session ID. If cwd changes (e.g., user switches active project), treat as first spawn (generate new `--session-id`, do NOT use `--resume`).

- **Missing UUID → no fallback, hard exit 1.** Orchestrator must track which sessions have been spawned. On first spawn: use `--session-id <new_uuid>` (not `--resume`). On respawn (if session previously created in same cwd): use `--resume <uuid>`. If respawn cwd differs from original, fall back to `--session-id <new_uuid>`.

- **Option B fix is viable but requires cwd consistency.** The `--session-id` + `--resume` approach successfully prevents `--continue` CWD bleed (each teammate gets an isolated `.jsonl` file, Lead's session cannot be resumed by accident). The only constraint: orchestrator state must store `{role → (session_id, original_cwd)}` and enforce same cwd on respawn. Implementation sketch:

  ```python
  # spawn (first time)
  session_id = str(uuid.uuid4())  # lowercase v4
  subprocess.run(["claude", "--session-id", session_id, "--cwd", cwd, ...])
  store_session(role, session_id, cwd)

  # respawn
  sid, original_cwd = load_session(role)
  if cwd == original_cwd:
      subprocess.run(["claude", "--resume", sid, "--cwd", cwd, ...])
  else:
      # cwd changed — must start fresh
      new_sid = str(uuid.uuid4())
      subprocess.run(["claude", "--session-id", new_sid, "--cwd", cwd, ...])
      store_session(role, new_sid, cwd)
  ```
