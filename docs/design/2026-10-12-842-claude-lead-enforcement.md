# #842 / #866 — Restricted Lead enforcement for Claude (threat model + design)

Companion to `2026-10-11-842-codex-lead-enforcement.md`. Same policy and asset:
under `lead_may_implement: false` (preset `full`, …) the Lead may read, brief,
assign and report, and must not change project files.

## Why this exists (#866)

2.2.15 refused every restricted Lead that was not Codex, on the grounds that
Claude's PreToolUse hooks do not cover shell writes. That is true of the hooks,
but the refusal made the default setup (Claude Lead + `full` preset) unusable:
on prod the Lead never started, and the reason only reached a 30-second
status-bar line. 2.2.16 gives Claude a real boundary instead.

## Boundary

Claude Code has no OS sandbox on native Windows (its Bash sandbox runs on
macOS, Linux and WSL2 only). The boundary is Claude Code's own permission
engine, which the vendor documents as enforced by Claude Code, not the model:

* `--permission-mode dontAsk` replaces `--dangerously-skip-permissions`.
  Every call that would prompt is denied — no prompt, so the Lead never stalls
  waiting for Enter (the reason the old deny-rule guard was removed).
* `permissions` in the per-project `--settings` file
  (`runtime/hook-settings-lead-<project>.json`):
  * allow `Bash`/`PowerShell` for `takkub *`, `gh issue *`, `gh pr view|list|checks|diff *`,
    `gh run list|view *`;
  * allow `Edit(//<root>/**)` only for `$TAKKUB_DOCS_DIR` and `$TAKKUB_ARTIFACTS_DIR`
    (Edit rules cover every file-editing tool; `Write(path)` rules are not matched);
  * deny `takkub verify`, `takkub worktree merge|clean`, `takkub services`,
    `takkub cleanup` — they run project code or write the project from the CLI
    process, which a sandboxed Codex Lead could not do either — and
    `gh issue develop` (`--checkout` switches the project's branch);
  * `disableBypassPermissionsMode` and `disableAutoMode` = `disable`.
* `--disallowed-tools Agent`: a subagent definition may set
  `permissionMode: acceptEdits`, which runs even under a dontAsk parent.
* rtk's PreToolUse rewrite is left out of this settings file: it would turn a
  built-in read-only `git status` into `rtk git status`, which dontAsk denies.
* Fail closed: Claude Code older than 2.1.267, an unwritable settings file or a
  write root overlapping the project → the Lead does not spawn, the reason is
  logged (`lead_spawn_failed`) and raised as a Lead-unavailable alert.
* The readiness ack is not an Edit root: `takkub context read` runs as an
  allowed command and writes it, so the model cannot forge it with Edit.

Readiness records `claude_permissions`; `takkub context status` reports
`native_tool_gate: permission_allowlist`. A live Claude Lead spawned
unrestricted (mode `none`) blocks a preset change to restricted, exactly like
Codex: close Lead, change preset, spawn again.

## Threats

| # | Path | Claude Lead, restricted |
|---|------|-------------------------|
| T1 | shell redirection / interpreters (Python, PowerShell) | denied (not allowlisted) |
| T2 | Write/Edit tools into the project | denied (outside Edit roots) |
| T3 | git that writes (`checkout -b`, `worktree add`) | denied; read-only git still runs |
| T4 | staging in `%TEMP%` | denied (not an Edit root; shell writes not allowlisted) |
| T5 | role/env spoofing | unchanged: Lead token on the CLI server |
| T6 | policy tampering (preset file) | outside every Edit root; `team set` unlock refused server-side |
| T7 | native subagents | `Agent` tool removed |
| T8 | skills with `allowed-tools` | `Skill` is denied in dontAsk (not allowlisted) |
| T9 | bypass/auto mode via settings or subagent | disabled in the session settings |
| T10 | `takkub` subcommands that execute locally | the five above denied; the rest go through the cockpit server's own checks |

## Evidence (Windows 11, Claude Code 2.1.296, disposable git repo)

Probe: `claude -p --model claude-haiku-5-5 --permission-mode dontAsk` with the
settings file rendered by `hook_wiring.ensure_hook_settings_file(permissions=…)`
and `--disallowed-tools Agent`, sentinel `PROTECTED` (sha256 `0db5bf3c…`).

* denied: Write tool, `echo > sentinel.txt`, `python -c open(...,'w')`,
  PowerShell `Set-Content`, `git checkout -b`, `cp`, `takkub verify`,
  `rtk git status`, Skill tool, Agent tool
* allowed: `git status --short`, `cat`, `takkub --help`, `gh issue list`,
  Write to the granted root
* sentinel byte-identical afterwards; no file created in the project
* without `--disallowed-tools Agent`, a project subagent with
  `permissionMode: acceptEdits` created `agent.txt` → why Agent is removed

## Gaps (not claimed)

* No OS-level layer for Claude: the boundary is Claude Code's command parser
  and allowlist. An allowed program that itself writes (a future `takkub`
  subcommand, `gh` extensions) is outside it.
* `AskUserQuestion` is denied in dontAsk — the Lead asks in plain text.
* Skills are unavailable to a restricted Claude Lead.
* macOS: same Claude Code engine, not run live on a Mac. Claude's Bash sandbox
  could add an OS layer there; not enabled yet.
* gemini/opencode/cursor still refuse a restricted Lead (no boundary).
