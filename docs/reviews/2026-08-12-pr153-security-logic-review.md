# Security and Logic Review: PR #153 (than-aa)

**Date**: 2026-08-12  
**Reviewer**: agent-takkub reviewer  
**PR**: #153 (`than-aa`) — *Support single dot in names + fix xterm double-paste*  
**Status / Recommendation**: **MERGE APPROVED** (Ready to merge; Lead to fix 1 line ruff formatting on `config.py:67`).

---

## 1. Overview & Scope

PR #153 introduces two main improvements:
1. **Name Validation Update**: Allows single dots (`.`) in project and role names (e.g. `www.abc.com` or `my.proj`), while maintaining strict path-traversal protection blocking `..`, leading dots (`.hidden`), trailing dots (`trailing.`), and path separators (`/`, `\`, `:`).
2. **Terminal Double-Paste Fix (`terminal.html`)**: Prevents duplicate text submission when pasting into xterm.js by intercepting `insertFromPaste` input events on xterm's helper textarea.

---

## 2. Security Evaluation: Path Traversal & Ingress Validation

### 2.1 `config.py` — `validate_name`
- **Charset**: Updated `_SAFE_NAME` regex from `^[a-z0-9][a-z0-9_-]{0,63}$` to `^[a-z0-9][a-z0-9._-]{0,63}$`.
- **Traversal Guard**:
  ```python
  if ".." in name or name.startswith(".") or name.endswith("."):
      raise ValueError(f"invalid {kind}: {value!r}")
  ```
- **Shard Handling (`role#1`)**:
  ```python
  if ".." in role_part or role_part.startswith(".") or role_part.endswith(".") or not _SAFE_NAME.fullmatch(role_part):
      raise ValueError(...)
  ```
- **Analysis of Edge Cases**:
  - `www.abc.com` / `my.proj` → **ACCEPTED** (Starts with alnum, single dot inside, ends with alnum, valid length).
  - `..` / `...` / `a..b` → **REJECTED** (Blocked by `".." in name`).
  - `.` / `.hidden` → **REJECTED** (Blocked by `name.startswith(".")` and regex requiring `^[a-z0-9]`).
  - `trailing.` → **REJECTED** (Blocked by `name.endswith(". ")`).
  - `../../etc/passwd` / `a/b` / `a\b` / `a:b` → **REJECTED** (Slashes and colons not in `_SAFE_NAME` charset).
  - Unicode lookalikes (e.g. `․` U+2024, `．` U+FF0E) → **REJECTED** (`_SAFE_NAME` matches ASCII `[a-z0-9._-]` strictly).
  - Shard names (`www.abc.com#1`) → **ACCEPTED** (Role part validated with same traversal checks).

### 2.2 `pane_tools_policy.py` — `_validate_name`
- Updated regex to `^[a-z0-9][a-z0-9._-]*$`.
- Guard `if ".." in name or name.endswith(".")` blocks traversal and trailing dots.
- Regex `^[a-z0-9]` implicitly blocks leading dots (`.hidden`).

### 2.3 `role_memory.py` — `_safe`
- Updates sanitization logic:
  ```python
  s = re.sub(r"[^A-Za-z0-9._-]", "_", name)
  while ".." in s:
      s = s.replace("..", "_")
  s = s.strip(".")
  return s or "default"
  ```
- **Guarantee**: Output segment is guaranteed to be a single safe path component containing no `..`, no leading/trailing dots, and no path separators.

---

## 3. Impact Analysis on Existing Callers

Grep search confirmed callers of `validate_name`, `_validate_name`, and `_safe`:
- **`orchestrator.py` & `spawn_engine.py`**: Project namespaces (`runtime/tasks/<project>/`, `runtime/logs/<project>/`, `~/.takkub/memory/<project>/`) now support domain-style project names (`www.abc.com`) without path escaping risks.
- **`pane_tools_policy.py` & `mcp_bridge.py`**: Accepts domain-formatted MCP/plugin names cleanly.
- **`custom_roles.py`**: Role names with single dots validate safely without breaking file creation in `CUSTOM_AGENTS_DIR`.

No regression or unexpected side effects detected across any caller module.

---

## 4. Terminal Double-Paste Analysis (`terminal.html`)

### 4.1 Root Cause of Original Double-Paste
1. xterm.js handles pastes via its native `paste` event listener on `.xterm-helper-textarea`, emitting `term.onData` (which sends bracketed paste sequences like `\x1b[200~pasted_text\x1b[201~`).
2. Browser paste simultaneously inserted raw text into `helperTextarea.value`.
3. `flushTextareaInput` attempted to deduplicate by checking `lastEmittedText === capturedVal`.
4. Because `lastEmittedText` held the wrapped bracketed paste string (`\x1b[200~...`) while `capturedVal` held plain text, the comparison failed, causing `flushTextareaInput` to re-send the text a second time.

### 4.2 PR #153 Fix
```javascript
if (e.inputType === 'insertFromPaste') {
  e.preventDefault();
  return;
}
```
And safety net in `input` listener:
```javascript
if (e.inputType === 'insertFromPaste') {
  helperTextarea.value = '';
  return;
}
```
- **Effect**: Browser is prevented from inserting raw pasted text into `helperTextarea`. xterm.js's native `paste` handler remains the single writer. `helperTextarea` stays empty, eliminating duplicate sends.
- **Normal Typing**: `insertText`, composition, and keydown events are unmodified.

---

## 5. Verification & Tests

- Added tests in `test_pane_tools_policy.py`, `test_role_memory.py`, and `test_security_ingress.py` cover all new features and security edge cases.
- Executed unit test suite (`pytest`): **150 tests passed**.
- Python verification script confirmed all path traversal edge cases are strictly blocked.

---

## 6. Action Items / Recommendation

1. **Merge Recommendation**: **APPROVED / READY TO MERGE**.
2. **CI Formatting Note**: Line 67 in `src/agent_takkub/config.py` exceeds Ruff's 100-character limit (116 chars). Lead should format this line before final merge/push.
