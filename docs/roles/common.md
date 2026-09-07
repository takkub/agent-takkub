# Common specialist rules (#516 token diet)

Shared by every non-Lead role file (`frontend`, `backend`, `mobile`,
`devops`, `qa`, `reviewer`, `critic`, `codex`, `gemini`, `opencode`,
`kimi`, `cursor` today — other specialist roles can point here too). Each
role's core `.claude/agents/<role>.md` carries only a one-line pointer into
whichever section below applies; this file is **not staged into any pane's
boot context** — read a section only when the situation it covers actually
comes up. Written in English (model-only instructions, no user-facing output
format here) per the #267 precedent — Thai stays only where the *user* reads
the output (e.g. the `takkub done` note the model writes).

## version-control

Never run `git commit` / `git push` / `git reset --hard` / `git push --force`
/ `git branch -D` / `git tag -d` / `git rebase` / `git merge` / `git
checkout` under any circumstances — only Lead handles version control.
Deciding work is "done enough to commit" is not a specialist's call.

Allowed (read-only): `git status`, `git diff`, `git log`, `git show`, `git
stash`.

If you think the work needs saving: run `takkub done "<note>"` — Lead sees
the report, reviews the diff, and decides when/whether to commit, bundle, or
push. Never pre-empt that, even if you think the user would probably want it
committed.

The claude pane is blocked at the hook level (`takkub _guard` →
`pane_guard.py`); panes on another provider (codex / gemini-agy / opencode /
kimi / cursor) are held to this rule by this prose alone — do not work
around it.

## process-safety (#169)

Never kill a process by image/process name — it can't tell your pane's
process from every other pane's, so it kills every match machine-wide:
- ❌ `taskkill /IM node.exe`, `taskkill /F /T /IM python.exe`
- ❌ `pkill <name>`, `killall <name>`
- ❌ PowerShell `Stop-Process -Name <name>`

Do instead: target only the PID your own pane spawned — `taskkill /PID
<pid>`, `Stop-Process -Id <pid>`, `kill <pid>`.

Real incident (2026-07-08): a frontend pane ran `taskkill /F /T /IM node.exe`
to clear a stuck port debugging `next dev` and killed every node process
machine-wide, including other teammate panes and other tasks' dev servers.

## no-editable-installs (#202)

Never run `pip install -e .` (or any `--editable` path): `pip install -e .`
/ `pip3 install --editable .` / `python -m pip install -e .`. It writes the
current worktree's path into `__editable__*.pth` in the shared venv used by
every other pane — deleting this worktree later breaks the venv machine-wide
(`ModuleNotFoundError`), and every process using that venv meanwhile
silently imports code from the wrong worktree (real incident #202: a qa
pane's full suite in an overlapping window got results from the wrong
worktree).

Do instead: just run `pytest` normally, no reinstall needed. Need a real
dependency change? Tell Lead via `takkub send --to lead` instead of
touching the shared venv yourself.

## no-host-network-changes (#400)

Never change the host machine's network — it belongs to the user, not the
pane's sandbox:
- Windows: `netsh wlan connect/disconnect/add profile/delete profile`,
  `netsh interface set/add/delete` (ip/ipv4/ipv6), `ipconfig /release`,
  `ipconfig /renew`, `route add/delete/change`, `rasdial`, `netsh winhttp
  set/reset proxy`
- macOS: `networksetup -setairportnetwork/-setairportpower/
  -setnetworkserviceenabled/-set*proxy*`, `ifconfig <if> up/down`, `route
  add/delete`, `scutil --proxy` (incl. every `sudo` variant)

Read-only and fine: `netsh wlan show`, bare `ipconfig`, `route print`,
`networksetup -getairportnetwork`, bare `ifconfig`.

Need to test over a different network path? Ask the user to use a second
device — never touch the host's network yourself. Real incident (#400): a
pane ran `netsh wlan connect` to test a networking change and the user lost
all connectivity instantly, with no warning.

## no-drive-wide-scans

Never scan the whole drive — `find / ...`, `find C:\ ...`,
`Get-ChildItem <root> -Recurse` burn disk I/O until the whole machine
stutters. Use the Glob/Grep tool, or scope the path narrowly (e.g. `find
src -name '*.ts'`).

## temp-files (#1, #104)

Temp files / images / test scripts go only in `$TAKKUB_ARTIFACTS_DIR`, never
in the project's repo — evidence for your own task specifically →
`$TAKKUB_ARTIFACTS_DIR/<your-role>/` (stops evidence scans from grabbing the
wrong pane's images, #109). Always read files with the Read tool — never a
shell one-liner (`cat`/`type`) on a long path.

## test-placement (#478)

Every task that touches logic needs a regression test in the **same diff**
— not "later", not skipped.

- Node/TS: spec next to the file it tests, named `<file>.spec.ts` or
  `<file>.test.ts`, **matching whatever pattern the project already uses**
  (check its existing test files first — if it uses `__tests__/`, follow
  that). Never invent a new test folder/pattern when the project already has
  a convention. A project with no tests at all → don't set up a test runner
  unilaterally; tell Lead "no test runner" and keep working.
- Python: `tests/test_<module>.py` matching the module touched.
- e2e/browser: only the e2e folder the project already has (`e2e/`,
  `tests/e2e/`, `playwright/`) — never a new one.
- smoke: the single `smoke` script in package.json (#475), no extra folder.
- Never leave scratch files in the repo: `debug_*`, `tmp_*`, a bare
  `test.js`/`test.py`, screenshots outside the designated folder, `*.log`,
  self-created `.env.*` — temp files belong in scratchpad/DATA_HOME only.
- Screenshot self-verify (#433): the one path the project designates (or
  `<DATA_HOME>/runtime/artifacts/<project>/` if none) — never in the repo.

## reporting-done

`takkub done` means the task is finished, full stop — it closes the pane
within 2.5s, killing any subprocess still running (e.g. an unfinished
build/test — #234). Not done yet, just want to update status? Use `takkub
progress "<msg>"` instead — it doesn't close the pane, call it as many times
as you want while working.

Must actually run `takkub done` through the Bash tool — never type it as
descriptive text on screen ("Done. takkub done appended"). That doesn't
notify Lead, and the idle watchdog keeps firing `[auto-reminder]` until the
command is actually executed.

```bash
takkub done "<note summarizing what changed>"
```

The orchestrator notifies Lead and closes your pane automatically — this is
the only way Lead learns the task is done. Never skip it.

Per #485/#433/root-CLAUDE.md test tiers: do not run `takkub qa-gate`
yourself — write the regression test in your diff, run at most the targeted
test/spec for the files you touched, and let qa's single end-of-batch gate +
CI prove the rest.

## communication

Peer messages: `takkub send --to <role> "<msg>"` — the orchestrator routes
it and auto-CCs Lead, so you never need to send twice.

Roles you can send to: `frontend` `backend` `mobile` `devops` `designer`
`qa` `reviewer` (plus any custom role Lead added).

Blocked, or the task spec is incomplete? Use `takkub send --to lead
"blocked: <what's wrong + what you need>"` — never print the question as
plain text on your own screen and wait. Lead cannot see your pane's screen,
only `takkub list`'s working/done status; a question that's only on-screen
just vanishes and both sides stall. `takkub send --to lead` gets the
orchestrator to inject your message into Lead's input, and the idle
watchdog suppresses the auto-reminder until Lead replies.

## browser-non-ui-roles

Non-UI roles (all provider slots — codex/gemini/opencode/kimi/cursor,
backend, devops, etc.) never install or run a browser driver themselves —
`playwright` / `puppeteer` / `selenium` / headless chrome through any
channel:
- ❌ `npx playwright ...`, `npm i playwright`, `pnpm add puppeteer`, `yarn
  add puppeteer-core`
- ❌ `pip install playwright`, `python -m playwright install`
- ❌ an ad-hoc script `require('playwright')` / `from playwright...`
- ❌ `chrome --headless`, `chromium --remote-debugging-port=...`

Browser verification is qa's job (critic/designer for visual review) — they
have an isolated Playwright MCP + browser profile the cockpit hands out per
shard. Installing your own outside that reloads Chromium (cache once
bloated to 2.88GB across 4 builds) and burns untracked RAM/disk.

Work that needs browser verification → note it on `takkub done` and let
Lead route it to qa.
