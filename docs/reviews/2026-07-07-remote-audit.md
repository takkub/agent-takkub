# Remote PWA subsystem — security + correctness audit

- **Date:** 2026-07-07
- **Reviewer:** reviewer (code review — quality / security / code-level perf)
- **Scope:** `src/agent_takkub/remote/{api,auth,http_server,notify,settings_dialog}.py` + `static/{app.js,index.html,sw.js}` (git diff vs `main`)
- **Method:** manual read of full trust-boundary flow + empirical repro of the top finding. No Snyk (pure-stdlib subsystem, no new deps in the diff).

## Verdict

**1 HIGH, 1 MEDIUM, 2 LOW. No Critical.**

Almost every finding from the previous two audit rounds (H1 password global-bool, H2 SSE multiline framing, M1 socket timeout, L2 CSP, L4 password min-length, cross-project H-A leak, SSE named-event mismatch) is **verified fixed** in this branch. The one High below is a *new* logic hole created by the H1 fix's routing — the password brute-force lockout is silently defeated on the exact path it's meant to protect.

### Findings by severity

| # | Sev | File:line | One-liner |
|---|-----|-----------|-----------|
| H1 | **HIGH** | `auth.py:107-133,173-184` + `http_server.py:354-356` | Password lockout defeated: valid-token holder brute-forces the password unthrottled |
| M1 | **MEDIUM** | `static/app.js:552-565` | Markdown link renderer: `"` unescaped → `href` attribute injection (only CSP prevents XSS) |
| L1 | **LOW** | `config.py:65-72` | Unknown `tunnel` key in `remote.json` silently resets *entire* config to default (remote off) |
| L2 | **LOW** | `http_server.py:294-299` | `_send_json` omits CSP header (JSON isn't rendered, but it's the one response type with no CSP) |

---

## H1 — Password brute-force lockout is defeated for a leaked-link holder  ·  **HIGH**

**Files:** `auth.py:107-118` (`check_token`), `auth.py:123-133` (`check_password`), `auth.py:173-184` (`_record_result_locked`), `http_server.py:354-356` (verify-password route), `http_server.py:305-312` (`_check_bearer`).

**Threat model (in scope, stated in the code itself):** the password is the *third factor* that must hold "so a leaked link alone still can't get in" (`auth.py:28-29`). The adversary therefore **holds a valid bearer token + secret path** (leaked pairing URL/QR) but **not** the password. The global lockout (`lockout_after_fails`, PBKDF2 200k) is the backstop that keeps even a weak 8-char password (the new `_MIN_PASSWORD_LENGTH`) safe against brute force.

**Bug:** every `POST /api/verify-password` request runs `_check_bearer()` **before** `_handle_verify_password()`:

```python
# http_server.py:354-356
if rest == "/api/verify-password":
    if self._check_bearer():            # → check_token(valid) → _record_result_locked(ok=True)
        self._handle_verify_password(body)   # → check_password(wrong) → _record_result_locked(ok=False)
```

`_record_result_locked` **resets `_fail_count` to 0 on any success** (`auth.py:176-177`). Because the attacker's token is *valid*, `check_token` succeeds and zeroes the counter on **every single request**, one call before `check_password` increments it. The counter oscillates `0 → 1 → 0 → 1 …` and never reaches the threshold. The lockout never arms.

**Repro (verified — this is the exact per-request HTTP sequence):**

```python
g = AuthGate(RemoteConfig(token="tok123",
                          password_hash=hash_password("hunter2"),
                          lockout_after_fails=5))
for i in range(50):
    assert g.check_token("tok123") is True        # _check_bearer with the leaked token
    assert g.check_password(f"wrong{i}") is False  # brute-force the password
# → g.is_locked_out() == False, g._fail_count == 1  (never locks, ran 50 guesses)
```

Result: 50 wrong-password attempts, `is_locked_out() == False` throughout. The only remaining brake is PBKDF2 server-side CPU (~tens of ms/guess) + network — no lockout, no per-IP cap (`ThreadingHTTPServer`, no rate limit). An 8-char password is brute-forceable at full request concurrency.

**Why the existing test misses it:** `tests/test_remote_auth.py:249 test_password_fails_share_the_token_lockout_counter` interleaves `check_token("wrong")` — a **failure**. The real attack interleaves `check_token(VALID)` — a **success** that resets the counter. The test proves the counter is *shared*, not that a valid-token holder is *throttled*. False confidence.

**Fix (recommended):** give the password factor its own counter/backoff independent of the token counter — e.g. `_pw_fail_count` / `_pw_locked_until` bumped only inside `check_password`, checked at the top of `check_password` and surfaced via a password-specific `is_locked_out`. A successful *token* check must not be allowed to wipe evidence of *password* brute-forcing. Add a test that interleaves a **valid** token with wrong passwords and asserts lockout arms.

---

## M1 — Markdown link renderer allows `href` attribute injection  ·  **MEDIUM**

**File:** `static/app.js:552-556` (`mdEscape`), `app.js:561-565` (`mdInline` link rule). Rendered into the DOM via `innerHTML` at `app.js:759-760` and `app.js:786`.

**Bug:** `mdEscape` escapes via `textContent → innerHTML`, which by the HTML serialization spec escapes only `& < >` (and nbsp) — **it does not escape `"`**. The link rule then interpolates the captured URL straight into a double-quoted attribute, and the URL character class `[^\s)]+` **permits `"`**:

```js
s = s.replace(/\[([^\]]+)\]\(((?:https?:\/\/|\/)[^\s)]+)\)/g, function (m, t, u) {
  return '<a href="' + u + '" ...>' + t + "</a>";   // u may contain a literal "
});
```

So Lead-relayed text like `[x](/a"onmouseover=…)` breaks out of the `href` attribute and injects arbitrary attributes onto the `<a>` element. The in-code comment "XSS-safe by construction — never an innerHTML of raw text" (`app.js:575-576`) is therefore **false for the link path** — the primary defense layer is broken.

**Reachability:** the `kind === "lead"` body is rendered with `renderMarkdown` (`app.js:760`). Lead text routinely echoes *untrusted* content (teammate messages, file contents, web-fetch/search results, git diffs, user paste), so this is a realistic relayed-injection vector, not just self-XSS.

**Current mitigation (honest):** the served CSP (`http_server.py:65-68`) is `script-src 'self'` (no `'unsafe-inline'`), which **blocks injected inline event handlers**, and `connect-src`/`img-src` fall back to `'self'`/`data:` — so injected `ping=`/`style` beacons are also blocked. On a spec-compliant browser with the CSP intact, script execution is currently prevented. The bug is a **defense-in-depth failure**: CSP is meant to be the *backup*, not the *only* layer, and a future dev trusting the "safe by construction" comment could loosen/remove CSP and expose real stored XSS. It also fails open on any client that ignores CSP or a proxy that strips the header.

**Fix (trivial):** build the anchor safely instead of string-concatenating into an attribute — either reject/strip URLs containing `"` before interpolation, or additionally escape `"` → `&quot;` in `u` (and ideally `t`), or construct the `<a>` via `createElement`/`setAttribute`/`textContent`. Then the comment's guarantee actually holds without leaning on CSP.

---

## L1 — Unknown `tunnel` key silently disables the whole remote config  ·  **LOW**

**File:** `config.py:65-72`.

`TunnelConfig(**tunnel_data)` is **not** key-filtered (unlike the top-level `known = {k … if k in cls.__dataclass_fields__}` at line 69). A `remote.json` written by a newer build with an extra `tunnel` field raises `TypeError`, caught by the outer `except` at line 71, which returns `cls()` — i.e. the **entire** config resets to default with `enabled=False`. Forward-incompatible and surprising (remote silently turns off), though it fails *safe* (off, not open). **Fix:** filter `tunnel_data` to `TunnelConfig.__dataclass_fields__` the same way the top level does.

## L2 — JSON responses carry no CSP header  ·  **LOW**

**File:** `http_server.py:294-299` (`_send_json`) vs `_serve_static` at `http_server.py:479` which does set it.

Only static files get the `Content-Security-Policy` header; API JSON responses don't. JSON isn't rendered as a document so there's no direct XSS path, but a defense-in-depth CSP costs one header line and closes the gap for any client that mis-sniffs a JSON body (belt-and-suspenders with the `Content-Type`). Optional. **Fix:** add the CSP header (and `X-Content-Type-Options: nosniff`) in `_send_json` too.

---

## Verified-clean (spot-checked, no issue)

- **Auth primitives:** `secrets.compare_digest` on secret-path and token; `hmac.compare_digest` in `verify_password`; PBKDF2-HMAC-SHA256 200k with per-hash 16-byte salt; plaintext password never persisted. Entropy: secret_path `token_urlsafe(16)`=128-bit, token `token_urlsafe(32)`=256-bit, SSE ticket/session `token_urlsafe(24)`=192-bit.
- **SSE ticket:** single-use (`consume_ticket` pops), 30 s TTL, stamped with project_ns; `/api/lead` transitively password-gated because a ticket is only mintable after `_check_password_gate` on `/api/sse-ticket`. No replay (single-use + TTL).
- **Cross-project isolation (H-A):** `SSEBroadcaster.push` filters by `project_ns`; `_on_done` stamps each event's own project; `_resolve_scoped_project` validates client project against open tabs and falls back to active (never scopes to an unopened project).
- **XSS elsewhere:** all non-link markdown leaves go through `mdEscape`/`mdInline` into *element-content* context (safe); index.html has no inline `<script>`/handlers (CSP-compliant); projects/paths/pulse rendered via `textContent`; `parseSseData` extracts only `.text`.
- **notify.py tailing:** partial-line hold-back (`tail.partial = lines.pop()`), offset backed up to a line boundary in `_tail_start_offset`, `size <= offset` guard against re-read, `_HISTORY_MAX_BYTES` cap with first-fragment drop on truncated seek. No offset/partial-parse loss found.
- **DoS surface:** `_MAX_BODY_BYTES` 64 KB, `timeout = 30` on the handler (pre-auth slowloris closed), SSE cap 6 with drop-oldest eviction + bounded per-client queue, no keep-alive (`HTTP/1.0`).
- **Idle-expire:** `touch()` only after a *successful* bearer/ticket auth (M-6) — a wrong-token request that merely knows the secret path can't keep the server alive.
- **Path traversal:** `_serve_static` resolves and checks `_STATIC_ROOT in candidate.parents`; `..`/URL-encoded traversal both rejected.

---

## Re-audit (2026-07-07) — verify fixes closed the 4 findings

**Method:** re-read each patched site + traced the fix logic; ran the remote test suite. All 5 remote test files pass (`test_remote_auth`, `test_remote_http_server`, `test_remote_scaffold`, `test_remote_api`, `test_remote_notify` → **164 passed, 0 fail**).

**Verdict: all 4 findings CLOSED. No residual gap. No new blocker.**

### H1 (password lockout defeated) — ✅ CLOSED

The password factor now has its own counter/backoff, fully decoupled from the token counter:

- `AuthGate.__init__` adds `_pw_fail_count`/`_pw_locked_until` (`auth.py:81-82`), independent of `_fail_count`/`_locked_until`.
- **Token success can no longer wipe password-brute-force evidence:** `check_token` → `_record_result_locked` (`auth.py:189-201`) touches **only** `_fail_count`/`_locked_until`; it never reads or writes `_pw_*`. `check_password` → `_record_password_result_locked` (`auth.py:203-216`) touches **only** `_pw_*`. The two record paths are disjoint.
- **`check_password` arms and honors its own lockout:** checks `time.time() < self._pw_locked_until` at the top and returns `False` before running PBKDF2 (`auth.py:141-142`); bumps `_pw_fail_count` only on `ok=False` (`auth.py:208-211`); exponential backoff `min(300, 5·2^min(overflow,6))` identical to the token path (`auth.py:213-216`). During the window even the correct password is rejected (no reset on the locked path, since it returns before `_record_*`).
- **The old repro is now throttled.** Traced the exact HTTP sequence for `POST /api/verify-password`: `_check_bearer()` → `check_token(valid)` resets `_fail_count` (irrelevant now) → `_handle_verify_password` → `check_password(wrong)` increments `_pw_fail_count`; after `lockout_after_fails` wrong guesses `_pw_locked_until` is set and subsequent guesses short-circuit.
- **New test replaces the false-confidence one.** `test_valid_token_does_not_defeat_password_lockout` (`test_remote_auth.py:262-276`) interleaves `check_token("tok123")` (**VALID** — the exact attack the old L249 test missed by using an invalid token) with wrong passwords ×5, then asserts `is_password_locked_out() is True` and that the correct password is subsequently rejected. Ran it in isolation on this branch: **passes** (would have failed on the shared-counter code).
- **Regressions intact:** token lockout still arms/clears (`TestBearerTokenAndLockout`), `test_password_and_token_lockouts_are_independent` confirms a token fail doesn't arm the password lockout and vice-versa, and the no-password-configured path still issues a session unconditionally (`_handle_verify_password` at `http_server.py:420-426`; `test_no_password_configured_is_always_ok`).

> Note: the prior-round H1 fix (global-bool → per-client `_sessions`/`X-Session`) is also present and still verified — `password_ok` gates on a per-client session, not a server flag (`auth.py:180-187`, `TestPasswordGate`). The two H1 fixes are orthogonal and both hold.

### M1 (href attribute injection) — ✅ CLOSED

- `mdInline`'s link rule now quote-escapes the captured URL before interpolation: `var safeHref = u.replace(/"/g, "&quot;")` (`app.js:567`), used in the `href="…"` (`app.js:568`).
- **Traced `[x](/a"onmouseover=…)`:** `mdEscape` runs on the whole string first (`app.js:563`), so any `<`/`>`/`&` in the URL are already entity-encoded before the link regex; the only attribute-breakout char that survived was `"`, now mapped to `&quot;`. Result renders as `<a href="/a&quot;onmouseover=…" …>` — the quote can no longer close the attribute, so no attribute injection. The double-quoted attribute makes `'` a non-issue; the link **text** `t` sits in element-content context (pre-escaped), harmless.
- `sw.js` cache bumped to `takkub-remote-shell-v11` (`sw.js:10`) so clients actually fetch the patched `app.js` instead of serving a stale shell. The "XSS-safe by construction" claim now holds for the link path without leaning on CSP (which remains as backup).

### L1 (unknown tunnel key resets whole config) — ✅ CLOSED

- `tunnel_data` is now key-filtered to `TunnelConfig.__dataclass_fields__` before `TunnelConfig(**…)` (`config.py:67-70`), mirroring the top-level `known` filter. An unknown sub-key no longer raises `TypeError` → no fall-through to the `except` that returned a default (remote-off) config.
- Tests: `test_unknown_tunnel_subkey_is_ignored_not_reset` (config keeps `enabled=True`, tunnel = default), `test_unknown_tunnel_subkey_alongside_known_ones_preserves_known` (known sub-keys survive, bogus dropped), `test_unknown_top_level_keys_are_ignored` — **3 passed**.

### L2 (JSON responses carry no CSP) — ✅ CLOSED

- `_send_json` now emits `Content-Security-Policy: _CSP_HEADER` **and** `X-Content-Type-Options: nosniff` (`http_server.py:299-302`) — matching the static-file path (`http_server.py:483`) and adding the anti-sniff header the finding asked for.

**Residual / carry-over (not in this re-audit's scope, unchanged from prior rounds):** the leaked-*token* threat is now mitigated by the third factor + password lockout; no per-IP rate limit exists (documented as a no-op behind the single tunnel edge). Nothing new introduced by these four fixes.
