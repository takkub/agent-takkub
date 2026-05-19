"""Gemini pane GEMINI.md auto-plant.

Gemini CLI auto-discovers `GEMINI.md` from its cwd. The cockpit plants
a short cheatsheet there before spawning the gemini pane so the agent
knows about `takkub send/done` — letting Gemini behave like a real
teammate (peer coordination + report-back-to-Lead) instead of a
detached terminal.

Mirror of codex_agents_md.py. Lives as a separate module (rather than
generalising codex_agents_md) because:
- Marker text is distinct (so the two files coexist without overwriting)
- Filename is different (AGENTS.md vs GEMINI.md)
- Cheatsheet body addresses the agent as "Gemini Teammate"

Safety rule:
- We only manage files we tagged with our marker header. If a user
  already has their own `GEMINI.md` (no marker) we leave it alone —
  Gemini will use theirs and our `takkub` cheatsheet just won't be
  available. Acceptable degradation.
"""

from __future__ import annotations

from pathlib import Path

TAKKUB_GEMINI_MARKER = "<!-- takkub-managed GEMINI.md · do not commit -->"

GEMINI_MD = f"""{TAKKUB_GEMINI_MARKER}

# Gemini Teammate · agent-takkub cockpit

You are running inside an **agent-takkub** pane spawned by a human
operator (or by a Claude Lead pane via `takkub assign --role gemini
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


def ensure_gemini_md(spawn_cwd: str | Path) -> tuple[bool, str]:
    """Plant `<spawn_cwd>/GEMINI.md` with the cockpit cheatsheet.

    Returns `(planted, reason)`:
      - `(True, "written")` — file was created or refreshed.
      - `(False, "user-owned")` — existing GEMINI.md without our
        marker; left untouched.
      - `(False, "<error>")` — disk failure (permission, etc.).

    Idempotent: if the file already carries our marker, we overwrite
    it (refresh). If a user-owned GEMINI.md exists we skip.
    """
    target = Path(spawn_cwd) / "GEMINI.md"
    try:
        if target.exists():
            head = target.read_text(encoding="utf-8", errors="replace").splitlines()
            first = head[0] if head else ""
            if TAKKUB_GEMINI_MARKER not in first:
                return False, "user-owned"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(GEMINI_MD, encoding="utf-8")
        return True, "written"
    except OSError as e:
        return False, f"write failed: {e}"
