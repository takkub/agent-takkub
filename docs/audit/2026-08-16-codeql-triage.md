# 2026-08-16 — CodeQL code-scanning triage (15 alerts on `main`)

Full triage of every open CodeQL alert on `main` as of 2026-08-16. No alert
was dismissed without a proof; no fix weakens an existing check. Branch:
`wt/backend-1786859725`.

## Summary table

| # | Rule | Location | Sev | Outcome |
|---|------|----------|-----|---------|
| 18 | `py/weak-sensitive-data-hashing` | `remote/session_store.py:44` | High | **Dismissed — false positive** |
| 8/9/10 | `py/path-injection` | `remote/http_server.py:636/640/644` | High | **Dismissed — false positive** (guard hardened) |
| 13 | `js/user-controlled-bypass` | `remote/static/app.js:2539` | High | **Dismissed — false positive** |
| 11 | `actions/missing-workflow-permissions` | `.github/workflows/ci.yml:16` | Medium | **Fixed** |
| 12 | `actions/missing-workflow-permissions` | `.github/workflows/ci.yml:88` | Medium | **Fixed** |
| 5 | `js/insecure-temporary-file` | `npm/scripts/shortcut.js:48` | High | **Fixed** |
| 4 | `js/file-system-race` | `npm/scripts/pathfix.js:97` | High | **Fixed** |
| 14/15 | `py/incomplete-url-substring-sanitization` | `tests/test_remote_diagnostics.py:174/175` | High | **Dismissed — false positive** |
| 16/17 | `py/incomplete-url-substring-sanitization` | `tests/test_remote_scaffold.py:188/189` | High | **Dismissed — false positive** |
| 6 | `py/incomplete-url-substring-sanitization` | `tests/test_doctor.py:434` | High | **Dismissed — false positive** |
| 3 | `js/xss-through-dom` | `Dev Team Office.html:145` | High | **Fixed — dead file removed** |

(Alerts #1/#2/#7 were already `fixed` on `main` before this pass — not part
of the 15 open ones handled here.)

`#11/#12/#4/#5/#3` are left in GitHub's `open` state deliberately — they're
genuine code fixes, not false positives, so they close themselves as `fixed`
the next time CodeQL scans this branch/its merge into `main`. Dismissing a
real fix via the API with reason `false positive` would be inaccurate.

---

## Group 1 — network-exposed remote-control surface

### #18 — `session_store.py:44` — weak hash on "sensitive data (password)"

**Verdict: false positive**, annotated + dismissed.

CodeQL's source heuristic matches on the *word* "password" and traces it
into `hashlib.sha256(...)` — but neither call site actually hashes a
password:

- `hash_token()` (the flagged line) only ever receives `token =
  secrets.token_urlsafe(24)` — 192 bits of server-generated randomness (see
  `AuthGate.issue_password_session()` in `auth.py:185`). A slow KDF
  (PBKDF2/bcrypt/Argon2) exists to defend against brute-forcing a
  *low*-entropy secret (a human-chosen password); it adds nothing against a
  192-bit random value — exhausting that space is infeasible at any hash
  speed — while adding real CPU cost to every session check on every
  polling phone.
- `fingerprint()` hashes `config.password_hash` — which is **already** the
  PBKDF2-HMAC-SHA256 digest from `auth.hash_password()` (confirmed by
  reading `auth.py:48-53`), never the raw password — together with
  `secret_path`/`token`. It's a local on-disk change-detector (`load()`
  discards the session file on any fingerprint mismatch); it's never sent
  over the network or accepted as a credential, so weak-hash concerns don't
  apply to it either.

Verified the real password path is correct: `hash_password()`/
`verify_password()` in `auth.py` already use `hashlib.pbkdf2_hmac("sha256",
...)` with a random 16-byte salt — that's the actual password-hashing code,
untouched by this alert.

**Action:** added explanatory comments + `# codeql[py/weak-sensitive-data-hashing]`
on both `hashlib.sha256(...)` call sites in `session_store.py`, dismissed
alert 18 via API (`false positive`).
Tests: `tests/test_remote_session_store.py`, `tests/test_remote_auth.py` — pass.

### #8/#9/#10 — `http_server.py:636/640/644` — path injection in `_serve_static`

**Verdict: false positive** (guard was already correct; hardened + annotated).

`_serve_static(rest)` serves the PWA shell from disk using an
attacker-controlled URL path segment. The existing guard was
canonicalize-then-check-containment — the textbook-correct pattern (per
OWASP path-traversal guidance): resolve the candidate to an absolute path
first (collapsing `..` *and* following symlinks), then verify that absolute
path is the static root itself or strictly inside it, before any read.

Verified this actually holds across every escape vector, not just assumed:

- **`..` traversal** (`/secret/../../etc/passwd`): `urlsplit(self.path).path`
  does **not** normalize dot-segments, so `..` reaches `_serve_static`
  intact; `.resolve()` collapses it against the joined absolute path,
  producing a path outside `_STATIC_ROOT` → rejected.
- **Symlinks**: `.resolve()` follows them by default, so a symlink inside
  the static dir pointing outside still resolves to its real target before
  the containment check runs.
- **Windows drive-letter/UNC override**: `PurePath.__truediv__` resets to
  an absolute/drive-anchored right-hand operand (e.g. `_STATIC_ROOT /
  "C:/secret"` discards `_STATIC_ROOT` entirely and becomes just
  `C:/secret`). This doesn't bypass the guard — it just changes what
  `candidate` resolves to, and the containment check runs on the *final*
  resolved path regardless of how it got there, so a drive override is
  caught exactly like any other escape.
- **Percent-encoding**: `urlsplit().path` is never percent-decoded, so
  `%2e%2e%2f` is treated as a literal filename component (not `../`) —
  traversal-inert by construction (fails closed as 404, not a bypass).

The three flagged lines are `resolve()` (line 636 — the sink CodeQL always
flags here since computing the canonical path *is* the first, unavoidable
step of the containment check itself — there's no way to check containment
without first resolving) and the two filesystem touches (`is_file()`,
`read_bytes()`) that run **after** the guard. These two are provably safe:
they only execute once `candidate.is_relative_to(_STATIC_ROOT)` is true.

**Action:** simplified the guard from a manual `!=`/`not in .parents` pair
to the equivalent, clearer `Path.is_relative_to()` (Python ≥3.9, this repo
requires ≥3.11) — same semantics, easier for a reader (and a future
scanner) to verify. Added a doc-comment explaining the threat model above
each risk class, and `# codeql[py/path-injection]` at each of the three
sink lines. Dismissed alerts 8/9/10 via API (`false positive`).
Tests: `tests/test_remote_http_server.py::TestStaticFileTraversal` (existing
traversal-rejection coverage) + full file — pass (73/73).

### #13 — `app.js:2539` — user-controlled bypass of a "sensitive action"

**Verdict: false positive**, annotated + dismissed.

Flagged line: `if (state.token) enterAuthenticatedApp();` inside `init()`'s
network-failure `.catch()`. CodeQL's model: a client-controlled value
(`state.token`, from `localStorage`) gates a "sensitive action"
(`enterAuthenticatedApp`).

Traced what `enterAuthenticatedApp()` actually does: `showApp()` (pure
CSS/DOM view toggle, no data) + `fetchProjectsAndMode()` +
`startUsagePolling()` — both go through `apiFetch()`, which independently
re-authenticates **every single request** server-side (`Authorization:
Bearer` checked by `_check_bearer` → `AuthGate.check_token`, optional
password session checked by `_check_password_gate`, see
`http_server.py:365-372` and the `do_GET` routing table). No data and no
privileged action is reachable purely from `state.token` being truthy — a
forged/stale value here only means the later `apiFetch()` calls 404/403 and
`forgetToken()` bounces the UI back to pairing, exactly like today.

Also confirmed the intended failure mode is already handled correctly:
`apiFetch()` clears `state.token` (`forgetToken()`) on any real auth
failure (server 404 on a known-bad token, `http_server.py`'s zero-surface
design), so by the time this `.catch()` runs, a genuine rejection has
already zeroed `state.token` — what's left distinguishing "network blip,
stay optimistic" from "actually logged out" is exactly what the code
checks. This is UI-routing resilience, not an auth boundary — the browser
is not a trust boundary in this architecture; the server is, and it's
independently enforced.

**Action:** added an explanatory comment + `// codeql[js/user-controlled-bypass]`
at the flagged line. Dismissed alert 13 via API (`false positive`).

---

## Group 2 — easy, correct fixes

### #11/#12 — `ci.yml:16`/`:88` — workflow doesn't set `permissions`

**Verdict: real gap, fixed.**

Neither `lint-and-test` nor `installed-gate` pushes commits, opens PRs, or
touches packages/releases — both only read the repo to lint/build/test.
Added a single least-privilege top-level block covering the whole workflow
(applies to both jobs, since neither needs anything beyond `contents:
read`):

```yaml
permissions:
  contents: read
```

Verified `.github/workflows/ci.yml` still parses and both jobs remain
intact (`python -c "import yaml; ..."`) — no functional change otherwise.

### #5 — `shortcut.js:48` — insecure temp file

**Verdict: real gap, fixed.**

`path.join(os.tmpdir(), \`att-shortcut-${process.pid}.ps1\`)` is a
predictable path in a shared temp directory, written via plain
`writeFileSync` (default `'w'` flag = `O_CREAT|O_TRUNC`, **not**
`O_EXCL`) — so a pre-planted symlink at that exact path would be silently
followed and overwritten. Fixed by switching to `fs.mkdtempSync()` (a
freshly-created, uniquely-named, current-process-owned directory — the
same primitive Node's own docs recommend for this) and writing inside it
with the `wx` flag (`O_CREAT|O_EXCL`, refuses to follow/clobber anything
pre-existing at that path), cleaning up the whole temp dir afterward
instead of a single file.

Verified with a standalone smoke test
(`mkdtempSync` + `wx` write + re-`wx` write correctly throws `EEXIST`) —
passed. `createWindows()` behavior (shortcut creation, cleanup-on-finally)
is otherwise unchanged.

### #4 — `pathfix.js:97` — file-system race (TOCTOU)

**Verdict: real gap, fixed.**

`posixEnsure()` used three separate path-based calls — `existsSync`,
`readFileSync`, `writeFileSync` — each independently resolving `rc`
(`~/.zshrc`/`~/.bashrc`). Between any two of them, the path could start
pointing somewhere else (symlink swap) or its content could change
underneath the check. Rewrote to open the file **once** and reuse that
single fd for the whole read-then-append cycle: `r+` if it exists, `wx+`
(atomic create, still readable) if it doesn't — so what gets read and what
gets written to are provably the same open file description end to end,
closing the gap CodeQL flags. `fs.writeSync(fd, block)` appends correctly
because the preceding `fs.readFileSync(fd, 'utf8')` already advanced the
fd's position to EOF.

Caught a real bug while validating this fix: the first draft used `wx`
(write-only) for the create branch, which threw `EBADF` on the subsequent
read — fixed to `wx+`. Verified with a standalone smoke test exercising
all three paths (create-new, idempotent second run doesn't duplicate the
marker, existing content preserved) — all pass.

---

## Group 3 — triage (test-file false positives + one dead-code removal)

### #14/#15/#16/#17/#6 — `py/incomplete-url-substring-sanitization` (5×, all in tests)

**Verdict: false positive** (all 5, same shape), annotated + dismissed.

This CodeQL rule exists to catch code that tries to *validate/sanitize* an
untrusted URL by checking whether a trusted substring appears in it before
making a security decision (e.g. `if "trusted.com" in url: allow_redirect()`
— bypassable by `evil-trusted.com.attacker.com`). None of the 5 flagged
lines do that:

- `test_remote_scaffold.py:188-189` / `test_remote_diagnostics.py:174-175`:
  `assert "old.example.com" in note` / `assert "new.example.com" in msg` —
  asserting a **diagnostic string** (`hostname_mismatch_note`,
  `check_ingress_mismatch()`'s return value) mentions a hostname as free
  text. No fetch/redirect/allowlist is gated on it.
- `test_doctor.py:434`: `assert "nodejs.org" in node.fix_hint` — asserting
  a **install-hint string** mentions a domain. Same shape.

All 5 are test assertions on human-readable message content, not URL trust
decisions — the production code under test (`diagnostics.py`,
`doctor.py`) never uses substring containment to validate a URL anywhere.

**Action:** added a short comment + `# codeql[py/incomplete-url-substring-sanitization]`
at each of the 5 lines. Dismissed all 5 via API (`false positive`).
Tests: `test_remote_scaffold.py`, `test_remote_diagnostics.py`,
`test_doctor.py` — pass (73/73).

### #3 — `Dev Team Office.html:145` — DOM text reinterpreted as HTML

**Verdict: real finding, but in dead code — removed the file.**

Line 145 (`new DOMParser().parseFromString(template, 'text/html')`) feeds a
JSON-decoded template string into an HTML parser — a genuinely XSS-shaped
pattern *if* `template` could ever be attacker-influenced and this page
were actually served/opened by the app.

It isn't. Checked before touching anything:

- `grep -r "Dev Team Office"` across the whole repo → **zero** other
  references (nothing builds it, serves it, imports it, tests it, or links
  to it from docs).
- `grep -ri "bundler"` (its own internal terminology — `__bundler_loading`,
  `__bundler_thumbnail`, "Bundled Page" title) across the whole repo → the
  **only** match is inside this file itself. There is no "bundler" feature
  anywhere in the shipped product (Python source, npm scripts, tests, or
  docs) that this file is a fixture/output for.
- `git log` on the file: a single commit (`3634b9d`, "chore: scrollback
  500, jt-inbound-checker guide, office page") batching three unrelated
  changes — consistent with an accidental/stray commit of a leftover local
  artifact, not an intentional product asset.

Since it's a fully orphaned, unreferenced 518 KB artifact with a real
XSS-shaped sink and no product tie-in, the correct and most complete fix is
to delete the vulnerable code entirely rather than patch a file nothing
uses. `git rm "Dev Team Office.html"` — staged, not committed (Lead
reviews before merge, per this branch's isolation).

---

## Verification run (this pass, all targeted — no full suite)

```
tests/test_remote_session_store.py, tests/test_remote_auth.py           — pass
tests/test_remote_http_server.py (full file, incl. TestStaticFileTraversal) — 73/73 pass
tests/test_remote_scaffold.py, tests/test_remote_diagnostics.py,
tests/test_doctor.py                                                    — pass
ruff check / ruff format --check (all edited .py files)                 — clean
lint-imports (25 architecture contracts)                                — 25 kept, 0 broken
python -c "import yaml; ..." on ci.yml                                  — parses, both jobs intact
node smoke tests (pathfix.js fd-reuse cycle, shortcut.js mkdtemp+wx)     — pass
```

No full pytest suite run per repo convention (targeted-tests-mid-flight;
full suite is the qa batch gate before merge).
