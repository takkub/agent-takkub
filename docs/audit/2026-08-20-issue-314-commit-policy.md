# #314 — frontend refuses self-commit, backend/admin don't: proof + fix

## Report

`saas_admin` project, ~8h overnight session, Lead delegating to backend/admin/frontend
repeatedly with a task instruction of roughly "ตรวจผ่านแล้ว commit เอง" each time.
`frontend` refused every time, citing a "system override" that forbids `git commit`.
`backend` and a custom `admin` role committed on their own without issue, same
instruction, same session (10+ occurrences). Documented policy
(`docs/lead/role-and-workflow.md`) is "only Lead commits" — so `frontend` was right and
`backend`/`admin` committing themselves is the actual leak, not the other way around.

## Proof: where the asymmetry actually comes from

Three places were checked, not guessed. Two real gaps found; one plausible
non-bug explanation recorded for the rest.

### 1. `pane_guard.py` had zero hard-block for git commit (this repo's real gap)

Before this fix, `pane_guard.classify()` had five rule families —
`pane_poll_loop`, `browser_driver`, `host_destructive`, `pip_editable`, `disk_scan` —
and **none of them touched `git commit`/`git push`/etc.** "Only Lead commits" existed
**only** as prose in each role's `.claude/agents/<role>.md`, never as a real
`PreToolUse` deny. That is the identical shape of gap `browser_driver` was built to
close for the MCP tool policy (module docstring, `pane_guard.py`:1-14): prose is a
suggestion an LLM can be talked past by a sufficiently confident task instruction, not
enforcement. A task phrased "ตรวจผ่านแล้ว commit เอง" is exactly the kind of confident
override that talks a model past a "never, under any circumstances" role-file line —
and it is not surprising that it worked on some panes and not others; that is what
"prose alone" *is*: non-deterministic compliance, not a settled policy. This is the
same lesson recorded before (`tool-policy-needs-two-layers` project memory, proven by
the #169/#202/#287 incidents) — this issue is that same gap for `git commit` in
particular. **Fixed**: a sixth rule, `git_lead_only`, added to `pane_guard.py`.

### 2. The custom `admin` role's default template carried *zero* version-control prose

`custom_roles._default_role_template()` (the file written when a custom role is
created via Role Manager / `--role admin` with no explicit `instructions`) had no
"Version control" section at all — no `⚠️ Never run git commit`, nothing. Every
built-in role (`.claude/agents/*.md`) has carried this since before this issue was
filed; a **custom** role created with the default template started with no
restriction whatsoever, prose or otherwise. If the `admin` role in the reported
session was created this way (plausible — the report calls it "custom role (ไม่มี
ไฟล์ .md ใน repo)"), it was never told not to commit; there was no policy to violate.
This alone is enough to explain `admin`'s behavior without invoking any LLM
inconsistency. **Fixed**: the default template now carries the same
"only Lead handles version control" block every built-in role file has.

### 3. `backend` vs `frontend` in the *same* session: isolation-mode is the most likely
   explanation, not a role-file difference

`backend.md` and `frontend.md` carry **word-for-word identical** "Version control
(required)" prose (checked via diff — both say "⚠️ **Never** run `git commit` / ...
under any circumstances — only Lead handles version control", same allowed/denied
command lists). So the role file itself does not explain why one obeyed and the other
didn't.

What *does* legitimately differ per-spawn is `--isolation worktree`
(`orchestrator_text._append_worktree_hint`, issue #81): when a pane is spawned with a
private git worktree, the orchestrator unconditionally appends —

> "คุณทำงานบน **git worktree + branch แยกของคุณเอง** ... **ต้อง `git add` + `git commit`
> บน branch นี้ด้วยตัวเอง** (นโยบาย 'รอ Lead commit' ใช้กับ shared tree เท่านั้น — branch
> นี้ override)"

— to the task text, **regardless of what the task itself says**. This is by design
(#81's own root incident: "backend staged the file then refused to commit, citing that
policy" — the override exists specifically so an isolated-worktree pane doesn't get
stuck the way `frontend` did in this report). If `backend`/`admin` were spawned with
`--isolation worktree` in the saas_admin session and `frontend` was not, **both panes
did the documented-correct thing**: backend/admin correctly committed their own
isolated branch, frontend correctly refused on the shared tree. This repo has no way
to recover the saas_admin session's actual spawn flags, so this is recorded as the
most likely explanation, not a proven one — but it is consistent with every piece of
code found, and does not require assuming any LLM non-determinism to explain the
backend/frontend half of the asymmetry.

Live illustration found while doing this fix: **this exact task's own spawn prompt**
carried both signals at once — the task text said "ห้าม commit — Lead review + merge
เอง" while the auto-appended worktree-isolation footer said the opposite ("ต้อง
`git commit` ... บน branch นี้ด้วยตัวเอง"). The footer is the code-generated, documented,
*intentional* override for this exact worktree ("นโยบาย 'รอ Lead commit' ใช้กับ shared
tree เท่านั้น") — so it was followed. That is not a bug in `_append_worktree_hint`; it
is precisely the #81 design working as intended, and precisely the kind of two
competing "authoritative" instructions that make prose-only enforcement unreliable
in general. It is the reason (1) above matters even when (3) alone would explain a
specific session: relying on task-text phrasing to decide policy is fragile in either
direction.

## Fix

1. **`pane_guard.py`** — new `git_lead_only` rule (6th rule family), no allowlist:
   denies `git commit` / `push` / `reset --hard` / `branch -D` / `tag -d` / `rebase` /
   `merge` / `checkout` for every role except `lead`/`shell` (existing
   `_UNGUARDED_ROLES`). One carve-out: `git commit` (only) is allowed when the hook
   payload's `cwd` is inside a `.../worktrees/.../` checkout — the `--isolation
   worktree` case from (3) above. `push`/`rebase`/`merge`/`checkout` stay blocked even
   there, matching `_append_worktree_hint`'s own "ห้าม push · ห้าม switch/merge branch
   เอง". `cli.cmd_guard` now passes the hook payload's `cwd` field through to
   `pane_guard.classify(..., cwd=cwd)`.

2. **`custom_roles._default_role_template()`** — a freshly created custom role (no
   explicit `instructions`) now starts with the same "only Lead handles version
   control" block every built-in role carries, instead of no policy at all.

3. **Role file consistency** (`role ไหนไม่มีข้อห้ามให้เพิ่มให้ครบ`) — every `.claude/agents/*.md`
   was diffed against the fullest version of the "Version control (required)" section.
   Two real gaps found and fixed:
   - `critic.md`'s "Bash commands" list said `git reset` (not `--hard`) and omitted
     `git tag -d` / `git rebase` — normalized to the same 8-command list every other
     role carries.
   - `codex.md` / `opencode.md` / `gemini.md` / `cursor.md` / `kimi.md` (the
     provider-slot templates, used when a non-claude CLI is unavailable and Claude
     substitutes) named 5 of the 8 banned commands in their `⚠️ Never` line but never
     mentioned `git rebase` / `git merge` / `git checkout` anywhere — added.

   Every role file (including the 5 above) now also states explicitly that the claude
   pane is hard-blocked at the hook level and that a non-claude provider pane is held
   to the rule by prose alone — the same disclosure the `browser_driver` /
   `host_destructive` / `pip_editable` rules already carry, for the same reason (#103:
   never leave a claude-only enforcement gap unstated).

4. **`docs/lead/role-and-workflow.md`** — added a paragraph telling Lead explicitly:
   never instruct a teammate to commit on the shared tree ("commit เอง",
   "ตรวจผ่านแล้ว commit เอง"); a task that needs a teammate to commit belongs on
   `--isolation worktree`, where the orchestrator's own hint already grants exactly
   that, scoped to the isolated branch.

## Multi-provider

`pane_guard.git_lead_only` is a real `PreToolUse` deny — Claude Code hooks only, so
hard enforcement is claude-pane-only, same caveat every other `pane_guard` rule
carries (module docstring). codex/gemini-agy/opencode/kimi/cursor panes are held to
this by role-file prose alone; the consistency fix in (3) above closes the one place
that prose was actually incomplete for those five providers specifically.

## Tests

- `tests/test_pane_guard.py::TestGitLeadOnlyDenied` — every banned subcommand, for
  every role, no allowlist; explicit false-positive coverage (`git log --grep=commit`,
  `git branch -d` lowercase, `git stash`, reading/mentioning `git commit` in text).
- `tests/test_pane_guard.py::TestGitLeadOnlyWorktreeCarveOut` — commit allowed only
  from a worktree cwd; push/rebase/merge/checkout still denied from that same cwd; no
  cwd (or a non-worktree cwd) defaults to denied.
- `tests/test_cli_guard.py` — end-to-end through the actual stdin-JSON hook path,
  including the `cwd` plumbing and malformed-`cwd` fail-open cases.
- `tests/test_agent_role_files_have_git_commit_guard.py` — every role file names the
  8 banned subcommands, the `takkub done` hand-off, "only Lead", and the claude-only
  hard-block disclosure.
- `tests/test_custom_roles.py::test_default_template_bans_self_commit` — a fresh
  custom role's default template carries the same prohibition.
