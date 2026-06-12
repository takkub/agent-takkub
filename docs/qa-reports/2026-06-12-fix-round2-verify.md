---
date: 2026-06-12
role: qa
subject: Fix Round 2 — Full-system-review verification
verdict: PASS
---

# VERDICT: PASS

Full suite: **1959 passed, 2 skipped** (baseline 1947 + 12 new edge-case tests added this run, 0 failures).

---

## 1. Full regression baseline

```
python -m pytest
1947 passed, 2 skipped in 48.27s   ← baseline before new tests
1959 passed, 2 skipped in 36.07s   ← after adding 12 gap tests
```

No existing tests broken by any of the 5 fix areas.

---

## 2. Targeted smoke — 5 fix areas

### Fix 1 · Codex exit stale-session guard (`_on_codex_exit`)

**Implementation** (`orchestrator.py:2154`):
```python
if _pane_cdx is not None and _pane_cdx.session is not session:
    return
```

**Existing tests** (`test_regression_findings_2026_06.py::TestCodexExitStaleSessionGuard`):
- Current session fires `_on_session_exit` ✅
- Stale session (pane already on session B) → silently dropped ✅
- Exit-after-close (session=None) → dropped ✅

`TestStaleSessionExitDropped` also covers the lambda closure pattern used by shell, gemini, and claude `processExited` connections.

**Gap found:** `AgentPane._on_exit(code, gen)` generation guard was not tested.
**Gap closed** by `TestAgentPaneOnExitGenerationGuard` (5 cases — see §3).

---

### Fix 2 · Token lifecycle (revoke on spawn-fail / session-exit / explicit close, revoke-old before register-new)

**Implementation spans 4 spawn branches + 2 revocation paths:**

| Branch | Pre-revoke old | Revoke on spawn-fail | Revoke on session-exit | Revoke on close() |
|---|---|---|---|---|
| Shell (`orchestrator.py:1455`) | ✅ | ✅ L1482 | via `_on_session_exit` L2244 | via `close()` L3405 |
| Gemini (L1537) | ✅ | ✅ L1568 | ✅ | ✅ |
| Codex (L1598) | ✅ | ✅ L1654 | ✅ | ✅ |
| Claude (L1815) | ✅ | ✅ L2122 | ✅ | ✅ |

**Existing tests** (`test_regression_findings_2026_06.py::TestTokenRevocationOnSessionExit`):
- Token revoked after session exit (crash path via `_on_session_exit`) ✅
- Only matching token revoked; sibling role's token survives ✅
- Revoked token rejected by server (end-to-end via `_dispatch`) ✅
- Old token rejected, new token accepted after respawn ✅

**Gap found:** `close()` path revocation (L3403–3408) not covered.
**Gap closed** by `TestTokenRevocationOnClose` (see §3).

---

### Fix 3 · `_delayed_enter()` session capture (11 call sites)

**Implementation** (`orchestrator.py:494`):
```python
def _delayed_enter(pane, session, delay_ms):
    QTimer.singleShot(delay_ms, lambda: pane.session is session and pane.session.write(b"\r"))
```

**Call sites verified** (11 total, all migrated from inline `QTimer.singleShot`):
`L2519, L2590, L3231, L3301, L3483, L3534, L4918, L4939, L5027, L5067, L5076`

**Gap found:** Lambda's session-identity guard had no unit test.
**Gap closed** by `TestDelayedEnterSessionGuard` (3 cases — see §3).

---

### Fix 4 · TCP framing hardening

**Implementation** (`cli_server.py`):
- `_MAX_FRAME_BYTES = 64 KiB` — `readLine(maxSize)` bounded, oversized frame rejected
- `bytesAvailable()` check for unterminated oversized frame (no newline)
- `_open_connections` dict: tracks all sockets until disconnect
- Activity timestamp updated per valid frame (reaper gives fresh idle window)
- `_MAX_CONNECTIONS = 32` cap on `_on_new_connection`
- `_reap_idle_connections()` per-second QTimer

**Existing tests** (`TestFrameValidation`, `TestTcpConnectionTracking`):
- Oversized frame (with newline) → rejected + disconnected ✅
- JSON array / scalar → rejected ✅
- Non-string cmd / auth fields → rejected ✅
- Invalid JSON → rejected ✅
- Unterminated oversized frame (no newline, `bytesAvailable > _MAX_FRAME_BYTES`) → rejected ✅
- Socket stays tracked in `_open_connections` after first valid frame ✅
- Activity timestamp refreshed after valid frame ✅
- Idle socket reaped by `_reap_idle_connections()` ✅

**Gap found:** `_MAX_CONNECTIONS` cap enforcement in `_on_new_connection` not tested.
**Gap closed** by `TestConnectionCap` (2 cases — see §3).

**Trust-boundary edge case verified (manual inspection):**
A client that sends one valid frame and then holds the socket still gets reaped at `_IDLE_CONNECTION_TIMEOUT_S` seconds — timestamp is refreshed on the first frame but the next window starts from that moment, not from connect time. Covered by existing `test_last_activity_updated_after_valid_frame`.

---

### Fix 5 · `_sanitize_pane_text` — strip CR, preserve LF

**Implementation** (`orchestrator.py:467`):
```python
text = text.replace(_PASTE_END, "").replace(_PASTE_START, "")
text = text.replace("\x1b", "")
text = text.replace("\r", "")   # strip bare CR
# LF intentionally NOT removed
return text
```

**Existing tests** (`TestSanitizePaneText`, 8 cases):
- Strip `\x1b[200~` / `\x1b[201~` bracketed-paste markers ✅
- Strip bare `\x1b` ✅
- Strip `\r` ✅
- Preserve `\n` ✅
- Short payload with ESC markers ✅
- Long payload (70,000 bytes) with ESC markers ✅
- Combined CR + ESC in one payload ✅

**No gaps** — coverage is complete. Confirms LF is preserved intentionally (multi-line task bodies in bracketed-paste mode don't trigger submission).

---

## 3. New edge-case tests written (`tests/test_fix_round2_edge_cases.py`)

12 new tests, all green.

| Class | Test | Gap covered |
|---|---|---|
| `TestDelayedEnterSessionGuard` | `test_write_fires_when_session_unchanged` | Fix 3 |
| | `test_write_skipped_when_session_replaced` | Fix 3 |
| | `test_write_skipped_when_pane_session_is_none` | Fix 3 |
| `TestAgentPaneOnExitGenerationGuard` | `test_current_gen_fires_set_state` | Fix 1 |
| | `test_stale_gen_drops_signal` | Fix 1 |
| | `test_none_gen_legacy_compat_fires` | Fix 1 |
| | `test_unexpected_exit_sets_exited_state` | Fix 1 |
| | `test_expected_exit_sets_empty_state` | Fix 1 |
| `TestTokenRevocationOnClose` | `test_token_revoked_after_explicit_close` | Fix 2 |
| | `test_only_closed_role_token_revoked` | Fix 2 |
| `TestConnectionCap` | `test_connection_accepted_below_cap` | Fix 4 |
| | `test_connection_rejected_at_cap` | Fix 4 |

---

## 4. Trust-boundary items (fixes 2 + 4) — elevated scrutiny

### Fix 2: Token auth path
- `_dispatch` for `done`/`send` calls `secrets.compare_digest` on the lead token — timing-safe ✅
- Pane token lookup is a plain dict membership check (token is a 256-bit random URL-safe string — pre-image resistance means a 1-in-2^256 brute-force probability; timing side-channel on dict lookup is irrelevant at this length) ✅
- Token is revoked in 3 independent paths: spawn-fail (except block), session-exit (`_on_session_exit`), explicit close (`close()`) — no window where a crashed pane's token lingers ✅
- `end-session` added to `_LEAD_ONLY_CMDS` — teammate panes cannot call it even with a valid pane token ✅

### Fix 4: TCP ingress
- Bound to loopback only (`QHostAddress.LocalHost`) — no LAN exposure ✅
- 64 KiB per-frame cap limits Qt main-thread parse cost for a single malformed packet ✅
- 32-connection cap prevents socket-table exhaustion from a local process opening many connections ✅
- 30-second idle timeout prevents unbounded `QSocketDevice` read-buffer growth ✅
- `ANTHROPIC_AUTH_TOKEN` excluded from default pane env allowlist (`pane_env.py`) — opt-in only via `TAKKUB_PANE_ENV_ALLOW` ✅

---

## 5. Items not tested (known limitations)

| Item | Reason |
|---|---|
| `pty_session.py` `terminate()` `AttributeError`/`RuntimeError` guard | Requires a QObject created via `__new__` without `__init__`; the guard is defensive dead-code in normal operation |
| `chatlog_scanner.scan_hot_md_metrics` (new function) | Covered by existing `test_chatlog_scanner.py` patterns; new function is additive (no changed behaviour) |
| `pane_env._build_pane_env` `TAKKUB_PANE_ENV_ALLOW` opt-in | Tested in `test_orchestrator_env_allowlist.py` indirectly; direct unit test acceptable future work |

---

*QA report generated by qa role · 2026-06-12*
