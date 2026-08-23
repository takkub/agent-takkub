# Review: #367 Remote Reports — `wt/backend-1787488280`

**Verdict: MERGE-WITH-FIX** (should-fix items below; nothing found blocks merge)

Diff reviewed: `git diff c1bd25d..wt/backend-1787488280` (10 files, +1271/-1). Read-only review — no code changed. Verified by running `PYTHONPATH=<worktree>/src takkub qa-gate --targeted tests/test_remote_reports.py tests/test_remote_http_reports_route.py tests/test_cli_report.py` in the actual `worktrees/agent-takkub/backend-1787488280` checkout → **71 passed, 1 skipped** (symlink test skips when the environment can't create symlinks). Also ran `lint-imports` on that checkout → **29 kept, 0 broken**, confirming `remote-bolt-on-isolation` holds (`cli.py` never statically imports `agent_takkub.remote`).

Scope: `src/agent_takkub/remote/reports.py` (new), `remote/http_server.py`'s `/r/` route, `remote/auth.py`'s report lockout counter, `remote/tunnel.py::is_tunnel_alive`, `cli.py`'s `takkub report` subcommand, 3 new test files, 2 doc files.

## What's solid

- **Containment** (task item 2): `_validate_report_name`'s regex (`^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$`, plus explicit `".."`/`"/"`/`"\\"` checks) is a strict *allowlist* — `%` isn't in the allowed charset, so URL-encoded traversal (`%2e%2e`, `%2f`) is rejected structurally, not by a decode-then-blocklist check that could be bypassed. `_contained_path`'s `.resolve()` + `is_relative_to()` is redundant defense-in-depth on top of that (also catches symlink escape, since `.resolve()` dereferences). Tests cover traversal, absolute path, Windows drive-relative (`C:evil.html`), UNC-shaped, and symlink-escape — all pass, including against a real filesystem symlink where the environment allows creating one.
- **404 uniformity** (task item 1): wrong token / expired / revoked / unknown name / unknown project / disallowed extension all fall through `resolve()` → `None` → the same bare 404 from `_reject()`. Verified by the route tests (`test_wrong_token_is_404`, `test_expired_report_is_404`, `test_revoked_report_is_404`, `test_unknown_name_is_404`, `test_unknown_project_is_404`, `test_non_whitelisted_extension_is_404_even_with_matching_record`).
- **Token comparison**: `hmac.compare_digest` in `resolve()`, `secrets.token_urlsafe(32)` (256-bit) tokens. Good.
- **IP-based lockout**: correctly *not* attempted — no `X-Forwarded-For`/`client_address` reads anywhere in `remote/`, consistent with `check_token`'s existing "every request arrives from the same tunnel edge, per-IP counting is a no-op" rationale (§7.2). No new spoofable-header trust introduced.
- **`project_ns` derivation**: `reports.py::_project_ns` calls the exact same `_config.validate_name(project or "default", "project")` that `api.py::lead_upload_image` uses (confirmed both call sites literally) — no reimplementation/drift.
- **Atomic writes + secrets-derived tokens**: `_save_shares` is tmp+`os.open(0o600)`+`replace()`, matching `RemoteConfig.save()`'s existing pattern.
- **`cli.py` isolation contract**: dynamic `importlib.import_module` per the `remote-bolt-on-isolation` import-linter contract — verified `lint-imports` still reports it `KEPT` on this branch.
- **Lead-only gate**: enforced via `LEAD_ONLY_COMMANDS` in `main()`, tested directly (`TestRoleGate` — teammate rejected, lead/unset-role allowed).
- **Never-raises contract in `resolve()`**: every failure path (bad project, bad name, missing/expired token, missing file, containment failure) is caught and returns `None`; nothing propagates to a 500/traceback. `_serve_report` itself only has two narrow `try/except OSError` (file read, socket write) — consistent with the rest of the file.

## Should-fix

1. **Report lockout counter is global across *all* reports, not scoped per report/token** (`auth.py` `_report_fail_count`/`_report_locked_until` are single instance-wide fields). A successful request against report B resets the fail-count accumulated by an active brute-force attempt against report A — this is the same class of bug the H1 audit fixed for the token/password split (a legitimate success on one surface silently erasing brute-force evidence on an unrelated surface), just recreated one level down. `test_successful_request_resets_report_fail_count` in `test_remote_http_reports_route.py` actually documents this behavior as intentional. **Practical severity is low**: report tokens are 256-bit (`secrets.token_urlsafe(32)`), so brute-forcing is already computationally infeasible regardless of lockout state — the lockout is defense-in-depth on an already-uncrackable secret, not the primary control. Worth scoping the counter per `(project_ns, name)` for consistency with the module's own stated rationale, but not a merge blocker.
   - `src/agent_takkub/remote/auth.py:201-215`

2. **`record_report_token_result` counts requests with *no* `k` param at all as a failure**, unlike `check_token` which explicitly excludes empty/missing tokens from its counter ("Missing/malformed Authorization headers are common background probes... Counting them lets anyone who learns only the secret path globally lock out the owner" — `auth.py:141-147`). `_serve_report` calls `record_report_token_result(path is not None)` unconditionally, including when `query.get("k")` is `None`. Since reaching `/r/` already requires the correct secret path, impact is bounded to someone who already has that — but it's an inconsistency with the established pattern in the same file, and means a user pasting a report URL with the `?k=` part accidentally dropped (or a link-unfurl bot that truncates query strings) contributes to locking out the *actual* recipient. Recommend mirroring `check_token`'s `if token:` guard.
   - `src/agent_takkub/remote/http_server.py:296-303`

3. **Missing `X-Content-Type-Options: nosniff` on `/r/` responses.** `_send_json` in the same file sets this header on every response; `_serve_report` doesn't. Content-Type values served are already precise per the extension whitelist so practical browser-sniffing risk is low, but it's a one-line, zero-cost fix and keeps the file internally consistent.
   - `src/agent_takkub/remote/http_server.py:309-318`

4. **`validate_standalone_html`'s external-reference check has real bypasses, and the module docstring overclaims coverage** (task item 9). `_EXTERNAL_ASSET_RE` only fires on `.html` files, only inspects `<script|link|img>` tags, and only matches literal `https?://` — it misses:
   - protocol-relative URLs (`<script src="//evil.example/x.js">`)
   - `<iframe>`/`<object>`/`<embed>` external `src` (only `object-src 'none'` in the CSP catches object/embed; iframe isn't covered by the regex at all)
   - `@import url(...)`/`url(http://...)` inside an inline `<style>` block
   - **`.svg` files are never checked at all** — SVG is on the extension whitelist and can carry its own inline `<script>` or external references, but `validate_standalone_html` only runs its check when `path.suffix.lower() == ".html"`.

   `http_server.py`'s comment above `_REPORT_CSP_HEADER` says publish()/validate_standalone_html "already reject any external ... reference before a file is ever stored" — this isn't accurate for the cases above. **That said, this is not exploitable in practice**: `_REPORT_CSP_HEADER`'s `default-src 'self'` (no explicit `frame-src`/`connect-src`) means CSP's directive-fallback rules block all of the above at render/fetch time regardless of what the store-time regex missed — external `<script src>`, `<iframe src>`, `fetch()`/XHR exfiltration, and CSS `@import` all fall back to `default-src 'self'` and get blocked by the browser. So the CSP is doing the actual enforcement; the regex is a (currently leaky) secondary gate. Recommend either tightening the regex (add protocol-relative + iframe/object/embed + extend to `.svg`) or — cheaper — softening the docstring's claim so a future reader doesn't rely on the regex alone.
   - `src/agent_takkub/remote/reports.py:60-67`, `192-202`
   - `src/agent_takkub/remote/http_server.py:237-246` (comment)

## Minor / nice-to-have (not blocking, not must-fix)

- **Timing side-channel in `resolve()`**: "no such report" returns fast (single dict miss); "report exists, wrong token" does a JSON load + `hmac.compare_digest`. Both produce the same 404, but response *latency* differs slightly, which is technically a report-existence oracle. In practice this is drowned out by tunnel network jitter (cloudflared adds tens of ms; the compute delta here is microseconds), so not worth engineering around, but noting since the docstring explicitly claims "never let a guess distinguish exists from doesn't."
- **`_serve_report` buffers the whole file into memory on every request** (`path.read_bytes()`, no streaming, no size cap beyond `publish()`'s non-blocking 5MB *warning*). A large published file served to multiple concurrent requesters could add up in RSS. Self-inflicted only (Lead controls what gets published), low priority.
- **`tunnel.is_tunnel_alive()`** trusts a PID-exists check without verifying the PID still belongs to the tunnel process (PID reuse could produce a stale "alive" reading). Purely cosmetic — it only feeds the printed status line, not the actual `/r/` auth path — not a security issue.

## Confirmed by testing, not just reading

- LEAD_ONLY_COMMANDS gate (`TestRoleGate` in `test_cli_report.py`)
- Containment/traversal/symlink-escape (`TestNameValidation`, `TestResolve` in `test_remote_reports.py`; `test_traversal_in_path_is_404` in the route tests, raw-socket test with literal `../../` in the request line)
- 404 uniformity across every failure mode (route tests)
- Report lockout independence from bearer lockout (`test_lockout_is_independent_of_bearer_lockout`)
- `remote-bolt-on-isolation` contract intact (`lint-imports`, 29/29 kept)
- Full targeted suite green: 71 passed / 1 skipped
