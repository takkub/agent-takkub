"""Vault mirror — Obsidian write-side helpers for `takkub done` events.

Handles three concerns:
1. Vault discovery (`_resolve_vault_dir`) — locate the Obsidian vault via
   `$TAKKUB_VAULT_DIR` (or the built-in default path) and verify it has the
   `01-Projects/` layout before writing into it.
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

import hashlib
import json
import logging
import os
import pathlib
import re
import time
from datetime import datetime

from .config import REPO_ROOT

_distill_log = logging.getLogger(__name__)

# Where to look for the Obsidian vault that mirrors cockpit decision
# logs. Resolution order:
#   1. $TAKKUB_VAULT_DIR  — explicit override, wins over everything
#   2. the built-in `_DEFAULT_VAULT` fallback path (override via the env var)
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


# ---------------------------------------------------------------------------
# Near-duplicate dedup filter (in-process; resets on cockpit restart)
# ---------------------------------------------------------------------------

# (project, role, md5-of-first-line) tuples seen this process run.
_DEDUP_HASHES: set[tuple[str, str, str]] = set()


def _is_dedup_note(project: str, role: str, note: str) -> bool:
    """Return True (and swallow) when an identical first-line was already written
    this session for the same project+role — prevents rapid-fire duplicate done
    events from creating twin log files."""
    scrubbed = _scrub_note(note)
    first_line = scrubbed.splitlines()[0] if scrubbed else ""
    h = hashlib.md5(first_line.encode()).hexdigest()
    key = (project, role, h)
    if key in _DEDUP_HASHES:
        return True
    _DEDUP_HASHES.add(key)
    return False


# ---------------------------------------------------------------------------
# Retention / prune
# ---------------------------------------------------------------------------

# session logs older than this (seconds) are eligible for deletion
_SESSION_MAX_AGE_S = 14 * 86400
# keep at most this many session logs per project regardless of age
_SESSION_KEEP_LAST = 5
# brief files older than this (seconds) are deleted
_BRIEF_MAX_AGE_S = 30 * 86400


def prune_vault_logs(vault: pathlib.Path) -> tuple[int, int]:
    """Delete stale log files from ``99-Logs/`` inside *vault*.

    *session logs* (``99-Logs/sessions/<project>/``): keep last
    ``_SESSION_KEEP_LAST`` per project; delete any file older than
    ``_SESSION_MAX_AGE_S`` days.

    *briefs* (``99-Logs/briefs/``): delete files older than
    ``_BRIEF_MAX_AGE_S`` days.

    Returns ``(sessions_deleted, briefs_deleted)``. All IO errors are
    swallowed so a filesystem hiccup never bubbles up to the caller.
    """
    now = time.time()
    sessions_deleted = 0
    briefs_deleted = 0

    sessions_root = vault / "99-Logs" / "sessions"
    if sessions_root.is_dir():
        for proj_dir in sessions_root.iterdir():
            if not proj_dir.is_dir():
                continue
            try:
                files = sorted(
                    proj_dir.glob("*.md"),
                    key=lambda p: p.stat().st_mtime,
                )
            except OSError:
                continue
            kept: list[pathlib.Path] = []
            age_cutoff = now - _SESSION_MAX_AGE_S
            for f in files:
                try:
                    if f.stat().st_mtime < age_cutoff:
                        f.unlink()
                        sessions_deleted += 1
                    else:
                        kept.append(f)
                except OSError:
                    kept.append(f)
            # enforce keep-last cap on remaining files (oldest first)
            if len(kept) > _SESSION_KEEP_LAST:
                for f in kept[: len(kept) - _SESSION_KEEP_LAST]:
                    try:
                        f.unlink()
                        sessions_deleted += 1
                    except OSError:
                        pass

    briefs_dir = vault / "99-Logs" / "briefs"
    if briefs_dir.is_dir():
        age_cutoff = now - _BRIEF_MAX_AGE_S
        for f in briefs_dir.glob("*.md"):
            try:
                if f.stat().st_mtime < age_cutoff:
                    f.unlink()
                    briefs_deleted += 1
            except OSError:
                pass

    return sessions_deleted, briefs_deleted


# ---------------------------------------------------------------------------
# Obsidian graph filter
# ---------------------------------------------------------------------------

# Filter expression added to graph.json so 99-Logs/ is excluded from the
# Obsidian graph view, leaving only knowledge-tier notes visible.
_GRAPH_FILTER = "-path:99-Logs"


def write_obsidian_graph_filter(vault: pathlib.Path) -> bool:
    """Write/update ``<vault>/.obsidian/graph.json`` to exclude ``99-Logs/``
    from Obsidian's graph view.

    Merges the ``search`` key with any existing config so user customisations
    (colours, physics settings) are preserved. Returns True on success.
    """
    obsidian_dir = vault / ".obsidian"
    graph_path = obsidian_dir / "graph.json"

    config: dict = {}
    if graph_path.is_file():
        try:
            config = json.loads(graph_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            config = {}

    existing_search = config.get("search", "")
    if _GRAPH_FILTER not in existing_search:
        merged = f"{existing_search} {_GRAPH_FILTER}".strip() if existing_search else _GRAPH_FILTER
        config["search"] = merged

    try:
        obsidian_dir.mkdir(parents=True, exist_ok=True)
        graph_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Phase B — Distill layer
# ---------------------------------------------------------------------------

# Signals indicating a done-note carries durable knowledge (decision rationale,
# bug root-cause, reusable pattern). At least one must appear (case-insensitive)
# for the entry to be distilled into the curated project page.
_DURABLE_SIGNALS: tuple[str, ...] = (
    "fix",
    "bug",
    "root cause",
    "ปัญหา",
    "สาเหตุ",
    "crash",
    "workaround",
    "gotcha",
    "decision",
    "เลือก",
    "เหตุผล",
    "because",
    "เพราะ",
    "เนื่องจาก",
    "approach",
    "tradeoff",
    "pattern",
    "refactor",
    "extract",
    "migrate",
    "migration",
    "architecture",
    "ย้าย",
    "แยก",
    "split",
    "restructure",
    "redesign",
    "issue",
)

# Signals that route a note to the bug-patterns MOC.
_BUG_MOC_SIGNALS: frozenset[str] = frozenset(
    {
        "fix",
        "bug",
        "root cause",
        "crash",
        "ปัญหา",
        "สาเหตุ",
        "workaround",
        "gotcha",
    }
)

# Signals that route a note to the architecture-decisions MOC.
_ARCH_MOC_SIGNALS: frozenset[str] = frozenset(
    {
        "decision",
        "pattern",
        "refactor",
        "extract",
        "migrate",
        "migration",
        "architecture",
        "approach",
        "เลือก",
        "เหตุผล",
        "ย้าย",
        "แยก",
        "split",
        "restructure",
    }
)

# (vault_rel_path, display_name, initial_markdown) per MOC category key.
_MOC_TEMPLATES: dict[str, tuple[str, str, str]] = {
    "bug": (
        "02-Areas/bug-patterns.md",
        "bug-patterns",
        "# Bug Patterns\n\nCross-project bug patterns, root causes, and workarounds "
        "distilled from cockpit session logs.\n",
    ),
    "arch": (
        "02-Areas/architecture-decisions.md",
        "architecture-decisions",
        "# Architecture Decisions\n\nCross-project architecture decisions and design "
        "patterns distilled from cockpit session logs.\n",
    ),
}

# Header of the curated section inside 01-Projects/<project>.md.
_CURATED_SECTION = "## Decisions & Learnings"

# Intro blurb written once when the section is first created.
_CURATED_INTRO = (
    "Cross-session decisions, bug post-mortems, and reusable patterns distilled by "
    "cockpit. See also [[02-Areas/bug-patterns|bug-patterns]] · "
    "[[02-Areas/architecture-decisions|architecture-decisions]]."
)


def _is_durable_fact(note: str) -> bool:
    """Return True when the note likely carries durable knowledge worth distilling."""
    n = note.lower()
    return any(sig in n for sig in _DURABLE_SIGNALS)


def _moc_for_note(note: str) -> str | None:
    """Return MOC category key ('bug' or 'arch') for *note*, or None."""
    n = note.lower()
    if any(s in n for s in _BUG_MOC_SIGNALS):
        return "bug"
    if any(s in n for s in _ARCH_MOC_SIGNALS):
        return "arch"
    return None


def _scaffold_moc(vault: pathlib.Path, rel_path: str, content: str) -> None:
    """Create a MOC stub at ``vault/rel_path`` if it does not yet exist."""
    moc = vault / rel_path
    if moc.is_file():
        return
    try:
        moc.parent.mkdir(parents=True, exist_ok=True)
        moc.write_text(content, encoding="utf-8")
    except OSError:
        pass


def _ensure_project_page(vault: pathlib.Path, project: str) -> pathlib.Path:
    """Return path to ``01-Projects/<project>.md``, creating a minimal stub if absent."""
    page = vault / "01-Projects" / f"{project}.md"
    if not page.is_file():
        try:
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(
                f"# {project}\n\n{_CURATED_SECTION}\n\n{_CURATED_INTRO}\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    return page


def _append_decision_entry(page: pathlib.Path, entry: str) -> None:
    """Append *entry* under ``## Decisions & Learnings`` in *page*.

    Creates the section at EOF if the heading is absent. Skips the write
    when *entry* already appears verbatim (idempotent). Raises OSError on
    filesystem failure — callers must handle.
    """
    text = page.read_text(encoding="utf-8") if page.is_file() else ""
    if entry in text:
        return  # idempotent — already written
    if _CURATED_SECTION in text:
        sec_start = text.index(_CURATED_SECTION)
        sec_end = text.find("\n## ", sec_start + len(_CURATED_SECTION))
        if sec_end == -1:
            new_text = text.rstrip("\n") + "\n" + entry + "\n"
        else:
            new_text = text[:sec_end].rstrip("\n") + "\n" + entry + "\n" + text[sec_end:]
    else:
        new_text = text.rstrip("\n") + f"\n\n{_CURATED_SECTION}\n\n{_CURATED_INTRO}\n\n{entry}\n"
    page.write_text(new_text, encoding="utf-8")


def distill_session_facts(
    project: str,
    role: str,
    note: str,
    vault: pathlib.Path,
    *,
    now: datetime | None = None,
) -> bool:
    """Distill a durable takkub-done note into ``01-Projects/<project>.md``.

    Filters out noise (commands, status) and only appends notes that carry
    decision rationale, bug root-cause, or reusable patterns. Also scaffolds
    MOC stubs in ``02-Areas/`` with wiki-links from the entry.

    Best-effort: returns True on success, False when the note is not durable
    or when an error occurs. Never raises — errors are logged via the
    ``agent_takkub.vault_mirror`` logger.
    """
    try:
        if not _is_durable_fact(note):
            return False
        if now is None:
            now = datetime.now()
        iso = now.isoformat(timespec="seconds")
        scrubbed = _scrub_note(note)
        if len(scrubbed) > 300:
            scrubbed = scrubbed[:297] + "..."

        cat = _moc_for_note(note)
        moc_link = ""
        if cat is not None:
            moc_rel, moc_name, moc_content = _MOC_TEMPLATES[cat]
            _scaffold_moc(vault, moc_rel, moc_content)
            moc_link = f" → [[{moc_rel[:-3]}|{moc_name}]]"

        entry = f"- `{iso}` **{role}** — {scrubbed}{moc_link}"
        page = _ensure_project_page(vault, project)
        _append_decision_entry(page, entry)
        return True
    except Exception as exc:
        _distill_log.warning("distill_error project=%s role=%s: %r", project, role, exc)
        return False
