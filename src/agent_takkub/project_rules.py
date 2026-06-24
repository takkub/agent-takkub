"""Helpers for per-project CLAUDE.md rules — generation and I/O.

Design:
  generate_project_rules_proc() returns a subprocess.Popen object so callers
  (e.g. a QThread) can kill it on cancel.  The companion collect_result()
  helper resolves the Popen into a markdown string or raises RuntimeError.

  generate_project_rules() is a convenience wrapper (blocking, no cancel).

  read_project_rules() / write_project_rules() are thin wrappers around
  <project_root>/CLAUDE.md.  write_project_rules() writes atomically (temp
  file + replace) so a crash mid-write never leaves a partial file.

  All subprocess calls inherit the current environment so Max OAuth
  credentials flow through automatically; ANTHROPIC_API_KEY is never set.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ._win_console import SUBPROCESS_NO_WINDOW
from .config import find_claude_executable

_SYSTEM_INSTRUCTION = (
    "You are a technical writer helping a software development team create project rules. "
    "Output ONLY a markdown document suitable for use as a CLAUDE.md project rules file "
    "for an AI dev team. Cover: project overview and goals, tech stack and versions, "
    "deployment pipeline and environments, coding conventions and standards, constraints "
    "and things to avoid, key architectural decisions. "
    "Rules: write in the same language the user used; output ONLY raw markdown — "
    "no preamble, no code fences wrapping the whole document; "
    "be concise but complete; use headers, bullet points, and code snippets where needed."
)

_TIMEOUT = 600  # seconds — long enough for a thorough multi-section CLAUDE.md generation


def generate_project_rules_proc(prompt: str, project_name: str) -> subprocess.Popen:
    """Start claude headless and return the Popen object (so callers can kill on cancel).

    Caller must call collect_result(proc) to get the markdown string.
    """
    claude = find_claude_executable()
    if claude is None:
        raise RuntimeError("claude binary not found — install Claude CLI and ensure it is on PATH")
    full_prompt = (
        f"Project name: {project_name}\n\n"
        f"User description:\n{prompt}\n\n"
        "Generate the project rules CLAUDE.md now. "
        "Return only markdown, no fences/preamble."
    )
    return subprocess.Popen(
        [claude, "-p", full_prompt, "--append-system-prompt", _SYSTEM_INSTRUCTION],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=SUBPROCESS_NO_WINDOW,
    )


def collect_result(proc: subprocess.Popen, project_name: str) -> str:
    """Wait for a Popen started by generate_project_rules_proc() and return the markdown.

    Raises RuntimeError on timeout, non-zero exit, or empty output.
    """
    try:
        stdout, stderr = proc.communicate(timeout=_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise RuntimeError(
            f"claude timed out after {_TIMEOUT}s generating project rules for '{project_name}'"
        ) from exc
    if proc.returncode != 0:
        excerpt = (stderr or "").strip()[:400]
        raise RuntimeError(
            f"claude exited {proc.returncode} generating project rules for '{project_name}'"
            + (f": {excerpt}" if excerpt else "")
        )
    content = (stdout or "").strip()
    if not content:
        raise RuntimeError(
            f"claude returned empty output generating project rules for '{project_name}'"
        )
    return content


def generate_project_rules(prompt: str, project_name: str) -> str:
    """Blocking convenience wrapper.  Prefer generate_project_rules_proc() when
    you need cancel support (e.g. in a QThread)."""
    proc = generate_project_rules_proc(prompt, project_name)
    return collect_result(proc, project_name)


def read_project_rules(project_root: Path) -> str | None:
    """Return the content of <project_root>/CLAUDE.md, or None if absent."""
    target = project_root / "CLAUDE.md"
    if not target.exists():
        return None
    return target.read_text(encoding="utf-8")


def write_project_rules(project_root: Path, content: str) -> Path:
    """Atomically write content to <project_root>/CLAUDE.md and return the path."""
    target = project_root / "CLAUDE.md"
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)
    return target
