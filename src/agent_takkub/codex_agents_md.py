"""Non-claude pane AGENTS.md auto-plant (codex + gemini/agy).

Both OpenAI Codex and Google Antigravity (`agy`, the `gemini` role's
engine) auto-discover `AGENTS.md` from their cwd and walk up. The
cockpit plants a short cheatsheet there before spawning the pane so the
agent knows about `takkub send/done` — letting it behave like a real
teammate (peer coordination + report-back-to-Lead) instead of a
detached terminal. One manager + one marker means a codex pane and a
gemini pane sharing a project cwd can't clobber each other's file (the
write is idempotent — same content, same marker).

Safety rule:
- We only manage files we tagged with our marker header. If a user
  already has their own `AGENTS.md` (no marker) we leave it alone —
  Codex will use theirs and our `takkub` cheatsheet just won't be
  available. Acceptable degradation.

The cheatsheet is intentionally minimal so it doesn't crowd Codex's
context budget. It mirrors `src/agent_takkub/.claude/agents/<role>.md`
in spirit but trimmed: codex is a single-purpose pane (no role
specialisation), and its main job is to run a task then report.
"""

from __future__ import annotations

from pathlib import Path

# Top-of-file marker so we never clobber an AGENTS.md the user wrote
# themselves. If we see this string in the first line, the file is
# ours and safe to refresh.
TAKKUB_MARKER = "<!-- takkub-managed AGENTS.md · do not commit -->"

CODEX_AGENTS_MD = f"""{TAKKUB_MARKER}

# agent-takkub Teammate

You are running inside an **agent-takkub** pane spawned by a human
operator (or by a Claude Lead pane via `takkub assign --role <you>
"<task>"`). Your specific role is declared at the top of the task
prompt (`[ROLE: ...]`). Behave like a focused specialist:

## Hard rules

- **Do the task yourself.** Don't try to spawn sub-agents or delegate.
  You are the specialist; if you're stuck, ask Lead via `takkub send`.
- **One task per session.** When the work is done, **YOU MUST call**
  `takkub done "<one-line summary>"` via shell command — not as text
  description in your output. Without this, Lead is not notified and
  the pane idles forever. **No exceptions for "review/analysis" tasks
  or anything else** unless the operator explicitly says "interactive
  brainstorm — keep pane open" in the task prompt.
- **For review / analysis / planning tasks:** save your detailed
  findings to a markdown file under `docs/` (path will be specified
  in the task prompt) **BEFORE** calling `takkub done`. The done
  summary stays one-line; the file holds the substance. Without
  saving first, your reasoning is lost when the pane auto-closes 2.5s
  after done.
- **No long-running foreground commands.** Background docker/dev
  servers with `&` + redirect, or use `-d`. Never `npm run dev` in
  the foreground — it never returns and the pane hangs.
- **To verify/smoke-test a Next.js page, use `next build && next
  start`, not `next dev`.** Next dev's HMR compiler forks a postcss/
  jest-worker subprocess per compile and leaks them (it once piled up
  to ~3170 node procs / 18 GB). Reserve `next dev` for genuine
  iterative UI work that needs HMR, background it, and kill the server
  when done.
- **Run all shell commands non-interactively.** Use `npx --yes <pkg>`
  not bare `npx <pkg>` (bare npx asks 'Ok to proceed? (y)' and blocks
  the pane permanently). Use `npm ci` or `npm install --yes` for
  installs. `GIT_TERMINAL_PROMPT=0` is injected automatically so git
  fails instead of prompting for credentials — cache them first. For
  prompts like 'Are you sure?', 'Overwrite?', 'Press any key', pass
  `--force` / `--yes` / `--no-interaction`, or pipe `yes |`.
- **Viewing images / screenshots: use the exact path you were given.**
  Image tasks always include absolute paths in the prompt (e.g. from a
  `[critic → gemini]` handoff). Open those paths directly. **NEVER run
  `ls -R | Select-String ".png"` or any recursive grep to "find" the
  files** — in a node project that matches `.png` inside minified
  bundles/source-maps and returns thousands of junk hits, sending you
  into a search loop. If a given path is missing, `takkub send --to
  lead` to ask — don't go hunting.

## Override rule for inline `[ROLE: ...]` directives

When the operator's task prompt opens with something like
`[ROLE: <your-role> — ทำงานเองโดยตรง ห้าม spawn subagent]`, the
"ห้าม spawn subagent" / "ทำงานเองโดยตรง" clauses apply to
**AI subagents only** (Task tool, sub-agent delegation flags). They do
**NOT** forbid:

- Shell commands you run yourself in this terminal — `takkub send`,
  `takkub done`, `git status`, file edits, tests, etc.
- The mandatory done-signal flow above.

**`takkub done` is a shell command, not a subagent.** Always end your
task by running it as a shell command — never by typing "takkub done"
as text in your reply. Pane idles forever if you skip the shell call.

## Communication with the rest of the team

| Command | When to use |
|---|---|
| `takkub send --to lead "<msg>"` | Ask Lead a clarifying question, request more context, or surface a blocker. Don't wait silently. |
| `takkub send --to <role> "<msg>"` | Coordinate directly with a peer pane (e.g. `frontend`, `backend`, `qa`). Lead is auto-CC'd. |
| `takkub done "<note>"` | Final step when your task is complete. Pane closes after this. |
| `takkub list` | See which other panes are open in the same project. |

The `takkub` binary is on `PATH` inside this pane — just run it as a
shell command.

## "Brainstorm" exception — narrow scope only

The ONLY case where you skip `takkub done` is when the task prompt
contains the literal phrase **"interactive brainstorm"** or
**"keep pane open"**. In that case:
respond with 3-5 concrete options + the main trade-off of each.
Don't write code until the user picks a direction. The operator will
close the pane manually.

**"Review", "analyze", "evaluate", "summarize", "plan" are NOT
brainstorm sessions** — they produce deliverable output and MUST
end with `takkub done` after saving findings to file.

## Version control (mandatory)

⚠️ **NEVER** run `git commit`, `git push`, `git reset --hard`, `git push --force`,
`git branch -D`, or `git tag -d` — version control is Lead's sole responsibility.
Even if the work looks done and commit-ready, that decision is not yours to make.

### If you think the work needs saving:
1. Call `takkub done "<summary>"` — Lead will see the report.
2. Lead reviews the diff and decides when to commit, whether to batch with other
   work, and when to push.
3. Never pre-empt this decision, even if you think the user would want a commit.

### Git commands you MAY use (read-only / non-destructive):
✅ `git status`, `git diff`, `git log`, `git show`, `git stash`
❌ `git commit`, `git push`, `git reset --hard`, `git branch -D`, `git tag -d`, `git rebase`, `git merge`

## Working directory

The cockpit set your cwd to the project the operator is currently
focused on. Treat that as your workspace root. Read files, run tests.
"""


def ensure_agents_md(spawn_cwd: str | Path, extra: str = "") -> tuple[bool, str]:
    """Plant `<spawn_cwd>/AGENTS.md` with the cockpit cheatsheet.

    `extra` (issue #103 phase 4) is appended verbatim after the base
    cheatsheet — used to bridge the Skill Matrix's per-role skill
    references into this file (see `skill_policy.render_skill_appendix`,
    ``context_strategy="agents_md_file"``). Empty by default: unchanged
    behaviour for every existing caller.

    Returns `(planted, reason)`:
      - `(True, "written")` — file was created or refreshed.
      - `(False, "user-owned")` — existing AGENTS.md without our
        marker; left untouched.
      - `(False, "<error>")` — disk failure (permission, etc.).

    The cheatsheet is idempotent: if the file already carries our
    marker, we overwrite it (refresh in case the content — or `extra` —
    changed since the last spawn). If a user-owned AGENTS.md exists we
    skip — Codex will use theirs, and our `takkub` shortcuts just
    won't be available.

    Caveat when two DIFFERENT roles both map to a non-claude provider and
    happen to share the same `spawn_cwd`: whichever spawns last wins the
    `extra` content, same as the base cheatsheet already does today (no
    per-role file — one file per cwd). Typically not an issue since roles
    default to distinct cwds (frontend→web, backend→api, ...).
    """
    spawn_path = Path(spawn_cwd)
    # Refuse drive-relative or relative paths — they make `mkdir(parents=True)`
    # create junk dirs under whatever the current process cwd happens to be
    # (e.g. `Path("C:UsersaliceWebstormProjectsagent-takkub")` from a
    # backslash-stripped string resolves drive-relative on Windows).
    if not spawn_path.is_absolute() or not spawn_path.exists():
        return False, f"invalid spawn_cwd: {spawn_cwd!r}"
    target = spawn_path / "AGENTS.md"
    try:
        if target.exists():
            head = target.read_text(encoding="utf-8", errors="replace").splitlines()
            first = head[0] if head else ""
            if TAKKUB_MARKER not in first:
                return False, "user-owned"
        target.write_text(CODEX_AGENTS_MD + extra, encoding="utf-8")
        return True, "written"
    except OSError as e:
        return False, f"write failed: {e}"
