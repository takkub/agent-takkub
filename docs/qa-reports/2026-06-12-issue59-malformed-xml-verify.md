# QA Verify Report — Issue #59: Malformed Tool-Call XML Detection

**Date:** 2026-06-12  
**Branch:** main (post-fix)  
**Verifier:** qa  
**Scope:** pty_session.py `has_unparsed_tool_call()`, orchestrator.py `_maybe_surface_malformed_xml()` + wiring

---

## Summary

| # | Check | Result |
|---|---|---|
| 1 | Full pytest suite (2002 passed, 2 skipped) | ✅ PASS |
| 2 | False-positive analysis — prose/ready-prompt/build output | ✅ PASS (with 1 gap noted) |
| 3 | Lead branch fires — check placed before `continue` | ✅ PASS |
| 4 | Cooldown 60 s — nudge doesn't spam | ✅ PASS |
| 5 | Idle-reminder / TTY-block / rate-limit behaviour unchanged | ✅ PASS |

No blocking bugs found. One coverage gap (low-risk false positive) noted below.

---

## 1. Full Pytest Suite

```
2002 passed, 2 skipped in 33.81s
```

No regression vs. expected baseline. The 2 skipped are pre-existing (unrelated to this change).  
New test files:
- `tests/test_pty_ready_prompt.py` — 34 tests (includes `TestHasUnparsedToolCall`, 12 cases)
- `tests/test_idle_watchdog.py` — 19 tests (includes `TestMalformedXmlWatchdog`, 5 cases)

All 53 tests in these two files pass.

---

## 2. False-Positive Analysis

### Regex under test
```python
_MALFORMED_XML_RE = re.compile(
    r"<\s*/?\s*(antml:)?(invoke|parameter|function_calls)\b",
    re.IGNORECASE,
)
```

### Cases that correctly do NOT fire

| Input | Result |
|---|---|
| `"I will now invoke the Bash tool."` | no-match ✅ |
| `"The parameter value is the command string."` | no-match ✅ |
| `"No errors found in parameter handling"` | no-match ✅ |
| `"function_calls in this module handle X"` | no-match ✅ |
| `"bypass permissions"` (claude ready) | no-match ✅ |
| `"OpenAI Codex (v1.2.3)"` (codex ready) | no-match ✅ |
| `"Type your message or @path"` (gemini ready) | no-match ✅ |
| XML pushed >10 rows into scrollback | no-match ✅ (cursor-window isolation works) |

### Cases that correctly DO fire

| Input | Result |
|---|---|
| `<invoke name="Bash">` | MATCH ✅ |
| `</invoke>` | MATCH ✅ |
| `<parameter name="command">ls</parameter>` | MATCH ✅ |
| `<function_calls>` / `</function_calls>` | MATCH ✅ |
| `< invoke>` (spaces inside) | MATCH ✅ |
| `</ invoke>` (spaces in closing) | MATCH ✅ |

### ⚠️ Coverage gap — `<parameter>` in non-tool-call XML context

The tag name `parameter` is generic and could appear in any XML document. If an agent outputs or quotes XML that happens to use a `<parameter>` element (e.g., describing an API schema, discussing non-Claude XML formats, or reviewing code that uses `<parameter>` in config XML), the detector **will fire** even though nothing is wrong.

- **Severity:** Low (nudge is informational, non-destructive; pane still works normally after receiving it)
- **Trigger condition:** pane must be at ready-prompt AND screen must show `<parameter` tag from any source
- **Missing test:** No test covers `has_unparsed_tool_call()` returning `None` when screen contains `<parameter>` in a non-tool-call XML block
- **Recommendation:** Add a test case like:
  ```python
  def test_generic_xml_parameter_tag_in_api_schema_fires(self) -> None:
      # document that this IS a false positive and the team accepts it as
      # a design tradeoff (the harness invariant makes it rare in practice)
      s = _feed_screen("<parameter key='timeout'>30s</parameter>")
      # By design: any <parameter tag as visible text fires the detector.
      # This is accepted because (a) it's informational-only, and (b) the
      # harness invariant makes true false-positives extremely rare on these panes.
      assert s.has_unparsed_tool_call() is not None
  ```

This documents the known limitation rather than leaving an implicit assumption. Recommend Backend adds the test (explicit acceptance comment) before the next release.

---

## 3. Lead Branch Fire

**Location:** `orchestrator.py` lines 4655–4660

```python
if name == LEAD.name:
    # Issue #59: malformed-tool-call detection covers Lead too
    if pane.session and pane.session.is_alive and pane.session.is_at_ready_prompt():
        self._maybe_surface_malformed_xml(key, name, project_name, pane, now)
    continue   # <— after the call, not before
```

The check runs **before** `continue`. A previous Lead-exempt pattern would have placed the `continue` first and skipped the check entirely. Here it does not.

**Guard:** Lead only gets nudged when `is_at_ready_prompt()` is True (idle). A busy Lead (returning `False`, e.g., tool call in progress) is not nudged — `test_lead_not_busy_no_nudge` validates this.

**Test coverage:** `TestMalformedXmlWatchdog.test_lead_idle_with_xml_gets_nudge` exercises this path and passes. ✅

---

## 4. Cooldown Behaviour

`PaneState.malformed_xml_notice_ts` initialised to `0.0` (dataclass default).

`_maybe_surface_malformed_xml()`:
```python
if now - ps.malformed_xml_notice_ts < MALFORMED_XML_NOTICE_COOLDOWN_S:
    return
```

- First call at any `now`: `now - 0.0` is always ≥ 60, so the first nudge fires immediately on detection.
- Subsequent calls within 60 s: suppressed.
- After 60 s elapses: next detection fires again.

**Test coverage:** `test_nudge_cooldown_prevents_spam` (passes). Also `test_lead_idle_with_xml_gets_nudge` confirms first-tick fire. ✅

---

## 5. Pre-Existing Behaviours Unchanged

All pre-existing watchdog paths (idle reminder, TTY-block surface, rate-limit suppression) unchanged in logic and tests:

- `TestIdleWatchdog` (7 tests) — all pass
- `TestTtyBlockIdleWatchdog` (4 tests) — all pass
- `TestIdleResetHooks` (2 tests) — all pass
- No interference between XML check and idle-done reminder: the XML check runs at line 4719 (before idle streak threshold check), but the idle reminder at 4727–4736 still fires independently if the idle streak threshold is crossed. The two are additive, not exclusive.

---

## Action Items for Backend

1. **Add explicit false-positive acceptance test** for `<parameter>` in non-tool-call XML context (see §2 gap). Low priority — can go in next batch, not a blocker for this release.

No other action items. Change is safe to ship.
