# Remote-control security audit — 2026-07-07

**Scope:** `src/agent_takkub/remote/{http_server,auth,api,notify,config,tunnel}.py` + `static/{app.js,sw.js,index.html}` + `settings_dialog.py`
**Reviewer:** reviewer (code review only — no code changed, no commit)
**Threat model:** loopback-only HTTP (`127.0.0.1`) reached from the public internet via a cloudflared tunnel; heavy untargeted bot scanning of the public hostname, plus a targeted "leaked pairing link" adversary.

## Verdict

| Severity | Count |
|---|---|
| **Critical** | **0** |
| **High** | **1** |
| Medium | 1 |
| Low | 4 |

**No Critical.** No path lets an unauthenticated scanner in, no client-driven RCE, no plaintext secret shipped, and the markdown renderer is XSS-safe by construction. The one **High** is a design weakness in the *third* auth factor (password), not a break of the first two (128-bit secret path + 256-bit bearer token), which hold. Ship is defensible, but H1 should be fixed or its guarantee re-documented before leaning on the password as "leaked-link protection."

---

## HIGH

### H1 — Password third factor is a **server-global flag**, defeated for any token-holder once the owner logs in
**File:** `auth.py:78,134-141` (`_password_verified`, `password_ok`) · `http_server.py:379-389` (`_check_password_gate`)

**What the design promises** (`settings_dialog.py:252-255`, `auth.py:25-31`): the password is never in the pairing URL/QR, so *"a leaked link (secret path + token) alone still can't get in."*

**Reality:** `password_ok()` reads a single boolean `self._password_verified` that lives on the one `AuthGate` instance shared by every handler thread for the whole server run. It flips `True` the first time **anyone** POSTs the correct password to `/api/verify-password` — and stays `True` until the server restarts or idle-expires (default `idle_expire_min=240`, i.e. up to 4 h). There is **no per-client / per-token binding**: the bearer token is the same single string for every client, so after the flag is set, every request carrying that token clears the gate.

**Repro (leaked-link adversary):**
1. Attacker obtains the pairing link (secret path + `#token=…`) — screenshot, shoulder-surf, shared QR. Password is *not* in the link (by design).
2. Attacker hits any authed route → `403 {"msg":"password_required"}`. Blocked so far. ✅
3. Owner opens the PWA and enters the password once → `_password_verified = True` **globally**.
4. Attacker replays the token → `_check_password_gate()` now passes → full read, and full **control** (`/api/lead/say`, `/api/open`) if `mode=="control"`. ❌

So the password only protects the window *before the owner's first login this session* — which in practice is almost never, since the owner logs in at the start of every session (`placeholder="required — asked again on every Enable"`, `_password_verified` starts `False` each run). The documented guarantee is false for the rest of the session.

**Caveat / honest calibration:** this requires the 256-bit token to already be leaked. Against an *untargeted* scanner (no token) the token + secret-path fully protect — this is **not** a bot-scan exposure. It is specifically a defeat of the mitigation that was added for the leaked-link case, which is why it rates High rather than Critical.

**Fix:** bind password success to the client, not the server. Simplest stateless option: on correct password, mint a per-client **session credential** (like the SSE ticket — `secrets.token_urlsafe`, server-side table, longer TTL) that the client must present on every authed request *in addition to* the bearer token; gate on that, not on a global flag. Minimum acceptable: fold a password-derived proof into each request. If neither is done, **re-document** the password as "protects only until first login of a session," because the current copy oversells it.

---

## MEDIUM

### M1 — No socket/request timeout + unbounded handler threads → pre-auth slowloris / slow-body DoS
**File:** `http_server.py:246-251` (`_RemoteHandler` — no `timeout` class attr) · `501-502` (`ThreadingMixIn`, `daemon_threads=True`, no thread cap)

`BaseHTTPRequestHandler.timeout` defaults to `None`, so `StreamRequestHandler.setup()` never calls `settimeout()` on a normal GET/POST socket (only the SSE path sets one, `http_server.py:483`). Combined with `ThreadingHTTPServer` spawning an **unbounded** thread per connection, an attacker can:

- open many connections and **trickle the request line/headers** one byte at a time — this happens in `BaseHTTPRequestHandler` *before* `do_GET/do_POST` dispatch, so **no secret path / token is needed**; or
- send `Content-Length: 64000` then drip the body — `self.rfile.read(length)` (`http_server.py:338`) blocks with no timeout.

Each held connection pins one thread indefinitely → thread/FD/memory exhaustion of the remote server. The Qt GUI stays safe (separate thread pool, as the module docstring correctly claims), and the loopback-only bind means the attacker must come through cloudflared — but the public tunnel + "lots of bot scanning" makes this reachable. Blast radius is the remote feature only.

**Repro:** `for i in $(seq 200); do (exec 3<>/dev/tcp/<host>/<port>; printf 'GET /x HTTP/1.0\r\n' >&3; sleep 600) & done` against the tunnel hostname → threads accumulate, none time out.

**Fix:** set `timeout = 30` (class attr) on `_RemoteHandler` so slow request/body reads are cut; optionally cap concurrency (bounded `ThreadPool` / connection semaphore) since this is a single-user tool that never needs more than a handful of concurrent requests + `_MAX_SSE_CLIENTS` streams.

---

## LOW

### L1 — `/api/projects` discloses each project's absolute filesystem path to the authenticated remote
**File:** `api.py:150-170` (`path: _config.lead_cwd(n)`)

`projects()` returns `{name, active, path}` for **every** imported project, where `path` is the full Lead cwd (e.g. `C:\Users\monch\WebstormProjects\…`). That leaks the OS username and directory layout to the phone — wider than `pulse`'s strict count-only data-min (`api.py:77-86`) that the design otherwise prizes ("hides workstation detail", `notify.py:90-92`). It is behind full auth and intentional (the picker renders it), so **Low**, but it is the one place the "workstation detail stays hidden" principle is relaxed. Consider showing only a basename/leaf, or gating the full path behind an explicit toggle.

### L2 — No Content-Security-Policy on the PWA shell
**File:** `static/index.html` (no CSP `<meta>`) · `http_server.py:441-462` (`_serve_static` sets no CSP header)

The markdown renderer is XSS-safe by construction — `mdEscape` routes every leaf through `textContent`→`innerHTML` first, and `mdInline`'s link regex restricts hrefs to `https?://` or `/` and can't break out of the quoted attribute (`app.js:537-556`). So there is no *known* injection. But a network-exposed page that `innerHTML`s Lead-authored text should carry a CSP as defense-in-depth. The shell has no inline `<script>` (only `<script src="app.js">`), so a strict `default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'` (allow `'unsafe-inline'` for styles only if needed) would cost nothing and neutralize any future renderer regression.

### L3 — Global lockout is triggerable by a secret-path-only holder → owner lockout DoS
**File:** `auth.py:97-155` (`_record_result_locked`, global counter — comment at `:94-96` acknowledges non-per-IP)

Fail counting is global (correct rationale: all requests share one tunnel-edge IP). But `check_token` runs only after `_match_secret_path` succeeds, so anyone who knows the **secret path** (128-bit) but not the token can send bad tokens to drive `_fail_count` past the threshold and lock out the *legitimate* user (`_locked_until`, backoff up to 300 s). Requires guessing/leaking the 128-bit secret path first, so likelihood is low. Acceptable as-is; note it because the lockout is a shared resource an attacker who has *only* the secret can weaponize against the owner.

### L4 (Info) — No password strength enforcement + throttled brute-force window against `/api/verify-password`
**File:** `settings_dialog.py:241-244` (no min-length/complexity) · `http_server.py:339-341` + `auth.py:117-125`

`/api/verify-password` is reachable with just secret-path + token (it *is* the password gate, so it can't sit behind the gate). A leaked-link adversary can therefore brute-force the password. It is well-throttled — PBKDF2-HMAC-SHA256 × 200 000 per attempt (`auth.py:30,56`) + shared lockout (steady state ≈ 1 try / 300 s after overflow) — so a strong password is safe for years. But the dialog enforces **no minimum length**, so a 4-digit PIN falls in ~35 days of sustained attack (and instantly if the owner reuses a common one). Add a client-side min-length/complexity check (and ideally a hint) at set time. Tied to H1's precondition (leaked link); harmless without it.

---

## What was checked and is solid (no finding)

- **Entropy:** `secret_path`=`token_urlsafe(16)` (128-bit), `token`=`token_urlsafe(32)` (256-bit), SSE ticket=`token_urlsafe(24)` (192-bit). All ample. (`__init__.py:73-76`, `auth.py:166`)
- **Constant-time compares** everywhere it matters: `secrets.compare_digest` for secret-path/token, `hmac.compare_digest` for the PBKDF2 digest. No `==` on secrets. (`auth.py:57,85,109`)
- **Password at rest:** PBKDF2 salt+digest only; plaintext lives solely in the dialog's `QLineEdit`, cleared on disable, never persisted/logged/returned. (`settings_dialog.py:26-27,238-243,316-318`)
- **404-for-everything** pre-auth (wrong secret path *or* wrong token) — no 401, no route existence oracle. `log_message` suppressed so `?ticket=` never hits stderr. (`http_server.py:253-256,272-277`)
- **`touch()` only after successful auth** (M-6) — a secret-path-only prober can't keep the idle-expire clock alive. (`http_server.py:296,470`, `auth.py:87-92`)
- **Control-mode gate present on every write route** (`/api/lead/say`, `/api/open`) *before* the handler runs; `pulse`/`projects`/`lead/history` are read-only and view-safe. `open_project` validates against the `projects.json` allowlist and only re-opens an already-imported tab — no arbitrary spawn. (`http_server.py:350-375`, `api.py:106-130`)
- **Client can't choose the cli_server `cmd`** — only `list` (pulse) and `send` (lead_say) are hardcoded in `api.py`; no passthrough. `lead_say` text is run through `_sanitize_pane_text` (strips ESC/CR/bracketed-paste) before paste, so no terminal-escape injection — the text reaches Lead as an AI prompt, not a shell command. (`api.py:79,96-98`, `orchestrator.py:1088`)
- **SSE isolation:** ticket carries its `project_ns`, single-use `pop` (no replay), 30 s TTL; `push` drops events whose `project_ns` ≠ the client's ticket namespace → no cross-project leak. Requested project validated against open tabs, else falls back to active (never arbitrary). (`auth.py:163-181`, `http_server.py:97-107,213-234`)
- **SSE framing/injection closed** (H-C): payload JSON-encoded before the wire, `event` allow-listed → raw `\n`/`event:` can't break framing. Client uses `addEventListener("lead"|"done"|"working")` matching the server's named events. (`http_server.py:157,213-220`, `app.js:814-822`)
- **Data-min holds on the live surface:** `working` events send only a fixed coarse category (`_TOOL_ACTIVITY`), never tool args/paths/commands; `lead`/history extract only `type=="text"` assistant blocks, never tool_use/thinking/user text; `pulse` is `{working,total}` count only. (`notify.py:91-125,128-150`, `api.py:77-86`)
- **Static path traversal closed:** `(_STATIC_ROOT/rel).resolve()` re-checked against `_STATIC_ROOT`/its parents; symlinks resolved out. No secrets in the served shell (token comes from the URL fragment, client-side). (`http_server.py:441-462`)
- **`lead_history` / jsonl glob:** `project_ns` is always an open-tab/active name (never raw client input), `session_uuid` comes from pane state, not the client → no traversal via the glob. (`api.py:133-147`, `notify.py:153-181`)
- **CSRF N/A:** auth is a custom `Authorization` header + localStorage token, no cookies/ambient credentials, no CORS headers → a cross-site page can neither read responses nor forge authed writes.
- **Tunnel hardening:** `public_url` control-char + strict-hostname validated and fed to `yaml.safe_dump` (no hand-templated YAML); `credentials_json` must be an absolute path with a UUID `TunnelID`; teardown via `_tree_kill` + Windows kill-on-close Job Object. Tunnel inputs are local config, not client-reachable — no remote command injection. (`tunnel.py:87-149,208-257,365-382`)
- **Secrets not in repo/npm:** `remote.json` lives at `~/.takkub/remote.json` (outside the repo tree, `.takkub/` also gitignored); `git ls-files` shows none tracked. All test fixtures are obvious dummies (`sek`/`tok`/`s3cr3t`). (`config.py:19-21`)
- **Off by default:** `enabled=False`, `bind_port` loopback-only, `password_hash=""` (third factor opt-in). Nothing binds a socket until `enabled=true`. (`config.py:37-53`, `__init__.py:51-66`)

---

## Recommended priority
1. **H1** — bind password verification to the client (per-client session credential), or re-document the guarantee.
2. **M1** — set `_RemoteHandler.timeout = 30` (+ optional concurrency cap).
3. **L2** — add a strict CSP header/meta (cheap defense-in-depth).
4. **L1 / L4 / L3** — trim the disclosed path, enforce a password min-length, note the secret-path lockout DoS.

---

## Remediation status — 2026-07-07 (backend)

- **H1 — FIXED.** `AuthGate` no longer holds a global `_password_verified` flag.
  `check_password()` now only verifies + records the fail counter; a
  successful `/api/verify-password` mints a per-client session credential via
  `issue_password_session()` (`secrets.token_urlsafe(24)`, TTL = `idle_expire_min`
  minutes, or 4h if idle-expire is disabled). Every authenticated route gates
  on `AuthGate.password_ok(session_token)`, where `session_token` comes from
  the new `X-Session` request header — a bearer token alone (leaked link) no
  longer unlocks the gate once some other client has logged in. See
  `auth.py` (`issue_password_session`/`check_password_session`) and
  `http_server.py` (`_check_password_gate`). Tests:
  `tests/test_remote_auth.py::TestPasswordGate`,
  `tests/test_remote_http_server.py::TestPasswordGate`.
- **M1 — FIXED.** `_RemoteHandler.timeout = 30` (class attribute) bounds every
  socket read (setup/request-line/headers/body) — a trickled pre-auth
  connection can no longer pin a handler thread forever. Concurrency cap left
  as-is (optional per the recommendation; `_MAX_SSE_CLIENTS` already bounds
  the one intentionally long-lived connection type).
- **L2 — FIXED.** `_serve_static` now sends
  `Content-Security-Policy: default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'`
  on every static response (`'unsafe-inline'` on `style-src` only, for the
  shell's one inline `<style>` block — no inline `<script>` exists).
- **L4 — FIXED.** `settings_dialog.py` now rejects a password under 8
  characters client-side at Enable time (`_MIN_PASSWORD_LENGTH`), with an
  updated placeholder hint. Server-side hashing/storage unchanged.
- **L1 — accepted, not fixed.** User confirmed the full project path in
  `/api/projects` is wanted as-is (project-picker UX); leaving as documented
  in the original finding.
- **L3 — accepted, note only.** Global lockout remains intentionally
  non-per-IP (all requests share one tunnel-edge IP per the original
  rationale); the secret-path-only lockout-DoS risk is acknowledged and left
  unmitigated, consistent with the audit's own "acceptable as-is" call.
