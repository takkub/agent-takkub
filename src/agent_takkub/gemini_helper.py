"""Antigravity CLI (`agy`) wrapper - non-interactive one-shot mode.

Backs the cockpit's `gemini` role. Google retired the standalone
Gemini CLI on 2026-06-18 and replaced it with the **Antigravity CLI**,
whose binary is `agy`. The role keeps its `gemini` identity (the
"third brain" Google AI planning / second-opinion slot) but is now
powered by `agy` end to end.

Mirror of codex_helper.py. Lets the user fire Antigravity via the
cockpit's `takkub gemini` command for quick second-opinion / planning
/ brainstorm questions without spawning a full pane. No PTY, no
orchestrator IPC - just `subprocess.run(["agy", "-p", "<prompt>"])`
with the prompt text routed through and the result printed back.

Auth is whatever the Antigravity CLI itself uses (Google Sign-In on
first run or `ANTIGRAVITY_API_KEY` env var). The cockpit never touches
those credentials. If `agy` isn't logged in, its own stderr surfaces
the error verbatim.

Design rules (mirror codex_helper.py):
- Best-effort. Any failure returns `(False, <reason>)`.
- subprocess.run with cwd specified, never shell=True, default
  timeout 120 s.
- No file writes by this module. `agy` writes its own session
  artefacts under `~/.antigravity/` (or `%LOCALAPPDATA%\\agy\\`)
  independently.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW

# Install hint surfaced whenever the binary is missing. Antigravity ships
# as a native binary (NOT npm) — the Windows installer drops `agy` under
# %LOCALAPPDATA%\agy\bin and adds it to PATH.
_INSTALL_HINT = (
    "agy binary not on PATH. Install the Antigravity CLI from "
    "https://antigravity.google/download (Windows installer drops `agy` "
    "under %LOCALAPPDATA%\\agy\\bin), then run `agy` once to sign in."
)


def _default_agy_paths() -> list[Path]:
    """Known fixed install locations for `agy.exe` (PATH-independent).

    The Antigravity Windows installer drops the binary under
    %LOCALAPPDATA%\\agy\\bin\\agy.exe but does NOT reliably add that dir
    to the user PATH (observed 2026-06-19 — the installer registered a
    stale `...\\Programs\\Antigravity\\bin` that doesn't exist, leaving
    the real `agy.exe` off PATH). Probing the canonical location keeps
    the cockpit from falsely degrading the `gemini` role to a Claude
    substitute when `agy` is in fact installed and working.
    """
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "agy" / "bin" / "agy.exe")
    home = Path.home()
    candidates.append(home / "AppData" / "Local" / "agy" / "bin" / "agy.exe")
    return candidates


def find_agy_executable() -> str | None:
    """Return the absolute path to the `agy` binary, or None when it
    can't be located. Caller surfaces the friendly install message in
    the None case.

    Resolution order:
      1. `shutil.which("agy")` — the binary on PATH (uses %PATHEXT% so a
         bare `agy` matches `agy.exe`/`agy.cmd`). Works when the
         Antigravity installer registered PATH correctly.
      2. Fixed install location %LOCALAPPDATA%\\agy\\bin\\agy.exe — a
         fallback for the common case where the installer dropped the
         binary but didn't put its dir on PATH.
    """
    on_path = shutil.which("agy")
    if on_path:
        return on_path
    for candidate in _default_agy_paths():
        if candidate.is_file():
            return str(candidate)
    return None


def gemini_exec(
    prompt: str,
    *,
    cwd: str | None = None,
    timeout: float = 120.0,
    model: str | None = None,
) -> tuple[bool, str]:
    """Run `agy -p "<prompt>"` and return `(ok, output)`.

    Antigravity's non-interactive entry point is the `-p`/`--print`
    flag (same shape the old Gemini CLI used), NOT a subcommand like
    codex's `exec`. Do not reuse codex's argv shape - `agy exec "..."`
    would fail with "unknown command".

    `cwd` lets the caller scope `agy` to a specific project. Defaults
    to the process cwd so `takkub gemini` from inside any pane targets
    that pane's project naturally.

    `model` is optional and gets forwarded as `-m <name>` (e.g.
    `gemini-3.1-pro`); when None, `agy` uses whatever its config
    defaults to.

    `timeout` defaults to 120 s. Timeout returns
    (False, "agy exec timed out").

    The public name stays `gemini_exec` because it backs the cockpit's
    `gemini` role + `takkub gemini` subcommand; only the engine behind
    it changed (Gemini CLI → Antigravity `agy`).
    """
    binary = find_agy_executable()
    if binary is None:
        return False, _INSTALL_HINT
    if not (prompt or "").strip():
        return False, "empty prompt"
    # Bound agy's own print-mode wait (default 5m) to just under our subprocess
    # timeout so agy gives up first and we never sit on a dead call. agy's
    # `--print` mode is unreliable without a real TTY (returns empty / blocks),
    # so this is belt-and-suspenders against a hang.
    agy_print_timeout = f"{max(10, int(timeout) - 5)}s"
    argv: list[str] = [binary, "-p", prompt, "--print-timeout", agy_print_timeout]
    if model:
        argv = [binary, "-m", model, "-p", prompt, "--print-timeout", agy_print_timeout]
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
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, "agy exec timed out"
    except FileNotFoundError:
        return False, "agy binary disappeared from PATH"
    except Exception as e:
        return False, f"agy exec failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "agy exec failed").strip()
        return False, tail
    out = (proc.stdout or "").strip()
    if not out:
        # agy exited 0 but emitted nothing — its `--print` mode needs a real
        # terminal and silently no-ops when captured non-interactively. Turn the
        # misleading "empty success" into actionable guidance instead of handing
        # the caller a blank answer.
        return False, (
            "agy print mode returned no output. Antigravity's `agy -p` needs a "
            "real terminal (TTY) and produces nothing when run non-interactively "
            "(the cockpit captures output via a pipe). Use the interactive gemini "
            'pane instead: `takkub assign --role gemini "<task>"`.'
        )
    return True, out
