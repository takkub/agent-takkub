# #243: teammates walk into permission gates blind, then stall unattended

## Incident

2026-08-15: `backend#3` was clearing a merge conflict and ran
`git reset --hard <sha>` to move HEAD onto a hand-made merge commit. That
command matches `.claude/settings.json`'s `permissions.ask` rule
`Bash(git reset --hard:*)`. An `ask` rule is the one thing
`--dangerously-skip-permissions` cannot bypass by design — so the pane sat
at an unanswered y/N prompt. Someone was awake to press it that time; in an
unattended overnight run nobody would be, and the whole wave stalls with
no signal (`takkub status` still reports `working` — that's #236, a
separate detection-side issue).

Root cause: nothing told the pane, at spawn time, which commands its own
project's settings would gate, or what to do instead of typing one and
waiting.

## Fix

New leaf module `src/agent_takkub/permission_gates.py` — single source of
truth read live from `.claude/settings.json` at spawn time, never
duplicated into the 16 role `.md` files (which would drift the instant
settings.json changes, exactly the failure mode the task explicitly
warned against).

### `resolve_claude_ask_rules(cwd)`

Walks up from the spawn cwd to the nearest `.git` boundary (capped at 12
levels), reading `permissions.ask` out of every `.claude/settings.json` +
`.claude/settings.local.json` it finds, merged + de-duplicated. This
mirrors what claude itself actually merges for that pane — every teammate
spawn already passes `--setting-sources project,local`
(`provider_spec.claude_spec.extra_static_args`), so the function reads the
same two sources claude reads, not a guess, and deliberately skips the
user's `~/.claude/settings.json` (not in that source list).

### `render_claude_gate_appendix(cwd)`

For each resolved `ask` rule, renders:
- the gated command pattern
- a gate-free alternative, looked up from a small curated table built from
  the cockpit's *own* current gate list:

  | Gated pattern | Alternative |
  |---|---|
  | `git reset --hard` | `git checkout -B <branch> <sha>` · `git restore --source=<sha> --worktree --staged .` · `git merge/rebase/cherry-pick --abort` · `git restore --staged <path>` |
  | `git push --force` / `-f` / `--force-with-lease` | commit normally on your own branch, report to Lead for review/push (role policy already forbids teammate push) |
  | `npm install -g` / `npm i -g` | `npm install <pkg>` (local devDependency) or `npx --yes <pkg>` |

  A pattern not in the table (settings.json gains a new `ask` rule later)
  still gets listed — with a generic "no known alternative, report FAILED"
  fallback instead of being silently dropped.
- the FAILED-report instruction: `takkub done --fail "ต้องใช้ <คำสั่ง>
  ซึ่งติด permission gate — <รายละเอียด>"`. `orchestrator._build_verify_fail_handoff`
  already prefixes this with `[<role> FAILED] ` and routes it to Lead's
  propose-a-fix-loop path — this reuses that existing protocol rather than
  inventing a new one.

Returns `""` when the project sets no `ask` rules (the overwhelming
majority of spawns) — same "only emit when non-empty" discipline
`lead_context.py`'s substituted-providers section already uses, so a normal
spawn's token cost doesn't move.

### Wiring (`spawn_engine.py`)

- **claude teammate branch**: appended into the same `_appendix` that
  already carries `BIG_FILE_GUARD`/`STALE_FILE_GUARD`/`GRAFT_TOOL_CAVEATS`,
  using that spawn's resolved `spawn_cwd` — the identical pattern those
  three guards use, so this reads as "one more guard," not a new mechanism.
- **non-claude generic branch** (`context_strategy == "agents_md_file"`):
  appended into the `extra` string passed to `ensure_agents_md()`.

Scope: teammates only. Lead was deliberately left untouched —
`cockpit CLAUDE.md`'s Lead policy already forbids Lead from running
`git commit`/`push`/`reset --hard` itself (Lead's job is to delegate), and
the incident + issue title are both about a teammate stalling
unattended. Extending to Lead later is a one-line addition to
`lead_context._build_lead_context_text` calling the same function — the
module is provider/cwd-generic already, nothing role-specific to redo.

## Multi-provider (#103)

Only claude currently has a **persistent, bypass-proof** gate mechanism
the cockpit can enumerate — `.claude/settings.json`'s `permissions.ask`
survives `--dangerously-skip-permissions` by design. Checked every other
registered provider's actual spawn flags (`provider_spec.py`
`autonomy_flags`) for an equivalent:

| Provider | Autonomy flag as spawned by this cockpit | Persistent ask-list mechanism? |
|---|---|---|
| codex | `--ask-for-approval never -s workspace-write ...` (win32: `--dangerously-bypass-approvals-and-sandbox`) | No — "never" means no approval prompt at all in this cockpit's configuration |
| gemini (agy) | `--dangerously-skip-permissions` | No — full bypass, no ask-list surface found in agy 1.1.6 docs |
| opencode | `--auto` ("auto-approve permissions not explicitly denied") | Has a permissions-deny concept per its docs, but the cockpit configures no per-command deny list today — nothing to enumerate |
| kimi | `--yolo` | No — full bypass |
| cursor | `--force` ("Force allow commands unless explicitly denied") | Has an "explicitly denied" concept per docs, but the cockpit configures no deny list today — nothing to enumerate |

None of these are `permissions.ask`-equivalent as currently *configured by
this cockpit* — every non-claude provider is launched with a flag that
disables its own approval gate entirely. This is a real gap, not a
claude-only shortcut taken silently: `render_generic_gate_note()` is
injected into every non-claude teammate's `AGENTS.md` and says so in
plain language — cites the actual autonomy flag the pane was launched
with, states there's no persistent ask-list to enumerate yet, points at
this issue's follow-up (#103), and still tells the pane to
`takkub done --fail` rather than sit on an unanswered prompt if it somehow
hits one anyway (e.g. an interactive login/trust modal the autonomy flag
doesn't cover).

**Hook for a future per-provider resolver**, so this isn't a dead end: if
a provider later gains a configurable deny/ask list (opencode's
`permissions` config and cursor's "explicitly denied" both look like
candidates), the natural extension is a provider-keyed dispatch inside
`permission_gates.py` — `{"claude": resolve_claude_ask_rules, "opencode":
resolve_opencode_deny_rules, ...}` — with `render_generic_gate_note`
staying as the fallback for whichever providers still have nothing
configured. No `spawn_engine.py` change would be needed beyond the one
dispatch call already wired in.

## Item 3 — unattended-mode auto-answer "No" (proposal only, not implemented)

The issue also asked to consider: when cockpit is running unattended and a
pane reaches an actual confirmation prompt (gate list notwithstanding —
e.g. a brand-new `ask` rule this session's context predates, or a
non-claude provider's rare interactive modal), auto-answer "No" instead of
waiting forever, then let the pane report FAILED.

Per the task instructions this is **write-up only** — explicitly not
implemented — pending a safety decision from the user. Sketch, for that
discussion:

- **Detection**: a pane sitting at a known confirm-prompt marker
  (`pty_session.py`'s `READY_HARD_BLOCKERS`/ready-rule tables already
  recognize several — "esc to interrupt", "press enter to continue", etc.)
  for longer than some grace window, only while the cockpit's own
  unattended/overnight mode (if such a mode exists — needs to be defined;
  today there's no explicit "nobody's watching" flag, only
  session inactivity) is active.
- **Action**: send the CLI's own "reject" keystroke (`n` + Enter, or the
  provider-specific equivalent) rather than any destructive keystroke, then
  auto-report `takkub done --fail "auto-answered No to an unanswered
  permission prompt: <captured prompt text>"` on the pane's behalf so Lead
  still sees it and the wave doesn't sit blocked.
- **Why this needs explicit sign-off before building**:
  - A false-positive prompt match could auto-reject a **legitimate,
    wanted** confirmation (e.g. a one-off exception a human operator would
    have approved), silently discarding real work instead of just stalling
    it — stalling is recoverable (a human can find and unstick it later);
    a wrong auto-No might not be (if the pane then gives up or takes a
    different, worse path).
  - It only ever complements the render_claude_gate_appendix /
    render_generic_gate_note guidance above — those two try to prevent the
    pane from ever typing the gated command in the first place. Auto-No is
    strictly a last-resort net for whatever slips past that, so it's lower
    priority than the fix that shipped here.
  - Needs a real "unattended" signal to gate on (so it never fires during
    an attended session where the user might actually want to answer),
    which doesn't exist yet in the codebase as a concrete API — that's its
    own prerequisite piece of work, not a detail to improvise inline here.

## Verification

- `PYTHONPATH=<worktree>/src .venv\Scripts\python.exe -m pytest
  tests/test_permission_gates.py` — 10/10 pass (rule resolution: cwd-level,
  merge settings+settings.local, walk-up-to-git-root, stop-at-git-root,
  dedup across levels; rendering: empty-when-no-rules, known-pattern
  alternative + FAILED instruction, unknown-pattern generic fallback,
  generic non-claude note states the gap).
- Targeted regression: `test_spawn_task_delivery.py`,
  `test_provider_models.py`, `test_project_memory_inject.py`,
  `test_orchestrator_shard.py`, `test_orchestrator_claude_env_leak.py`,
  `test_codex_crash_instrumentation.py`, `test_config.py` — all pass
  (spawn-time appendix building and both claude/non-claude branches
  unaffected).
- `ruff check src/agent_takkub/permission_gates.py
  src/agent_takkub/spawn_engine.py` — clean.
- `lint-imports` — 25/25 contracts kept (new module has no
  orchestrator/UI/CLI edges; `spawn-engine-layer` contract still holds).
- Did not touch `remote/notify.py` (backend#1) or `orchestrator.py`
  (backend#3) — only `spawn_engine.py` (2 small additive blocks) + the new
  `permission_gates.py` leaf module + its test file.
