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
| T2 | `apply_patch` | allowed | applied by Codex under the same sandbox policy — **live check pending** |
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
  `-c permissions.takkub_lead={extends=":read-only", filesystem={<root>="write",…}, network={enabled=true}}`
  `-c default_permissions="takkub_lead"` `--ask-for-approval never`.
  Writable roots: the project's docs + artifacts dirs (`TAKKUB_DOCS_DIR`,
  `TAKKUB_ARTIFACTS_DIR`), `runtime/lead-readiness`, a Lead scratch dir
  (`runtime/lead-tmp/<project>`, also `TEMP`/`TMP`). The event log is not writable.
  Network stays on: the `takkub` CLI talks to the cockpit over loopback TCP.
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

## Residual gaps (not claimed as protected)

* **apply_patch / live TUI session**: profile accepted by the config loader; an end-to-end
  turn of a real Codex Lead under the profile has not been run (would use the model).
* **macOS**: same flags, Seatbelt backend — not run on a Mac yet.
* **Denied-attempt audit**: OS writes are denied, but structured Cockpit audit
  events for each attempted tool/target are not yet available.
* **Policy flips while an unrestricted Lead runs**: readiness becomes false,
  but that old process must restart to gain the OS sandbox.
* **Command network egress**: `network.enabled=true` is required for the
  Cockpit CLI's loopback TCP connection. A no-model `codex sandbox` probe on
  Windows reached `1.1.1.1:443` even with `features.network_proxy=true` and a
  `localhost`/`127.0.0.1` domain allowlist; that standalone subcommand did not
  start the proxy. The actual TUI network boundary is unverified. Do not claim
  the profile prevents network-based remote implementation or publication.
