# #205 — subprocess text-mode decode crash on non-UTF-8 locale

## Symptom (as auto-captured on the user's machine)

```
UnicodeDecodeError: charmap codec cant decode byte 0x9a in position 62
  subprocess.py:1597 in _readerthread -> buffer.append(fh.read())
  encodings/cp874.py:23 in decode
pid 19952 (dev cockpit), Windows-10-10.0.26200, v1.0.60
```

## Root cause

`subprocess.run(..., text=True)` / `Popen(..., text=True)` without an
explicit `encoding=` decodes the child's stdout/stderr with
`locale.getpreferredencoding(False)` — the OS codepage, not UTF-8. This
user's machine is Thai-locale Windows, so that codepage is **cp874**.
Any external CLI (git/npm/claude/codex/gemini/cloudflared/ngrok/security/
softwareupdate/pip/...) that ever writes a byte cp874 can't map
(`0x9a` here) kills the `_readerthread` background thread with
`UnicodeDecodeError`.

That thread crash doesn't propagate synchronously to the caller —
`threading.excepthook` in `app.py` / `auto_issue_capture.py` is what
turns it into the auto-captured cockpit issue. Confirmed by reproducing
both shapes back-to-back on this machine (`locale.getpreferredencoding()
== 'cp874'`):

- **Unpatched** (`text=True` only): reader thread raises
  `UnicodeDecodeError: 'charmap' codec can't decode byte 0x9a in position
  0` — identical file/line/exception shape to the reported crash.
- **Patched** (`text=True, encoding="utf-8", errors="replace"`): no
  exception, `stdout == '\ufffd AB'` (the undecodable byte replaced,
  not fatal).

`encoding=` alone is not sufficient: with the default `errors="strict"`,
a byte the *declared* encoding can't map still raises the same way.
Output from an external CLI is not something this codebase controls, so
every text-mode call needs `errors="replace"` too — never `"strict"`.

## Scope of the sweep

Grepped `text=True|universal_newlines=True` across all of
`src/agent_takkub/**/*.py`: 34 matches across 21 files. Of those, **23
call sites across 12 files** were missing `encoding=` and/or `errors=`
and got fixed:

- `doctor.py` — 8 sites (including two new checks: mini-browser npm
  install, PyQt6 reinstall, `--reinstall` pip repair, `_hook` self-probe)
- `mcp_bridge.py` — 2 (codex MCP list, codex `--version`)
- `limit_status.py` — 2 (`claude --version` probe, macOS Keychain read)
- `provider_usage.py` — 2 (codex `app-server` Popen stream, opencode
  `db` query)
- `worktree_manager.py` — 2, including
  `repair_editable_pth_if_stale()` (merged the night before this fix —
  same bug, brand new code)
- `release.py` — 2 (`gh release create`, internal `_git()` helper) —
  these two had **no `encoding=` at all**, not just a missing `errors=`
- `issues.py` — 1 (had `encoding="utf-8"` but no `errors=`, so it was
  still exploitable under `errors="strict"`)
- `graft_autobuild.py`, `provider_install.py`, `update_panel.py` (×2),
  `remote/settings_dialog.py` — 1 each

Files that already had the correct `encoding="utf-8", errors="replace"`
pair (used as the reference pattern for every fix above):
`git_status.py`, `project_rules.py`, `autoskills_installer.py`,
`codex_helper.py`, `claude_update.py`, `gemini_helper.py`,
`plugin_installer.py`, `services.py`.

**Not touched, and correctly so:**
- `shared_dev_tools.py`'s `tempfile.mkstemp(..., text=True)` — this is
  `tempfile`, not `subprocess`; the fd it returns is immediately
  re-wrapped with `os.fdopen(fd, "w", encoding="utf-8")`, so there's no
  locale-dependent decode in that path at all.
- Every `.decode(...)` call already found on a subprocess/PTY output
  path (`graft_autobuild.py`, `claude_update.py`, `skill_scan.py`,
  `verify.py`, `orchestrator.py`, `_pty_backend.py`, `remote/tunnel.py`,
  `token_meter.py`, `logs_panel.py`, `cli_server.py`) already carried
  `"utf-8", errors="replace"`.
- `.decode("utf-8")` calls *without* `errors=` in `cli.py`,
  `remote/api.py`, `remote/http_server.py`, `limit_status.py`,
  `browser_chrome.py` are decoding the cockpit's own internal
  JSON-RPC/HTTP wire payloads (not external-CLI output) — a different
  risk class where the bytes are UTF-8 by protocol construction, and
  silently `replace`-ing a corrupt frame would just turn one failure
  mode (`UnicodeDecodeError`) into a more confusing one
  (`json.JSONDecodeError` on mangled text). Left as strict, on purpose.

## Fix approach: per-site kwargs, not a new wrapper

Considered introducing a shared `run_text()` subprocess wrapper first
(the task explicitly asked me to weigh this). Rejected it:

- The call sites aren't uniform — some are `subprocess.run` one-shots,
  two are `subprocess.Popen` long-lived streams (`provider_usage.py`,
  `graft_autobuild.py`) with different kwarg shapes (`bufsize`,
  `stdin=PIPE`, `.communicate()`), so a single wrapper wouldn't cover
  every shape without becoming a second, parallel subprocess API.
- Every site still needs its call-site line touched either way (to
  swap the import/function name), so a wrapper doesn't shrink the diff.
- The codebase already had a working idiom for this
  (`encoding="utf-8", errors="replace"` inline, see `git_status.py` /
  `project_rules.py`) — extending that idiom everywhere is the smaller,
  lower-risk diff and matches existing conventions instead of adding a
  new abstraction only some call sites can actually use.

## Regression guard

`tests/test_subprocess_text_encoding_guard.py` — new AST-based test,
sibling to the existing `tests/test_subprocess_no_window_guard.py`
(same call-site discovery: `subprocess.run/Popen/call/check_output/
check_call`, same `# subprocess-encoding-ok: <reason>` escape hatch).
Parametrized one test per file under `src/agent_takkub/`; fails loudly
naming file:line if a text-mode call (`text=True` or
`universal_newlines=True`) is missing `encoding=` and/or `errors=`.

Independently re-verified with a standalone AST walk after the fixes
(not just the pytest run): **0 violations** across the whole
`src/agent_takkub` tree.

## Testing

- `pytest tests/test_subprocess_text_encoding_guard.py` — 73 files
  parametrized, all pass (0 violations).
- `pytest tests/test_subprocess_no_window_guard.py` — unaffected, still
  passes (no regression on the sibling invariant).
- Targeted tests for every touched module: `test_doctor.py`,
  `test_mcp_bridge.py`, `test_limit_status.py`, `test_graft_autobuild.py`,
  `test_provider_usage.py`, `test_provider_install.py`,
  `test_remote_settings_dialog.py`, `test_worktree_manager.py`,
  `test_release.py`, `test_cmd_release.py`, `test_issues.py` — all pass.
- `py_compile` on every edited file — clean.
- Live repro on this machine (`locale.getpreferredencoding() ==
  'cp874'`): unpatched call shape reproduces the exact reported
  traceback; patched call shape does not.

All tests run via the shared repo `.venv`
(`C:\Users\monch\WebstormProjects\agent-takkub\.venv`) with
`PYTHONPATH` pointed at this worktree's own `src/` — the venv's
editable install currently resolves `agent_takkub` to the **main
tree**, not this worktree (confirmed via
`python -c "import agent_takkub; print(agent_takkub.__file__)"`), and
`pip install -e .` against the shared venv is explicitly forbidden
(#202). `PYTHONPATH` override was verified to correctly redirect
resolution to this worktree's source without touching the shared venv's
`.pth` file.
