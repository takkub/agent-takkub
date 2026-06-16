"""Vault mirror — Obsidian write-side helpers for `takkub done` events.

Handles three concerns:
1. Vault discovery (`_resolve_vault_dir`) — find the `~/WebstormProjects/
   second-brain` vault (or `$TAKKUB_VAULT_DIR` override) and verify it
   has the `01-Projects/` layout before writing into it.
2. Junk filters (`_is_junk_note`, `_is_junk_project`) — drop "ok"/"wip"
   style stubs and test/scratch project namespaces so Obsidian's graph
   stays signal-dense.
3. Decision-note rendering (`_render_decision_note`) — produce the
   single markdown body shared by both the local session log and the
   vault mirror so the two copies don't drift.

Extracted from orchestrator.py to keep that file focused on pane
lifecycle. orchestrator.py re-exports the names for backwards-compat
with existing test imports and external scripts.
"""

from __future__ import annotations

import os
import pathlib
import re
from datetime import datetime

from .config import REPO_ROOT

# Where to look for the Obsidian vault that mirrors cockpit decision
# logs. Resolution order:
#   1. $TAKKUB_VAULT_DIR  — explicit override, wins over everything
#   2. ~/WebstormProjects/second-brain — author's default vault layout
# We require an existing `01-Projects/` folder inside the candidate before
# treating it as a vault: a stray empty dir at the default path mustn't
# silently absorb session logs. Returns None when nothing matches, which
# tells callers to skip the mirror without raising.
_VAULT_ENV = "TAKKUB_VAULT_DIR"
_DEFAULT_VAULT = pathlib.Path.home() / "WebstormProjects" / "second-brain"


def _resolve_vault_dir() -> pathlib.Path | None:
    """Return the configured Obsidian vault root, or None if missing."""
    candidates: list[pathlib.Path] = []
    override = os.environ.get(_VAULT_ENV, "").strip()
    if override:
        candidates.append(pathlib.Path(override))
    candidates.append(_DEFAULT_VAULT)
    for cand in candidates:
        if (cand / "01-Projects").is_dir():
            return cand
    return None


# Sessions whose `note` matches one of these (case-insensitive, after
# stripping) are treated as no-information events and never reach the
# vault. They still flow through `agentDone` / `_recent_done_events` so
# Lead's inbox + hot.md still surface them — we just don't pollute
# Obsidian with stubs that have no analytical value.
_JUNK_NOTE_EXACT = frozenset(
    {
        "",
        ".",
        "ok",
        "ok.",
        "ok done",
        "done",
        "done.",
        "wip",
        "wip.",
        "appended",
        "yes",
        "no",
        "fixed",
        "all green",
    }
)

# Notes shorter than this (after stripping) are treated as junk even if
# they don't match the exact-junk list. 15 chars is enough for a
# substantive 2-3 word summary like "added /login" but trims one-word
# acknowledgements.
_JUNK_NOTE_MIN_LEN = 15

# Project names matching one of these prefixes are also skipped from
# the vault mirror — typically scratch/test/throwaway workspaces that
# nobody wants in the Obsidian graph.
_JUNK_PROJECT_PREFIXES = ("test", "tmp", "scratch", "playground")


def _is_junk_note(note: str) -> bool:
    """Return True when the takkub-done note is too thin to keep."""
    s = (note or "").strip().lower()
    if s in _JUNK_NOTE_EXACT:
        return True
    return len(s) < _JUNK_NOTE_MIN_LEN


def _is_junk_project(project: str) -> bool:
    """Return True when the project name looks like a scratch workspace."""
    p = (project or "").strip().lower()
    if not p:
        return True
    return any(p.startswith(prefix) for prefix in _JUNK_PROJECT_PREFIXES)


# Strip C0 control bytes (incl. ESC) except TAB/LF, DEL, and 8-bit C1, so an
# agent-authored note can't replay terminal escapes when the vault file is
# `cat`-ed, nor smuggle a CSI/OSC introducer past markdown (sec-w1).
_NOTE_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_NOTE_MAX = 8000


def _scrub_note(note: str) -> str:
    """Make an agent-authored note safe to write into a vault markdown file:
    strip control/escape bytes, defuse a leading frontmatter delimiter, and cap
    length. A normal note (no control bytes, not starting with ``---``, under
    the cap) is returned identical to ``note.strip()``."""
    text = _NOTE_CONTROL.sub("", note).strip()
    # A leading `---`/`...` line reads as a YAML frontmatter boundary to some
    # Obsidian/Dataview parsers; a zero-width space breaks the delimiter while
    # staying invisible in rendered output.
    if text.startswith("---") or text.startswith("..."):
        text = chr(0x200B) + text
    if len(text) > _NOTE_MAX:
        text = text[:_NOTE_MAX].rstrip() + "\n\n…(note truncated)"
    return text


def _render_decision_note(
    project: str,
    role: str,
    note: str,
    now: datetime,
    transcript_path: str | None = None,
) -> str:
    """Render the markdown body shared by the local session log and the
    vault mirror. Single source of truth so the two copies don't drift.

    Body layout (Obsidian-friendly):
      - YAML frontmatter: role / project / date / tags → enables
        Dataview queries and `tag:#session` filters.
      - `[[01-Projects/<project>|<project>]]` backlink in body so the
        graph view clusters each session under its project page.
      - Plain markdown `## Note` block so events.log/hot.md scrapers
        keep working with the existing pattern.
      - Optional `## Transcript` section with a relative path to the raw
        PTY byte-stream file (ANSI included) so the full pane output is
        one `less -R` away.
    """
    iso = now.isoformat(timespec="seconds")
    body = (
        f"---\n"
        f"role: {role}\n"
        f"project: {project}\n"
        f"date: {iso}\n"
        f"tags: [session, {role}, {project}]\n"
        f"---\n\n"
        f"# {role} done · {iso}\n\n"
        f"**Project:** [[01-Projects/{project}|{project}]]\n"
        f"**Role:** {role}\n\n"
        f"## Note\n\n{_scrub_note(note)}\n"
    )
    if transcript_path:
        try:
            rel = pathlib.Path(transcript_path).relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = transcript_path
        body += (
            f"\n## Transcript\n\n"
            f"Raw byte stream (with ANSI): `{rel}`\n\n"
            f"ดูดิบ: `cat {rel}`  \n"
            f"ดูแบบมีสี: `less -R {rel}`\n"
        )
    return body
