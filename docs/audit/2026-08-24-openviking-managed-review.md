# OpenViking Managed-Local — Security + Correctness Review

Reviewer pass over `main` from `0999c2e` (v1.4.1) to `HEAD` (`ea3ed05`), covering all three
`feat(openviking-managed)` waves:

- `a45d4d0` — Wave 1: installer + process supervisor + manager + port picker
- `0ed6d8c` — Wave 2: Setup Wizard + Settings UI managed-runtime controls + boot wiring
- `e41ec94` — Wave 3: CLI managed lifecycle + doctor + studio + docs

Checklist basis: `docs/plans/openviking-managed-local-2026-08-24/18_ACCEPTANCE.md`,
`06_SECURITY_PORT.md`, `05_PROCESS_LIFECYCLE.md`. Read-only review — no files modified.

## Verdict: **SHIP with 1 fix required before general availability**

Process-kill safety, port/bind discipline, and command-construction are solid and correctly
mirror `remote/tunnel.py`'s already-hardened pattern. One real gap: the setup wizard's API key
never goes through `SecretManager`/redaction the way `06_SECURITY_PORT.md` mandates — it lands
in a world-readable plaintext file and can be echoed unredacted into the Settings UI's log
viewer. This should be fixed before OpenViking is promoted from opt-in feature-flag to a
default-recommended path — it's a real secret-at-rest exposure, not a theoretical one, but it
is scoped to a device the user already controls and requires the API key to actually be entered
(step is optional — `ollama` needs none).

Counts: **1 high, 1 medium, 2 low/informational.**

---

## HIGH — API key bypasses `SecretManager`/redaction, spec says it must not

**Files:** `src/agent_takkub/openviking_setup_dialog.py:127-132`, `src/agent_takkub/config.py:82-101`,
`src/agent_takkub/openviking/process.py:296-329`, `src/agent_takkub/settings_knowledge_design.py:538-543`

`06_SECURITY_PORT.md` line 13: *"Secrets through SecretManager / service state with redaction."*
`15_TEST_PLAN.md` line 9 lists `redaction` as a required unit test. Neither happened:

1. `OpenVikingSetupDialog.build_ov_conf()` puts the user's embedding/VLM API key straight into
   the `ov.conf` dict, and `write_ov_conf()` → `config._write_json_atomic()` writes it as plain
   JSON with no `os.chmod`/permission hardening anywhere in the call chain (verified: no
   `chmod`/`0o600` call exists in `config.py`, `installer.py`, or `openviking_setup_dialog.py` —
   grepped the whole package). On POSIX the file lands with the process umask's default mode
   (typically `0644`), readable by any local user/process on a shared machine. This is the same
   codebase's other secret paths (`settings_knowledge_design.py:786,1119`) *do* use
   `SecretManager().set_secret(...)`/`get_secret(...)` for MCP and Penpot credentials — the
   OpenViking wizard is the one secret-entry path in this feature set that was never routed
   through it.
2. `OpenVikingProcess._drain_output()` (`process.py:296-329`) appends every line of the child
   `openviking-server` process's raw stdout to `LOG_FILE` and keeps it in `_last_output`, with
   **no call to `core/secrets/redact.py`'s `redact()`** — a function that already exists in this
   exact codebase for exactly this purpose (stripping `api_key`/`Bearer`/`sk-...` shapes out of
   log lines) and is not imported anywhere in `openviking/`. If `openviking-server` logs its own
   loaded config on startup (common for services, and this one takes an api_key straight from a
   file it reads at boot), the key lands in `openviking.log` unredacted.
3. `settings_knowledge_design._on_kd_ov_view_logs_clicked()` (line 538-543) then reads that same
   `LOG_FILE` and renders up to the last 20,000 characters **verbatim** in a Settings UI dialog —
   so even a user who never opens `ov.conf` directly can have the key surfaced back to them (or
   to a screenshot/screen-share) through "View Logs".

**Failure scenario:** user fills in a real `embedding_api_key` (e.g. an OpenAI key) in the Setup
Wizard, clicks "Install & Start". `ov.conf` now holds that key in cleartext at
`~/.agent-takkub/services/openviking/config/ov.conf` with default file permissions. If
`openviking-server` echoes its config at startup (unverified against the actual upstream binary
in this review — flagging as the plausible mechanism, not a confirmed log line), the same key
also lands in `openviking.log` and is then shown unredacted in the Settings UI's "View Logs"
panel.

**Fix:** route the API key through `SecretManager` (store `secret://openviking/default`, keep
`ov.conf` holding only a reference or omit the field and inject at spawn time via env var the
same way other providers already do elsewhere in this codebase) — or, at minimum, `os.chmod(CONFIG_FILE, 0o600)`
after every write (POSIX; Windows ACLs are a separate, lower-priority item since the default
Windows ACL already scopes to the owning user account) and run `redact()` over every line before
it hits `LOG_FILE`/`_last_output` in `process.py`.

---

## MEDIUM — `ov managed repair` doesn't actually recreate the venv

**File:** `src/agent_takkub/openviking/installer.py:89-93,158-173`

`11_UPDATE_REPAIR.md` line 10: *"Repair: recreate managed venv."* `cli.py`'s `repair` subparser
help text says the same: *"recreate the managed venv, preserving config/data"*. The
implementation is `ensure_installed(force=True)`, which — once `force` bypasses the
`is_installed()` early-return — calls `_create_venv(VENV_DIR)` directly. `_create_venv` runs
`python -m venv <VENV_DIR>` with no `--clear` flag and never removes `VENV_DIR` first.

Verified empirically this session: `python -m venv` against an **existing, non-empty** target
directory returns exit code 0 and leaves unrelated pre-existing files in `site-packages`
untouched — it does not clear/recreate the environment, only re-lays the interpreter
symlinks/`pyvenv.cfg` on top. So a genuinely broken managed venv (corrupted/partial packages,
a bad dependency pin dragged in by a prior `pip install`, disk-full mid-write) is **not** fixed
by `repair` — only `openviking` itself gets re-installed via the subsequent `pip install
--upgrade`, while whatever actually made the venv "broken" (e.g. a stale/incompatible
dependency, a half-written `.dist-info`) survives untouched.

`tests/test_openviking_installer.py::test_force_reinstalls_even_when_already_installed` gives
false confidence here — it mocks `_run` entirely (asserts call count == 4) and never exercises
real `venv` semantics against a pre-existing directory, so this gap is invisible to the test
suite.

**Fix:** `shutil.rmtree(venv, ignore_errors=True)` before `_create_venv` when `force=True` (or
pass `--clear` to the `venv` invocation), matching what "recreate" actually means in the spec.

---

## LOW / informational

**1. PID-reuse on the target process itself is not guarded (inherited limitation, not a new
regression).** `process.py`'s `reap_orphan_process()`/`stop()` (mirroring `remote/tunnel.py`'s
identical, already-shipped pattern) verify the **owner** (the cockpit process that spawned
`openviking-server`) is still alive via `owner_pid`+`owner_create_time` before reaping, but once
that check passes, `_tree_kill(pid)` targets the recorded `pid` with no equivalent freshness
check on the *target* process itself. If `openviking-server`'s PID has since been reused by an
unrelated process after it exited (classic PID-reuse race — narrow window, needs the owner
cockpit to also be gone/reused for the reap path, or `stop()` to race an unrelated fast-reuse),
that unrelated process gets `taskkill /T /F` (Windows) or `killpg` (POSIX) treatment. This is
identical to `tunnel.py`'s existing, accepted design (same docstring, same technique, explicitly
called out as deliberately duplicated) — not something this PR introduced or worsened. Flagging
for awareness only; not blocking.

**2. `closeEvent`'s `manager.stop()` call blocks the UI thread up to ~5s** (`process.py:373-376`'s
`proc.wait(timeout=5)`) during app shutdown. Consistent with the rest of `closeEvent`'s already-
synchronous teardown (`pane.session.terminate(wait=True)`, `self._remote.stop()`) — not a new
pattern, not flagged as a defect.

---

## Acceptance checklist verification (`18_ACCEPTANCE.md`)

| Item | Status | Evidence |
|---|---|---|
| no Docker | ✅ | `installer.py` — dedicated venv + `pip install`, no Docker anywhere in the diff |
| optional OpenViking | ✅ | `openviking_adapter.enabled()` gates every entry point; `manager.status()`/`start()` both return zero-cost when disabled |
| managed isolated runtime | ✅ | Fixed `~/.agent-takkub/services/openviking/venv` — separate from Cockpit's own venv |
| one-click install | ✅ | Setup Wizard "Install & Start" → `ensure_installed()` + `start()` in one click |
| auto-start | ✅ | `boot_wiring()`, gated on `cfg.enabled and cfg.start_automatically`, backgrounded thread |
| owned auto-stop | ✅ | `manager.stop()` only touches `self._owned` process; wired into `closeEvent` |
| external process never killed | ✅ | `stop()`/`restart()` check `_owned`; `start()` marks `owned=False` for both `already_healthy` port-occupant and `TAKKUB_OPENVIKING_URL` override paths |
| localhost only | ✅ | `port.py` binds/probes `127.0.0.1` exclusively; `build_ov_conf` sets `server.host = "127.0.0.1"`; no `0.0.0.0` anywhere in the diff |
| port conflict handling | ✅ | `pick_port()`: free → use; occupied+healthy → reuse read-only; occupied+unhealthy → OS-assigned free port. TOCTOU explicitly documented as an accepted limitation |
| setup UI | ✅ | `openviking_setup_dialog.py` |
| masked secrets | ⚠️ | Entry field uses `QLineEdit.EchoMode.Password` (masked in UI) and the wizard never round-trips a saved key back into the form — but see **HIGH** finding above: masked *entry* ≠ secured *at rest* |
| health/doctor | ✅ | `port.is_healthy()`, `installer.run_doctor()`, `doctor.check_openviking_managed()` |
| Open Studio | ✅ | `cmd_ov_managed`'s `studio` action |
| strict project isolation preserved | ✅ | `openviking_adapter.py` diff is additive-only (`set_runtime_url`/`base_url` fallback chain); `apply_scope_and_trust`/scope-gate code paths untouched |
| fail-open | ✅ | Every manager method catches and degrades to `error=`/disabled-for-session rather than raising into callers; `boot_wiring()` wraps both steps in `try/except` |
| update/repair/remove | ⚠️ | `update`/`remove` correct; **`repair` doesn't actually recreate the venv — see MEDIUM finding** |
| Windows/macOS tests | ✅ | `sys.platform == "win32"` gates present with POSIX branches at every OS-specific call site (`process.py` Job Object vs. `start_new_session`/`killpg`, `installer.py` `Scripts/`+`.exe` vs. `bin/`) |
| CI green | not verified this pass | out of scope for a read-only code review — defer to CI status |
| #362 untouched | ✅ | `git diff --stat` shows no V2-authority/Phase-10 files touched; only `openviking/`, `cli.py`, `doctor.py`, `main_window.py`, `openviking_settings.py`, `openviking_setup_dialog.py`, `settings_knowledge_design.py`, `openviking_adapter.py` (additive), plus tests and `depgraph.json` |

## Scope notes

- Command construction (installer's `pip install`, process's `openviking-server` argv) is 100%
  built from internal constants/ints — no shell, no string interpolation of user input into
  argv. No injection vector found.
- `uninstall()`/`_cmd_ov_managed_remove` only ever touch fixed paths under the module's own
  `OPENVIKING_HOME` constant — no path traversal surface (nothing user-supplied reaches a path
  join).
- This review did not attempt to install the real `openviking` PyPI package or run
  `openviking-server` to observe its actual startup log content — the redaction finding's
  "config gets echoed to stdout" mechanism is the plausible, unverified half of an otherwise
  fully-verified gap (no redaction call exists in the code path regardless of what the upstream
  binary actually logs).
