"""Per-(role × project) learned memory.

Each teammate role accumulates its own project-specific knowledge across runs in
``runtime/role-memory/<project>/<role>.md``: conventions, gotchas, key decisions,
plus role-specific notes (qa: test login / accounts / flows). The orchestrator
injects a pointer into the teammate's spawn prompt telling it to READ the file
before working and APPEND concise learnings when it discovers something
non-obvious — so e.g. frontend-on-app grows into its project instead of starting
cold on every spawn.

Cockpit-managed and gitignored (lives under ``runtime/``). Lead is intentionally
excluded — it owns the project-wide ``MEMORY.md`` instead.

Seeding is best-effort and never raises: a filesystem failure just means the
pointer isn't injected for that spawn (the pane still works, it just doesn't have
a learned-notes file yet).
"""

from __future__ import annotations

import datetime
import logging
import pathlib
import re

from .config import RUNTIME_DIR

_log = logging.getLogger(__name__)

ROLE_MEMORY_DIR = RUNTIME_DIR / "role-memory"

# Sections every role's notes start with.
_BASE_SECTIONS = """## Conventions / patterns
- (ว่าง — เติมเมื่อเรียนรู้)

## Gotchas / pitfalls
-

## Key decisions / เหตุผล
-
"""

# Extra sections seeded per base role (appended after the base sections).
_ROLE_SECTIONS: dict[str, str] = {
    "qa": """## Test login & accounts
> ⚠️ plaintext — single-user cockpit, gitignored. ใช้ throwaway / test account เท่านั้น
-

## Known flows (ขั้นตอนไปถึงแต่ละหน้า)
-

## Flaky / known-failing
-
""",
    "frontend": """## Components & structure
-

## Build / dev server (รันยังไง)
-

## Styling / UI conventions
-
""",
    "backend": """## Endpoints & schema
-

## Migrations / DB
-

## Local run
-
""",
    "mobile": """## App structure / navigation
-

## Build / run (iOS / Android)
-
""",
    "devops": """## Services / compose / ports
-

## Deploy / CI
-
""",
    "reviewer": """## Recurring review issues ที่นี่
-

## Risky areas
-
""",
    "critic": """## Design system / tokens
-

## Recurring UX issues
-
""",
    "designer": """## Design system / tokens
-

## Recurring UX issues
-
""",
}


def _safe(name: str) -> str:
    """Sanitize a project / role name into ONE safe path segment.

    Dots are dropped (not just other separators) so a ``..`` can never survive as
    a parent-dir-traversal segment, even if a caller bypasses the upstream
    validate_name guard. ``my.proj`` → ``my_proj``; ``..`` → ``__``.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", name) or "default"


def role_memory_path(project: str, base_role: str) -> pathlib.Path:
    """The ``runtime/role-memory/<project>/<role>.md`` path for this (project, role)."""
    return ROLE_MEMORY_DIR / _safe(project) / f"{_safe(base_role)}.md"


def role_memory_archive_path(project: str, base_role: str) -> pathlib.Path:
    """The ``runtime/role-memory/<project>/<role>-archive.md`` L2 archive path (#151).

    Holds the FULL text of entries curation truncated/trimmed out of the L1 file
    at ``role_memory_path``. Deliberately a different filename so nothing that
    reads role-memory by the ``role_memory_path``/``ensure_role_memory`` return
    value ever picks it up — it is never inlined into a spawn prompt.
    """
    return ROLE_MEMORY_DIR / _safe(project) / f"{_safe(base_role)}-archive.md"


def _seed(project: str, base_role: str) -> str:
    header = (
        f"# {base_role} — learned notes · project: {project}\n\n"
        f"> สิ่งที่ **{base_role} เรียนรู้เกี่ยวกับโปรเจคนี้** สะสมข้ามรอบงาน (cockpit per-role memory).\n"
        "> อ่านก่อนเริ่มงาน · **append** สิ่งที่ไม่ obvious เมื่อเจอ.\n"
        "> **กฎตายตัว: entry ละไม่เกิน 2-3 บรรทัด ห้าม paste done report / เรียงความยาว** "
        "(เกิน 600 ตัวอักษรโดนตัดทิ้งอัตโนมัติตอน spawn ครั้งถัดไป).\n"
        '> อย่าซ้ำกับ code / git / โปรเจค MEMORY.md — เก็บเฉพาะ "ความรู้ที่ต้องเสียเวลาค้นใหม่".\n'
        "> entry ที่โดนตัด/ทิ้งจากที่นี่ เนื้อเต็มยังอยู่ที่ "
        f"`{base_role}-archive.md` ข้างๆไฟล์นี้ — ค้นด้วย `takkub search`.\n\n"
    )
    extra = _ROLE_SECTIONS.get(base_role, "")
    body = _BASE_SECTIONS + (("\n" + extra) if extra else "")
    return header + body


# ──────────────────────────────────────────────────────────────────────
# Curation (#43)
#
# The append side is prompt-driven — the agent free-form Edit/Writes the file —
# so there's no programmatic hook to dedup/cap at write time. Instead we curate
# the EXISTING file on read (in ensure_role_memory, which runs on every spawn):
# dedup repeated bullets and cap total size by trimming the OLDEST agent-added
# bullets. This matters because since ab20854 the file CONTENT is inlined into
# the spawn prompt, so unbounded growth directly bloats per-spawn tokens.
#
# Hard rules: best-effort (NEVER raise on the spawn path → fall back to the file
# untouched), preserve the header + seeded section headings verbatim, and never
# f-string/.format note bodies (role-memory legitimately contains literal braces
# like Go templates `{{.State.Health.Status}}`).
_MEM_MAX_BYTES = 6_000
_MEM_MAX_ENTRIES = 120

# Per-entry cap (token-reduction task, 2026-08): agents were observed pasting a
# whole done-report paragraph as ONE bullet (~1,500 tok seen in the wild) — the
# byte/entry budget above can't stop that since a single oversized entry can
# dominate the whole file. Any bullet block longer than this many characters is
# collapsed to its first line/sentence + " …" at curation time.
_MEM_MAX_ENTRY_CHARS = 600

# A content bullet: a `- ` / `* ` marker followed by real text. A bare "-"
# placeholder (the seed's empty sections) deliberately does NOT match, so seed
# placeholders are never deduped or trimmed.
_BULLET_RE = re.compile(r"^\s*[-*]\s+\S")


def _seeded_headings() -> set[str]:
    """All ``## `` headings produced by ``_seed()`` — protected from trimming so
    the template skeleton always survives (the agent expects those sections)."""
    heads = {ln.rstrip() for ln in _BASE_SECTIONS.splitlines() if ln.startswith("## ")}
    for extra in _ROLE_SECTIONS.values():
        heads.update(ln.rstrip() for ln in extra.splitlines() if ln.startswith("## "))
    return heads


def has_learned_content(
    text: str, project: str | None = None, base_role: str | None = None
) -> bool:
    """True iff the role-memory text contains at least one *real* learned bullet —
    a ``- ``/``* `` marker with actual text that is NOT one of the seed skeleton's
    own placeholders.

    The seed isn't purely bare ``-`` markers: ``_BASE_SECTIONS`` ships one
    content-shaped placeholder (``- (ว่าง — เติมเมื่อเรียนรู้)``). So a naive
    ``_BULLET_RE`` scan would read a fresh file as "has content". We therefore
    exclude every bullet the seed itself emits (matched on the same normalized key
    the dedup logic uses), leaving only agent-added bullets. This is conservative:
    a real note can never collide with a seed placeholder's key, so tok-5 can never
    suppress an actual learned note on spawn.
    """
    seeded_keys: set[str] = set()
    if project is not None and base_role is not None:
        seeded_keys = {
            _norm_bullet(ln)
            for ln in _seed(project, base_role).splitlines()
            if _BULLET_RE.match(ln)
        }
    for ln in text.splitlines():
        if _BULLET_RE.match(ln):
            k = _norm_bullet(ln)
            if k and k not in seeded_keys:
                return True
    return False


def _norm_bullet(line: str) -> str:
    """Dedup key for a bullet: drop the marker, lowercase, collapse whitespace,
    strip trailing punctuation. Empty string for non-content lines."""
    s = line.strip()
    if s[:1] in "-*":
        s = s[1:].strip()
    return " ".join(s.lower().split()).rstrip(".·!?,;: ")


def _split_doc(text: str) -> tuple[list[str], list[list]]:
    """Split into (header_lines, sections) where each section is
    ``[heading_line, body_lines]``. Splits ONLY on ``## `` — ``### `` sub-headings
    stay in the body. Round-trips exactly via ``_render`` when unchanged."""
    header: list[str] = []
    sections: list[list] = []
    cur: list | None = None
    for ln in text.split("\n"):
        if ln.startswith("## "):
            cur = [ln, []]
            sections.append(cur)
        elif cur is None:
            header.append(ln)
        else:
            cur[1].append(ln)
    return header, sections


def _render(header: list[str], sections: list[list]) -> str:
    out = list(header)
    for heading, body in sections:
        out.append(heading)
        out.extend(body)
    return "\n".join(out)


def _block_split(body: list[str]) -> list[list]:
    """Group a section body into blocks: a bullet block = a ``- `` line plus its
    following indented/continuation lines (so a multi-line entry is one unit);
    every other line is its own passthrough block. Returns ``[[is_bullet, lines]]``."""
    blocks: list[list] = []
    cur_bullet: list | None = None
    for ln in body:
        if _BULLET_RE.match(ln):
            cur_bullet = [True, [ln]]
            blocks.append(cur_bullet)
        elif cur_bullet is not None and ln.strip() and not ln.startswith("#"):
            cur_bullet[1].append(ln)  # continuation of the current bullet
        else:
            cur_bullet = None
            blocks.append([False, [ln]])
    return blocks


def _blocks_to_lines(blocks: list[list]) -> list[str]:
    out: list[str] = []
    for _is_b, lns in blocks:
        out.extend(lns)
    return out


def _dedup_body(body: list[str]) -> list[str]:
    """Drop earlier duplicate bullet blocks (same normalized key), keeping the
    LAST occurrence so the newest restatement wins."""
    blocks = _block_split(body)
    last_at: dict[str, int] = {}
    for i, (is_b, lns) in enumerate(blocks):
        if is_b:
            k = _norm_bullet(lns[0])
            if k:
                last_at[k] = i
    kept: list[list] = []
    for i, (is_b, lns) in enumerate(blocks):
        if is_b:
            k = _norm_bullet(lns[0])
            if k and last_at.get(k) != i:
                continue  # an earlier duplicate — drop it
        kept.append([is_b, lns])
    return _blocks_to_lines(kept)


# Matches a sentence-ending punctuation mark (., !, ?, or Thai ฯ) followed by
# whitespace or end-of-string — used to find a natural "first sentence" cut
# point inside an over-long entry.
_ENTRY_SENTENCE_RE = re.compile(r"[.!?ฯ](?:\s|$)")


def _truncate_entry(lines: list[str], max_chars: int) -> tuple[list[str], str | None]:
    """Collapse one bullet block to its first line/sentence + ' …' if the
    whole entry (marker + all continuation lines joined) exceeds *max_chars*.

    Cuts at the first sentence-ending punctuation within the budget; falling
    back to the nearest preceding whitespace when no sentence end is found,
    so the cut never lands mid-word. Python string indices are codepoints
    (not UTF-8 bytes), so slicing here can split a combining-mark cluster
    (e.g. a Thai tone mark) from its base character but can never split a
    single character in half. Returns ``(lines, None)`` unchanged when under
    budget; otherwise returns ``(truncated_lines, original_full_text)`` — the
    caller archives *original_full_text* before the cut is applied (#151).
    """
    marker_m = re.match(r"^(\s*[-*]\s+)", lines[0])
    marker = marker_m.group(1) if marker_m else "- "
    first_rest = lines[0][len(marker) :] if marker_m else lines[0]
    joined = " ".join([first_rest.strip(), *(ln.strip() for ln in lines[1:])]).strip()
    if len(marker) + len(joined) <= max_chars:
        return lines, None
    budget = max(0, max_chars - len(marker) - 2)  # reserve room for " …"
    window = joined[:budget]
    m = _ENTRY_SENTENCE_RE.search(window)
    if m:
        cut = m.end()
    else:
        cut = window.rfind(" ")
        if cut <= 0:
            cut = len(window)
    truncated = window[:cut].rstrip()
    return [marker + truncated + " …"], "\n".join(lines)


def _truncate_body(
    body: list[str], max_chars: int = _MEM_MAX_ENTRY_CHARS
) -> tuple[list[str], list[str]]:
    """Apply `_truncate_entry` to every bullet block in a section body.
    Returns ``(new_body_lines, archived_full_texts)`` (#151)."""
    blocks = _block_split(body)
    archived: list[str] = []
    for block in blocks:
        if block[0]:
            new_lines, full = _truncate_entry(block[1], max_chars)
            block[1] = new_lines
            if full is not None:
                archived.append(full)
    return _blocks_to_lines(blocks), archived


def _trim_oldest_bullet(sections: list[list]) -> str | None:
    """Remove the single oldest (topmost, earliest-section) content bullet block.
    Returns its full original text if one was removed, else None (#151)."""
    for sec in sections:
        blocks = _block_split(sec[1])
        for bi, (is_b, lns) in enumerate(blocks):
            if is_b and _norm_bullet(lns[0]):
                full = "\n".join(lns)
                del blocks[bi]
                sec[1] = _blocks_to_lines(blocks)
                return full
    return None


def _curate_text(text: str) -> tuple[str, bool, list[str]]:
    """Return ``(curated_text, changed, archived_entries)``. Best-effort — any
    error → ``(text, False, [])``.

    Dedups repeated bullets within each section (newest wins) and, if the file
    exceeds the byte/entry budget, trims the oldest agent-added bullets until it
    fits — never touching the header or seeded section headings. *archived_entries*
    is the full original text of every entry this pass truncated or trimmed away
    (#151) — dedup drops are NOT archived since the dropped text is a verbatim
    duplicate of the kept entry, nothing is lost."""
    try:
        header, sections = _split_doc(text)
        seeded = _seeded_headings()
        archived: list[str] = []

        for sec in sections:
            sec[1] = _dedup_body(sec[1])
            sec[1], trunc_archived = _truncate_body(sec[1])
            archived.extend(trunc_archived)

        def _over_budget() -> bool:
            n_bul = sum(1 for sec in sections for ln in sec[1] if _BULLET_RE.match(ln))
            if n_bul > _MEM_MAX_ENTRIES:
                return True
            return len(_render(header, sections).encode("utf-8")) > _MEM_MAX_BYTES

        guard = 0
        while _over_budget() and guard < 10_000:
            guard += 1
            trimmed = _trim_oldest_bullet(sections)
            if trimmed is None:
                break
            archived.append(trimmed)

        # Drop truly empty NON-seeded sections. A retained sub-heading is
        # content too, even when all bullets beneath it were trimmed.
        sections = [
            sec for sec in sections if sec[0].rstrip() in seeded or any(ln.strip() for ln in sec[1])
        ]

        new = _render(header, sections)
        return new, (new != text), archived
    except Exception:
        return text, False, []


# ──────────────────────────────────────────────────────────────────────
# L2 archive (#151)
#
# Curation (above) truncates/trims entries out of the L1 file to keep the
# per-spawn inline budget flat. That used to just discard the cut text. The
# archive keeps the FULL original text of everything cut, in a sibling file
# that is never read by the spawn path (only `role_memory_path`'s exact
# filename is inlined into a prompt) — a searchable L2, not a token cost.
_ARCHIVE_MAX_BYTES = 200_000

# Every archived entry is prefixed with "### <date>" on append, so rotation
# can split the file back into (header, [entry, entry, ...]) by that exact
# separator and drop the oldest entries first without re-parsing markdown.
_ARCHIVE_ENTRY_SEP = "\n### "


def _archive_date() -> str:
    """Single-line ISO date for an archive entry's timestamp (no wall-clock
    time-of-day — just enough to see *when*, per #151)."""
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _archive_header(project: str, base_role: str) -> str:
    return (
        f"# {base_role} — archive (L2) · project: {project}\n\n"
        "> เนื้อเต็มของ entry ที่ถูก truncate/trim ออกจาก learned-notes (L1) ตอน curation.\n"
        "> ไฟล์นี้ **ไม่ถูก inject** เข้า spawn prompt — ค้นด้วย `takkub search` เมื่อสงสัยว่าเคยรู้เรื่องนี้มาก่อน.\n"
    )


def _cap_archive(text: str, max_bytes: int) -> str:
    """Drop the OLDEST archived entries (in append order, i.e. from the top)
    until *text* fits *max_bytes*. A no-op if already within budget."""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    parts = text.split(_ARCHIVE_ENTRY_SEP)
    head, entries = parts[0], parts[1:]
    while entries and len(_ARCHIVE_ENTRY_SEP.join([head, *entries]).encode("utf-8")) > max_bytes:
        entries.pop(0)
    return _ARCHIVE_ENTRY_SEP.join([head, *entries])


def _archive_entries(project: str, base_role: str, entries: list[str]) -> None:
    """Append the full text of *entries* to this (project, role)'s L2 archive,
    each stamped with today's date, then cap the file to `_ARCHIVE_MAX_BYTES`
    by rotating out the oldest entries. Best-effort: never raises — a failure
    here must never break curation of the L1 file it's called alongside."""
    if not entries:
        return
    try:
        path = role_memory_archive_path(project, base_role)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = (
            path.read_text(encoding="utf-8", errors="replace")
            if path.exists()
            else _archive_header(project, base_role)
        )
        stamp = _archive_date()
        addition = "".join(f"{_ARCHIVE_ENTRY_SEP}{stamp}\n{e}\n" for e in entries)
        new = _cap_archive(existing + addition, _ARCHIVE_MAX_BYTES)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(new, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        _log.warning("role_memory: failed to archive entries for %s/%s", project, base_role)


# ──────────────────────────────────────────────────────────────────────
# Auto-capture failures (ReflexionMemory-style — task ask was "auto-capture
# failure into role memory")
#
# Until now role-memory only grew when an agent decided to Edit/Write a
# bullet itself — a `done(failed=True)` report went to Lead and nowhere
# else, so the same role could repeat the same failure cold next spawn.
# This appends one bullet automatically from the `done()` path, no agent
# decision required. Every auto-captured line is prefixed with this exact
# "fail — " marker so dedup can match on the REASON alone (ignoring the
# date, which differs per occurrence) without colliding with a bullet an
# agent typed by hand.
_FAIL_ENTRY_RE = re.compile(r"^\s*[-*]\s+\d{4}-\d{2}-\d{2}:\s*fail\s*—\s*(.*)$", re.MULTILINE)


def _fail_entry_key(reason: str) -> str:
    return " ".join(reason.lower().split()).rstrip(".·!?,;: ")


def append_failure_entry(project: str, base_role: str, reason: str) -> bool:
    """Auto-capture a `done(failed=True)` report as one bullet in this
    (project, role)'s learned memory. Returns whether an entry was written
    (``False`` on empty *reason*, a dedup no-op, or any I/O failure).

    Appends under the role's "## Flaky / known-failing" section when the
    seed ships one (qa), else the universal "## Gotchas / pitfalls" section
    every role's seed ships — never invents a new heading. Dedups against
    prior auto-captured entries for the SAME reason (date-independent) so
    a repeat failure collapses to one line instead of piling up, then reruns
    `_curate_text` (dedup/entry-cap/byte-budget, same as `ensure_role_memory`
    applies on every spawn read) so the file never exceeds its spawn-prompt
    budget between spawns either.

    Best-effort: never raises. A filesystem hiccup here must never break the
    `done()` report it's called alongside — the pane still gets a clean
    return either way.
    """
    reason = " ".join((reason or "").split()).strip()
    if not reason:
        return False
    try:
        path = ensure_role_memory(project, base_role)
        if path is None:
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
        header, sections = _split_doc(text)

        heading = "## Flaky / known-failing"
        if not any(sec[0].rstrip() == heading for sec in sections):
            heading = "## Gotchas / pitfalls"
        target = next((sec for sec in sections if sec[0].rstrip() == heading), None)
        if target is None:
            # Neither heading exists (unexpected — every seed ships
            # "## Gotchas / pitfalls"). Append a fresh section rather than
            # silently dropping the failure.
            target = [heading, []]
            sections.append(target)

        new_key = _fail_entry_key(reason)
        for ln in target[1]:
            m = _FAIL_ENTRY_RE.match(ln)
            if m and _fail_entry_key(m.group(1)) == new_key:
                return False  # same failure already recorded — dedup, no-op

        prefix = f"- {_archive_date()}: fail — "
        budget = _MEM_MAX_ENTRY_CHARS - len(prefix)
        if budget > 0 and len(reason) > budget:
            reason = reason[:budget].rstrip() + "…"
        target[1].append(prefix + reason)

        new_text = _render(header, sections)
        curated, _changed, archived = _curate_text(new_text)
        if archived:
            _archive_entries(project, base_role, archived)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(curated, encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:
        _log.warning("append_failure_entry: failed for %s/%s", project, base_role)
        return False


def ensure_role_memory(project: str, base_role: str) -> pathlib.Path | None:
    """Return this (project, role)'s learned-memory path, seeding it if missing.

    Existing files are never overwritten (the role's accumulated learnings are
    preserved). Best-effort: returns None on any filesystem error so the caller
    can simply skip the spawn-prompt injection.
    """
    path = role_memory_path(project, base_role)
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_seed(project, base_role), encoding="utf-8")
        else:
            # Curate the accumulated file (dedup + size-cap, #43) so it can't grow
            # unbounded and bloat the inlined spawn prompt. Best-effort: a failure
            # here just skips curation — the (uncurated) file is still returned.
            try:
                cur = path.read_text(encoding="utf-8", errors="replace")
                new, changed, archived = _curate_text(cur)
                if archived:
                    # Full text of every truncated/trimmed entry, before it's
                    # lost from the L1 file below (#151).
                    _archive_entries(project, base_role, archived)
                if changed:
                    # Atomic replace so a crash mid-write can't leave a torn file.
                    tmp = path.parent / (path.name + ".tmp")
                    tmp.write_text(new, encoding="utf-8")
                    tmp.replace(path)
            except OSError:
                pass
        return path
    except OSError as e:
        _log.warning("ensure_role_memory: %s/%s: %s", project, base_role, e)
        return None
