"""Google Gemini CLI wrapper - non-interactive one-shot mode.

Mirror of codex_helper.py for the gemini CLI. Lets the user fire
Gemini via the cockpit's `takkub gemini` command for quick second-
opinion / planning / brainstorm questions without spawning a full
pane. No PTY, no orchestrator IPC - just
`subprocess.run(["gemini", "-p", "<prompt>"])` with the prompt
text routed through and the result printed back.

Auth is whatever Gemini CLI itself uses (Google login on first run
or `GEMINI_API_KEY` env var). The cockpit never touches Gemini's
credentials. If Gemini isn't logged in, its own stderr surfaces the
error verbatim.

Design rules (mirror codex_helper.py):
- Best-effort. Any failure returns `(False, <reason>)`.
- subprocess.run with cwd specified, never shell=True, default
  timeout 120 s.
- No file writes by this module. Gemini writes its own session
  artefacts under `~/.gemini/` independently.
"""

from __future__ import annotations

import shutil
import subprocess


def find_gemini_executable() -> str | None:
    """Return the absolute path to the `gemini` binary, or None when
    it isn't on PATH. Caller surfaces a friendly "install with
    `npm install -g @google/gemini-cli`" message in the None case.

    On Windows npm installs `gemini` as a `.cmd` shim alongside the
    Node script; `shutil.which` handles the extension probing
    automatically (uses %PATHEXT%)."""
    return shutil.which("gemini")


def gemini_exec(
    prompt: str,
    *,
    cwd: str | None = None,
    timeout: float = 120.0,
    model: str | None = None,
) -> tuple[bool, str]:
    """Run `gemini -p "<prompt>"` and return `(ok, output)`.

    Gemini's non-interactive entry point is the `-p`/`--prompt` flag,
    NOT a subcommand like codex's `exec`. Do not reuse codex's argv
    shape - `gemini exec "..."` would fail with "unknown command".

    `cwd` lets the caller scope Gemini to a specific project. Defaults
    to the process cwd so `takkub gemini` from inside any pane targets
    that pane's project naturally.

    `model` is optional and gets forwarded as `-m <name>`; when None,
    Gemini uses whatever its config defaults to.

    `timeout` defaults to 120 s. Timeout returns
    (False, "gemini exec timed out").
    """
    binary = find_gemini_executable()
    if binary is None:
        return False, (
            "gemini binary not on PATH. Install with "
            "`npm install -g @google/gemini-cli`, then run `gemini` once."
        )
    if not (prompt or "").strip():
        return False, "empty prompt"
    argv: list[str] = [binary, "-p", prompt]
    if model:
        argv = [binary, "-m", model, "-p", prompt]
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, "gemini exec timed out"
    except FileNotFoundError:
        return False, "gemini binary disappeared from PATH"
    except Exception as e:
        return False, f"gemini exec failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "gemini exec failed").strip()
        return False, tail
    return True, (proc.stdout or "").strip()
