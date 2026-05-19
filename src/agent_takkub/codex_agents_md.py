"""Codex pane AGENTS.md auto-plant.

Codex auto-discovers `AGENTS.md` from its cwd and walks up. The cockpit
plants a short cheatsheet there before spawning the codex pane so the
agent knows about `takkub send/done` — letting Codex behave like a
real teammate (peer coordination + report-back-to-Lead) instead of a
detached terminal.

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

# Codex Teammate · agent-takkub cockpit

You are running inside an **agent-takkub** pane spawned by a human
operator (or by a Claude Lead pane via `takkub assign --role codex
"<task>"`). Behave like a focused specialist:

## Hard rules

- **Do the task yourself.** Don't try to spawn sub-agents or delegate.
  You are the specialist; if you're stuck, ask Lead via `takkub send`.
- **One task per session.** When the work is done, call
  `takkub done "<one-line summary>"` so Lead is notified and the
  cockpit can free the pane.
- **No long-running foreground commands.** Background docker/dev
  servers with `&` + redirect, or use `-d`. Never `npm run dev` in
  the foreground — it never returns and the pane hangs.

## Communication with the rest of the team

| Command | When to use |
|---|---|
| `takkub send --to lead "<msg>"` | Ask Lead a clarifying question, request more context, or surface a blocker. Don't wait silently. |
| `takkub send --to <role> "<msg>"` | Coordinate directly with a peer pane (e.g. `frontend`, `backend`, `qa`). Lead is auto-CC'd. |
| `takkub done "<note>"` | Final step when your task is complete. Pane closes after this. |
| `takkub list` | See which other panes are open in the same project. |

The `takkub` binary is on `PATH` inside this pane — just run it as a
shell command.

## When the user said "brainstorm"

If the prompt is exploratory ("ideas for X", "how should we approach Y"):
respond with 3-5 concrete options + the main trade-off of each.
Don't write code until the user picks a direction. **Do not call
`takkub done` for brainstorm sessions** — the user will close the
pane manually when they've absorbed the answer.

## Working directory

The cockpit set your cwd to the project the operator is currently
focused on. Treat that as your workspace root. Read files, run tests,
commit when explicitly asked — don't push without permission.
"""


def ensure_agents_md(spawn_cwd: str | Path) -> tuple[bool, str]:
    """Plant `<spawn_cwd>/AGENTS.md` with the cockpit cheatsheet.

    Returns `(planted, reason)`:
      - `(True, "written")` — file was created or refreshed.
      - `(False, "user-owned")` — existing AGENTS.md without our
        marker; left untouched.
      - `(False, "<error>")` — disk failure (permission, etc.).

    The cheatsheet is idempotent: if the file already carries our
    marker, we overwrite it (refresh in case the content changed
    between cockpit versions). If a user-owned AGENTS.md exists we
    skip — Codex will use theirs, and our `takkub` shortcuts just
    won't be available.
    """
    target = Path(spawn_cwd) / "AGENTS.md"
    try:
        if target.exists():
            head = target.read_text(encoding="utf-8", errors="replace").splitlines()
            first = head[0] if head else ""
            if TAKKUB_MARKER not in first:
                return False, "user-owned"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(CODEX_AGENTS_MD, encoding="utf-8")
        return True, "written"
    except OSError as e:
        return False, f"write failed: {e}"
