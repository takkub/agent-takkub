"""Pieces every provider helper (codex/gemini-agy/cursor/opencode) and the
Remote mirror used to copy-paste (2026-10-24 review D):

* `run_cli` — the non-interactive `subprocess.run` + exception mapping
  behind `codex_exec` / `gemini_exec` / `cursor_exec`. Each helper still owns
  its argv shape and how it reads success/failure output.
* `find_executable` — PATH names first, then known install locations.
* `strip_remote_prefix` / `live_user_payload` — the "[remote → lead] " marker
  a phone-typed Lead message carries in every provider's transcript.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW

REMOTE_PREFIX = "[remote → lead] "
MAX_EVENT_CHARS = 16000


@dataclass(frozen=True)
class CliRun:
    """Either a finished process (`proc`) or why it never finished (`error`)."""

    proc: subprocess.CompletedProcess | None = None
    error: str = ""


def run_cli(argv: list[str], *, cwd: str | None, timeout: float, label: str) -> CliRun:
    """Run *argv* captured (utf-8, no console window) and map the failures
    every provider helper reported the same way: `"<label> exec timed out"`,
    `"<label> binary disappeared from PATH"`, `"<label> exec failed: …"`."""
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
        return CliRun(error=f"{label} exec timed out")
    except FileNotFoundError:
        # On PATH at which() time but gone by the time we ran it.
        return CliRun(error=f"{label} binary disappeared from PATH")
    except Exception as e:
        return CliRun(error=f"{label} exec failed: {e}")
    return CliRun(proc=proc)


def find_executable(names: Iterable[str], fallbacks: Iterable[Path]) -> str | None:
    """First of *names* on PATH, else the first existing *fallbacks* file."""
    for name in names:
        on_path = shutil.which(name)
        if on_path:
            return on_path
    for candidate in fallbacks:
        if candidate.is_file():
            return str(candidate)
    return None


def strip_remote_prefix(text: str) -> str:
    return text[len(REMOTE_PREFIX) :] if text.startswith(REMOTE_PREFIX) else text


def live_user_payload(text: str | None) -> list[dict]:
    """One SSE user event for *text* (`remote` = typed on the phone)."""
    if not text:
        return []
    remote = text.startswith(REMOTE_PREFIX)
    clean = strip_remote_prefix(text).strip()
    return [{"text": clean[:MAX_EVENT_CHARS], "remote": remote}] if clean else []
