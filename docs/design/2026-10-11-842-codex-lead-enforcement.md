# #842 — Restricted Lead enforcement for Codex (threat model + design)

## Asset and policy

When the effective team policy says `lead_may_implement: false` (presets `full`, …),
the Lead may read, brief, assign and report — it must not change project files.
Before this change a Codex Lead on Windows ran with
`--dangerously-bypass-approvals-and-sandbox`: every native tool (shell, `apply_patch`,
scripts, `spawn_agent` children) could write anywhere. Prompt text was the only control.

## Threats (who/what can write project files)

| # | Path | Before | After (Codex Lead, restricted) |
|---|------|--------|--------------------------------|
| T1 | shell redirection / scripts (Python, PowerShell, node) | allowed | OS sandbox denies (verified, see Evidence) |
| T2 | `apply_patch` | allowed | denied in a live Codex TUI turn; protected file remained byte-identical |
| T3 | new git worktree / alias path outside the project root | allowed | denied: base is `:read-only`, only listed roots are writable |
| T4 | staging in `%TEMP%` then copying in | allowed | `%TEMP%` not writable; Lead gets its own scratch dir under `runtime/` |
| T5 | role/env spoofing (`TAKKUB_ROLE=`) to reach privilege routes | `team set` unlock judged by caller-supplied `from` | also judged by the Lead capability token (`auth`), not only `from` |
| T6 | policy tampering (team preset file) | writable | outside every writable root (`projects/<slug>/team_preset.json`) |
| T7 | native subagents (`spawn_agent`) | inherit full access | inherit the sandbox (same process tree) |
| T8 | MCP servers (run outside the sandbox) | unrestricted | disabled for restricted Codex Lead, including inherited and plugin MCPs |
| T9 | policy flips to restricted while an unrestricted Lead is running | unnoticed | readiness reports "not enforced — restart Lead"; Lead-only CLI refuses until respawn |

## Design

* **Boundary = Codex's own OS sandbox**, not hooks. Codex hooks fail open on error,
  need interactive trust, and the vendor documents them as "not a complete enforcement
  boundary"; they stay out of this change.
* Session-scoped profile, never `~/.codex/config.toml`:
  `-c permissions.takkub_lead={extends=":read-only", filesystem={<root>="write",…}, network={enabled=true, domains={"127.0.0.1"="allow", "localhost"="allow"}}}`
  `-c default_permissions="takkub_lead"` `--ask-for-approval never`.
  Writable roots: the project's docs + artifacts dirs (`TAKKUB_DOCS_DIR`,
  `TAKKUB_ARTIFACTS_DIR`), `runtime/lead-readiness`, a Lead scratch dir
  (`runtime/lead-tmp/<project>`, also `TEMP`/`TMP`). The event log is not writable.
  Loopback network stays on: the `takkub` CLI talks to the cockpit over TCP.
  The TUI also enables Codex's network proxy.
* Applies only when the policy at spawn is restricted. `solo-lead`/`pair` keep the old flags.
  Unsupported restricted providers refuse to spawn, including Claude (its hooks
  do not cover shell writes). Codex uses `--no-daemon` and `--strict-config`.
* Before Codex spawn, reject legacy `sandbox_mode` in user/project configs,
  then run a disposable command under the installed Codex sandbox. It must
  write its scoped scratch root and fail to alter a protected sentinel. This
  preflight makes no model/API call.
* A restricted Codex Lead gets an empty MCP table and disabled plugins/apps.
  The CLI server rejects any `team set`/`clear-override` that would unlock an
  already restricted policy, regardless of caller claims or process ancestry.
* On protected Codex session exit, the cockpit reads Codex's rollout tool
  outcomes and emits `lead_native_denied` events with role, project, task,
  tool, project-relative target and reason. It never copies command input or
  tool output into the event log. Missing rollout data emits
  `lead_native_audit_unavailable`; this audit is best-effort and does not
  replace the sandbox boundary.
* Readiness records the enforcement mode (`codex_sandbox` / `none`). A restricted policy
  with mode `none` is reported as not ready ("restart Lead to enforce"), so a policy change
  can never leave an unrestricted Lead looking protected.

## Evidence (Windows 11, codex-cli 0.162.1, `codex sandbox -P takkub_lead`)

Disposable dirs under `C:\Users\monch\takkub-sbx-probe-842`, probe = Python `open(..., "a")`:

* project file (also the cwd) → `PermissionError`, file byte-identical afterwards
* new file in project → `PermissionError`
* `%TEMP%` → `PermissionError`
* granted root → written
* loopback TCP → connects
* an undefined profile name → Codex refuses to load the config (profile is validated)
* 2026-10-11 follow-up with the same profile and disposable Git repo: `git status`
  succeeded; Python write, `cmd /c` redirection, PowerShell `Set-Content`, a
  junction alias write, and `git worktree add` were rejected. The sentinel
  remained byte-identical. Codex sandbox exit codes were 1 for writes and
  255 for worktree creation. A Python symlink attempt was unavailable on this
  Windows setup, so the alias case used a junction.
* Live Codex TUI 0.162.1, one authorized model test in a disposable repository at
  `C:\Users\monch\AppData\Local\Temp\takkub-842-live-j2om17xd\project`,
  session `01a12ae0-5f50-7752-8e20-0501497222dc`: `git status` ran;
  `apply_patch` was rejected with "writing outside of the project; rejected by
  user approval settings"; PowerShell `Set-Content` on `sentinel.txt` failed
  with access denied; a scratch write succeeded; Python `socket.connect_ex` to
  `1.1.1.1:443` returned 10013 (`connected=False`). The sentinel was
  `b'PROTECTED-842\r\n'` before and after. No approval was requested.
* Isolated installed-wheel probe on Windows: built wheel at commit `99491e82`,
  installed into a separate venv under
  `C:\Users\monch\AppData\Local\Temp\takkub-842-install-25691011191756`,
  verified import came from that venv's `site-packages`, then ran the shipped
  `verify_codex_boundary` against a disposable project. Allowed scratch write
  and denied protected write both passed. This did not replace the running
  Cockpit installation or restart its Lead.
* Isolated Cockpit activation with that wheel: separate `AGENT_TAKKUB_HOME`,
  disposable project, `full` preset, Codex provider, and a real `HeadlessPane`.
  After the initial cold-start failures, `Orchestrator.spawn("lead")` returned success and the
  pane state recorded `lead_enforcement_mode=codex_sandbox`; the pane was then
  terminated. The first cold-start attempt failed closed when the sandbox
  preflight exceeded its 25-second timeout. The source timeout is now 60
  seconds. No model call was made in either activation attempt.

## Residual gaps (not claimed as protected)

* **macOS**: same flags, Seatbelt backend — not run on a Mac yet.
* **Denied-attempt audit**: structured events are emitted at session exit for
  recognized Codex tool failures. It is not a guaranteed record of every
  denied operation: a missing session ID, missing/truncated rollout or a new
  error format can omit an event. Attribution requires the exact Codex session
  ID and cwd; the collector does not guess from another pane's newest rollout.
  Existing sessions do not gain this collector until restarted.
* **Policy flips while an unrestricted Lead runs**: readiness becomes false,
  but that old process must restart to gain the OS sandbox.
* **Command network egress**: a live Windows TUI denied one direct external
  socket attempt. The no-model `codex sandbox` subcommand still reached
  `1.1.1.1:443` with the same allowlist because it did not start the proxy.
  Other networks, platforms, and provider versions remain unverified.
