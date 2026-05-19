"""OpenAI Codex CLI wrapper — non-interactive one-shot mode.

This is Option D from the Codex integration plan: a thin wrapper
that lets the user fire Codex via the cockpit's `takkub` CLI for
quick second-opinion / refactor / review questions without
spawning a full pane. No PTY, no orchestrator IPC — just
`subprocess.run(["codex", "exec", "<prompt>"])` with the prompt
text routed through and the result printed back.

Auth is whatever Codex itself uses (ChatGPT login via `codex login`
or `OPENAI_API_KEY` env var). The cockpit never touches Codex's
credentials. If Codex isn't logged in, its own stderr surfaces the
error verbatim and the user runs `codex login` once.

Design rules (mirror update_helper.py):
- Best-effort. Any failure returns `(False, <reason>)`.
- subprocess.run with cwd specified, never shell=True, default
  timeout 120 s (longer than git ops because Codex can think).
- No file writes by this module. Codex writes its own session
  artefacts under `~/.codex/` independently.
"""

from __future__ import annotations

import shutil
import subprocess


def find_codex_executable() -> str | None:
    """Return the absolute path to the `codex` binary, or None when
    it isn't on PATH. Caller surfaces a friendly "install with
    `npm install -g @openai/codex`" message in the None case.

    On Windows npm installs `codex` as a `.cmd` shim alongside the
    Node script; `shutil.which` handles the extension probing
    automatically (uses %PATHEXT%)."""
    return shutil.which("codex")


def codex_exec(
    prompt: str,
    *,
    cwd: str | None = None,
    timeout: float = 120.0,
    model: str | None = None,
) -> tuple[bool, str]:
    """Run `codex exec "<prompt>"` and return `(ok, output)`.

    Output is Codex's combined stdout+stderr trimmed — for a
    successful exec the stdout carries the model's response, and
    for failures stderr carries the error. We pass both back so
    the caller can just print the result whichever path it took.

    `cwd` lets the caller scope Codex to a specific project (handy
    for "review this codebase" prompts). Defaults to the process
    cwd so `takkub codex` from inside any pane targets that pane's
    project naturally.

    `model` is optional and gets forwarded as `--model <name>`;
    when None, Codex uses whatever its config defaults to.

    `timeout` is generous (120 s) because Codex's reasoning runs
    can be long. Timeout returns (False, "codex exec timed out").
    """
    binary = find_codex_executable()
    if binary is None:
        return False, (
            "codex binary not on PATH. Install with "
            "`npm install -g @openai/codex`, then run `codex login` once."
        )
    if not (prompt or "").strip():
        return False, "empty prompt"
    argv: list[str] = [binary, "exec", prompt]
    if model:
        # Insert before the prompt so positional parsing in clap
        # treats `<prompt>` as the trailing positional, not as a
        # value for --model.
        argv = [binary, "exec", "--model", model, prompt]
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
        return False, "codex exec timed out"
    except FileNotFoundError:
        # PATH had the binary at which() time but it disappeared
        # between probes — rare but worth surfacing distinctly.
        return False, "codex binary disappeared from PATH"
    except Exception as e:
        return False, f"codex exec failed: {e}"
    if proc.returncode != 0:
        # Codex sometimes writes the response to stdout AND an
        # error to stderr (rate-limit, auth blob expired). Hand
        # back whichever has content so the caller has something
        # to show the user.
        tail = (proc.stderr or proc.stdout or "codex exec failed").strip()
        return False, tail
    return True, (proc.stdout or "").strip()
