# Review: New project with AI-generated rules

Date: 2026-06-01  
Reviewer: codex

Scope checked:
- `src/agent_takkub/main_window.py:2498` currently does direct "select folder -> map subdirs"; no New/Import split yet.
- `src/agent_takkub/claude_update.py:67` / `:285` already has a headless `claude -p` precedent with `encoding="utf-8"`, `errors="replace"`, `SUBPROCESS_NO_WINDOW`, timeout 150s.
- `src/agent_takkub/orchestrator.py:1260` / `:1275` shows the spawned-pane Claude policy: `--dangerously-skip-permissions`, `--setting-sources project,local`, explicit plugin/MCP config.
- `src/agent_takkub/lead_context.py:204` renders cockpit `CLAUDE.md` + injected project context for Lead.

## Findings

### 1. UI thread blocking is the first ship blocker

Do not run `claude -p` synchronously inside `_on_add_project_clicked`. A first-run Max/OAuth CLI call can take 30-150s, hit model overload, wait on auth/settings, or hang on hooks. If this runs on the PyQt main thread, the cockpit freezes and cancel is impossible.

Recommendation:
- Use `QThreadPool`/worker like the existing update worker pattern, or `QProcess` if backend wants easy cancel/kill.
- Show a modal/progress state with Cancel. Cancel must terminate the child process, not just hide the dialog.
- Reuse the subprocess hygiene from `claude_update._run`: no shell, `capture_output`, `encoding="utf-8"`, `errors="replace"`, `SUBPROCESS_NO_WINDOW`.
- Prefer a timeout around 150s initially. This matches the existing compatibility analyzer and is long enough for reasoning, short enough to avoid a dead pane. 60s may be too aggressive on overloaded Max; 300s feels too long for a dialog.

### 2. Print-mode flags should be deliberately minimal

`claude -p <prompt>` is already used in `claude_update.analyze_compatibility()` and the comment says it works with the user's existing Claude auth, including Max OAuth. That is the right baseline for this feature too.

Recommendation:
- Start with `[claude, "-p", prompt]` for pure markdown generation.
- Add prompt-level constraints: "Return only project rules markdown. Do not include fences, preamble, or explanation."
- If available in the installed Claude CLI, add `--output-format text` explicitly; otherwise plain `-p` stdout is acceptable.
- Do not add `--dangerously-skip-permissions` for this generation step unless tools are genuinely needed. Pure generation should not need tool permissions.
- Consider `--setting-sources project,local` only if generation runs with `cwd=<project>`. Otherwise user-level hooks/plugins may add noise or failures. This flag is already the cockpit's standard mitigation for broken user-level settings in spawned panes.

Blind spot: if backend runs `claude -p` with `cwd=<project>`, Claude may auto-read existing project `CLAUDE.md`/settings and bias the generated output. That can be useful for Import, but for New it should be an explicit choice.

### 3. OAuth and permission edge cases

Max OAuth should work because the existing headless analyzer depends on the same installed Claude CLI/auth path. Failure modes are still user-facing:

- Not logged in / expired OAuth: return clear error with "open a Claude pane or run `claude` once" style recovery.
- CLI binary missing: reuse `find_claude_executable()` from `config.py:251`.
- Model overload / 529: show retry; generation is not critical enough to silently fall back unless the CLI itself supports fallback in print mode.
- Settings hook crash: if stderr contains hook/settings failures, retrying with `--setting-sources project,local` is reasonable.
- Permission prompt / tool prompt: this indicates the prompt or settings caused tool use. For this feature, fail and tell user to edit manually rather than sending dangerous permission bypass by default.

### 4. Writing `<project>/CLAUDE.md` is correct, but needs overwrite policy

Storing rules at `<project>/CLAUDE.md` matches Claude auto-discovery for teammates because they spawn with `cwd` inside the project path. It is also transparent and portable outside cockpit.

Must handle before ship:
- If `CLAUDE.md` already exists, never overwrite directly. Show existing content and offer Append / Replace / Cancel, with Replace requiring explicit confirmation.
- For monorepos, the selected root may not be the same as the active role paths. A root-level `CLAUDE.md` may affect every package. That is probably desired for workspace-wide rules, but the UI should make the target path visible before save.
- If user maps subdirectories after generation, generated rules may belong at the common root while teammates spawn in subdirs. Claude normally walks upward, so root `CLAUDE.md` should be discovered, but this should be smoke-tested on Windows paths.
- The file should be UTF-8. Use atomic write to avoid a truncated rules file if the cockpit exits mid-save.

Cockpit-managed hidden storage is worse for this feature because teammates rely on cwd auto-discovery. It would require extra prompt injection for every role and would not work when the user opens the project outside takkub.

### 5. Important Lead-context duplication risk

Current Lead spawn behavior intentionally skips `_render_lead_context()` when `spawn_cwd == REPO_ROOT` (`orchestrator.py:1183`). That prevents the cockpit project from receiving cockpit `CLAUDE.md` twice.

The new design says "Lead reads through lead_context injection". If backend changes `_render_lead_context()` to also append the active project's `CLAUDE.md`, guard this carefully:

- If active project path is `REPO_ROOT` or the resolved project `CLAUDE.md` is the same file as cockpit `REPO_ROOT/CLAUDE.md`, do not append it.
- Cap injected project rules size, e.g. 8-12KB, and mark truncation. A generated rules file can become huge and every Lead spawn pays the token cost.
- Avoid recursive wording like "read CLAUDE.md" inside the injected markdown causing the Lead to re-open/inject the same content manually.
- Preserve the existing skip path for cockpit self-work. This is the highest-risk blind spot in the design because agent-takkub itself already has a large cockpit `CLAUDE.md`, and duplication would silently bloat every Lead spawn.

For teammate panes, no extra injection is needed if `CLAUDE.md` lives in the project tree and `cwd` is correct.

### 6. Error and cancel paths likely to be missed

Checklist before ship:
- User cancels New/Import chooser.
- User cancels prompt dialog.
- User cancels generation while process is running; child process is killed.
- Claude returns non-zero with stderr.
- Claude returns zero but empty stdout.
- Claude returns fenced markdown or chatty preamble; UI should still let user edit, but save should not include accidental code fences if avoidable.
- Existing `CLAUDE.md` detected.
- Save fails due to permissions / readonly / OneDrive lock / antivirus lock.
- Project root deleted or moved while generation dialog is open.
- Duplicate project name in `projects.json`; current code keys by folder name, so two different roots with same basename collide.
- User completes rules generation but then cancels path mapping. Decide whether generated file remains; preferably ask before writing until after the whole project add flow is confirmed.
- Concurrent Add Project clicks: disable the button while the flow is active.

## Bottom line

The feature shape is sound: use Claude CLI print mode, let the user edit, save `CLAUDE.md` in the project so cwd auto-discovery works. The main pre-ship risks are UI blocking, unsafe overwrite of existing `CLAUDE.md`, and accidental Lead-context duplication/token bloat when the active project is `agent-takkub` itself or when project rules are injected in addition to auto-discovery.
