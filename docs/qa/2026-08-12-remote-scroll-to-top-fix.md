# QA Verification Report: Remote/Mobile UI Scroll-to-Top Fix (#104)

**Target File:** `src/agent_takkub/remote/static/app.js`
**Fix:** Added unconditional `log.scrollTop = log.scrollHeight` after `lead.messages.forEach` loop in `renderSelectedProject()`.

---

## Test Results Summary

| Test # | Description | Status | Details |
|---|---|---|---|
| **Test 1** | Open remote UI & load project with long history | **PASS** | `scrollTop` landed at `11704` (`scrollHeight`: 11987, `clientHeight`: 283). Verified scrolled to latest message (bottom). |
| **Test 2** | Incremental streaming scrollback protection (`atBottom` heuristic) | **PASS** | When scrolled up (`scrollTop` = 50), streaming new message preserved reading position. When scrolled down to bottom, streaming auto-scrolled to bottom. |
| **Test 3** | Switch project/tab back and forth | **PASS** | Switching between Project A (100 msgs) and Project B (60 msgs) landed at bottom every time (`isAtBottom`: true). |

---

## Verification Evidence & Artifacts
- **Evidence Screenshot:** `runtime/exports/2026-08-12/agent-takkub/remote_scroll_verify.png`
- **Automated Verification Script:** `verify_scroll.py` (executed via Chrome CDP against running remote instance on port 9999)

**Overall Status:** **PASS (ALL 3 TESTS VERIFIED)**
