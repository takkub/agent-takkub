# Cross-Platform Fix Wave — Code Review (reviewer)

**Date:** 2026-07-10
**Scope:** working-tree `git diff` of the cross-platform fix wave vs. `docs/reviews/2026-07-10-xplatform-CONSOLIDATED.md`
**Method:** read the real code + tests (no guessing), traced each fix against its finding, verified encode/normalize semantics against the on-disk proof.

**Verdict:** C1 core / H1 / L1 / L5 / L6 are correct and well-tested. **2 MEDIUM regressions introduced by the wave** need a fix loop before merge (both installed-mode / security-boundary, both invisible on a dev checkout).

---

## 🟠 Findings (regressions from this diff)

### F1 — MEDIUM (security-boundary regression): `_resume_uuid_matches_cwd` path traversal via unvalidated `session_uuid`
**File:** `src/agent_takkub/spawn_engine.py:132` · **Regression from this diff:** YES

New code:
```python
return (proj_dir / f"{session_uuid}.jsonl").is_file()
```
`session_uuid` reaches here from the remote request **unvalidated** — `remote/api.py:263-265` only does `.strip()`, and `remote/http_server.py:166` passes `payload["session_uuid"]` straight through. A crafted value like `../<other-encoded-dir>/<real-uuid>` makes the join resolve to `proj_dir/../<other-encoded-dir>/<real-uuid>.jsonl`, and `Path.is_file()` **follows `..`** → the function returns `True` for a uuid that does **not** belong to `cwd`.

**Why it's a NEW regression, not pre-existing:** the old form used `base.glob(f"*/{session_uuid}.jsonl")`. pathlib glob treats a `..` segment as a *literal child name to match* (no real dir is named `..`), so the old form was traversal-**safe**. The `Path / str` + `.is_file()` rewrite is traversal-**capable**. This directly defeats the docstring's "forgery-proof check" claim and task focus item #1 ("validate ต้องไม่ bypass/forge ได้").

**Failure scenario:** authenticated control-mode remote client POSTs `session_uuid = "../<enc>/<uuid>"` → prevalidation passes → `orch.close(lead)` runs → `spawn(--resume "../<enc>/<uuid>")`. Practical blast radius is limited (claude's own `--resume` looks the id up in *its* cwd project dir and will reject a `../`-shaped value, so the foreign session isn't actually resumed), and it's control-mode gated — but the validation boundary is bypassed and it becomes a filesystem `*.jsonl` existence oracle. Since `close()` already ran by the time `spawn()` fails, the pane can also still be torn down.

**Fix:** reject path chars before any filesystem use — e.g. at the top of `_resume_uuid_matches_cwd` (and/or in `resume_lead`):
```python
if "/" in session_uuid or "\\" in session_uuid or ".." in session_uuid:
    return False
```
or format-validate against a strict uuid charset (`^[0-9A-Za-z_-]+$`). Add a regression test that a `../`-prefixed uuid returns `False` even when the target `.jsonl` exists in another project dir.

---

### F2 — MEDIUM (installed-build functional break): plugin-install verification still reads `~/.claude` while install now writes `default_claude_config_dir()`
**File:** `src/agent_takkub/plugin_installer.py:150` (`installed_on_disk`) · **Regression from this diff:** YES (incomplete M2/M3 fix)

The wave moved the **write** path: `_claude_env()` now sets `CLAUDE_CONFIG_DIR=default_claude_config_dir()` (plugin_installer.py:100-110) and `_default_plugin_dirs` reads `default_claude_config_dir()/plugins/cache` (lead_context.py:648). But the **read/verify** path in the same module was left hardcoded:
```python
base = (home or pathlib.Path.home()) / ".claude" / "plugins" / "cache"
```
`installed_on_disk()` is called by:
- `install_plugin()` post-install success check (`plugin.key in installed_on_disk()`, line ~208) →
- `missing_plugins()` (line ~166).

**Failure scenario (installed build only, where `default_claude_config_dir()` = `DATA_HOME/claude-config` ≠ `~/.claude`):** `claude plugin install` succeeds into `DATA_HOME/claude-config/plugins`, then `installed_on_disk()` scans `~/.claude/plugins/cache`, doesn't find it → `install_plugin` returns **`False, "CLI reported success but plugin not found on disk (try restart)"`** on a genuinely successful install, and `missing_plugins()` keeps re-prompting to install forever. Invisible on a dev checkout (both dirs collapse to `~/.claude`), so `test_plugin_installer.py` (all dev-mode) won't catch it — the new tests assert the write dir but never assert `installed_on_disk` reads the same dir.

The consolidated doc listed `plugin_installer.py:125` as an M2 site; that hardcode (now line 150) is the one left unfixed. `installed_on_disk`'s own docstring even claims it "Applies the SAME condition as `lead_context._default_plugin_dirs`" — which is now false (different base dir).

**Fix:** resolve `base` via `config.default_claude_config_dir() / "plugins" / "cache"` (keep the `home` override for tests). Add a test with `DATA_HOME != REPO_ROOT` asserting install-target and `installed_on_disk` agree.

---

## 🟢 Verified PASS

- **C1 encode identity** — `encode_path_for_claude` = `_NON_ALNUM_RE.sub("-", str(Path(cwd).resolve()))` reproduces the on-disk proof exactly (`C:\Users\monch\WebstormProjects\agent-takkub` → `C--Users-monch-WebstormProjects-agent-takkub`). `session_project_dir_for_cwd` resolves via `encode_path_for_claude` (matches old `.resolve()` behaviour). Edge coverage: hyphen/underscore/dot/space all tested (`test_resume_session_picker.py::TestSurvivesLossyPathChars` across all 3 call sites); trailing-slash handled by `resolve()`; unicode is a theoretical astral-plane edge (JS UTF-16 → 2 dashes vs Python 1 dash) — not practical for real project paths, note only.
- **C1 prevalidate-before-close** — both desktop (`user_actions.py:312`) and remote (`remote/api.py:271`) check `_resume_uuid_matches_cwd` **before** `orch.close()`; `test_mismatched_uuid_rejected_before_close` asserts `close`/`spawn` never called on mismatch. Correct.
- **H1** — all four branches build env via the shared `_build_pane_env()`/`_build_lead_env()` (spawn_engine shell@1024, gemini@1091, codex@1141, claude@1361); `_apply_*` now live inside those builders (pane_env.py:164-166, 212-214); claude branch's explicit calls removed → no double-apply. `_apply_*` are idempotent env-key sets (harmless even if re-applied). End-to-end tested for codex/gemini/shell (`test_h1_nonclaude_env.py`), including that host `COLORTERM` is dropped by the allowlist and re-set to `truecolor`.
- **L1** — `_WinptyBackend.spawn` argv-as-list change is **Windows-only**; the POSIX (`ptyprocess`) backend is untouched. pywinpty quotes `argv[1:]` internally via its own `list2cmdline`, so remaining-arg quoting still happens exactly once; `argv[0]` passed verbatim avoids the shlex-requote-keeps-quotes → `which()` miss.
- **L5** — `_normalize_cwd_for_compare` applied symmetrically to **both** sides of the auto-resume compare (spawn_engine.py:1676-1677); `resolve()` + `normcase` (no-op on POSIX). The added `bool(prior_uuid_cwd)` guard is correct and necessary — `_normalize("")` resolves to the process cwd, so without the guard two empty cwds would falsely match. Store sites (orchestrator.py:2034/3382, spawn_engine 1664/1689) keep raw cwd, which is fine since normalization is at compare time. `orchestrator.py:2033` is a *store* site, not a compare site — no change needed there (consolidated doc's citation was slightly off).
- **L6** — `_absolutize` in `lead_cwd()` uses `Path.expanduser().resolve()` with `OSError` fallback to raw; `resolve(strict=False)` doesn't raise on non-existent paths; idempotent with the later `encode_path_for_claude` resolve.
- **M1 (doctor)** — `.credentials.json` filename fixed on all branches; darwin branch probes Keychain via `_read_keychain_credentials` before file/WARN. Correct three-way `darwin`/`win32`/`else` split.
- **M3 (installer exe)** — `find_claude_executable()` (raises `RuntimeError`, never returns `None`) is wrapped by both callers' `try/except Exception` → returned as `(False, str(e))`, no crash.
- **Cross-platform branches** — gemini_helper `_default_agy_paths` (win32 vs darwin, both present), codex_helper native-exe probe gated `sys.platform == "win32"` with `.cmd` fallback, pane_env TMPDIR/XDG additions — all have both-OS handling, all use `pathlib`.

---

## Notes (not blocking)
- **encode unicode edge:** `[^A-Za-z0-9]` on an astral-plane codepoint (emoji) yields 1 dash in Python but Claude's JS `/[^a-zA-Z0-9]/g` over UTF-16 yields 2. Cannot affect real project paths; documenting only.
- **F1 desktop path is safe** — desktop `uuid` comes from the trusted picker list, not attacker input; only the remote `resume_lead` entry is exposed. F1's fix belongs in the shared helper so both are covered.
