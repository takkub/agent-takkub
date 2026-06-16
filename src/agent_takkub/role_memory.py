"""Per-(role × project) learned memory.

Each teammate role accumulates its own project-specific knowledge across runs in
``runtime/role-memory/<project>/<role>.md``: conventions, gotchas, key decisions,
plus role-specific notes (qa: test login / accounts / flows). The orchestrator
injects a pointer into the teammate's spawn prompt telling it to READ the file
before working and APPEND concise learnings when it discovers something
non-obvious — so e.g. frontend-on-PMS grows into its project instead of starting
cold on every spawn.

Cockpit-managed and gitignored (lives under ``runtime/``). Lead is intentionally
excluded — it owns the project-wide ``MEMORY.md`` instead.

Seeding is best-effort and never raises: a filesystem failure just means the
pointer isn't injected for that spawn (the pane still works, it just doesn't have
a learned-notes file yet).
"""

from __future__ import annotations

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


def _seed(project: str, base_role: str) -> str:
    header = (
        f"# {base_role} — learned notes · project: {project}\n\n"
        f"> สิ่งที่ **{base_role} เรียนรู้เกี่ยวกับโปรเจคนี้** สะสมข้ามรอบงาน (cockpit per-role memory).\n"
        "> อ่านก่อนเริ่มงาน · **append** สิ่งที่ไม่ obvious เมื่อเจอ (bullet สั้น กระชับ).\n"
        '> อย่าซ้ำกับ code / git / โปรเจค MEMORY.md — เก็บเฉพาะ "ความรู้ที่ต้องเสียเวลาค้นใหม่".\n\n'
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
_MEM_MAX_BYTES = 16_000
_MEM_MAX_ENTRIES = 120

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


def _trim_oldest_bullet(sections: list[list]) -> bool:
    """Remove the single oldest (topmost, earliest-section) content bullet block.
    Returns True if one was removed."""
    for sec in sections:
        blocks = _block_split(sec[1])
        for bi, (is_b, lns) in enumerate(blocks):
            if is_b and _norm_bullet(lns[0]):
                del blocks[bi]
                sec[1] = _blocks_to_lines(blocks)
                return True
    return False


def _curate_text(text: str) -> tuple[str, bool]:
    """Return ``(curated_text, changed)``. Best-effort — any error → ``(text, False)``.

    Dedups repeated bullets within each section (newest wins) and, if the file
    exceeds the byte/entry budget, trims the oldest agent-added bullets until it
    fits — never touching the header or seeded section headings."""
    try:
        header, sections = _split_doc(text)
        seeded = _seeded_headings()

        for sec in sections:
            sec[1] = _dedup_body(sec[1])

        def _over_budget() -> bool:
            n_bul = sum(1 for sec in sections for ln in sec[1] if _BULLET_RE.match(ln))
            if n_bul > _MEM_MAX_ENTRIES:
                return True
            return len(_render(header, sections).encode("utf-8")) > _MEM_MAX_BYTES

        guard = 0
        while _over_budget() and guard < 10_000:
            guard += 1
            if not _trim_oldest_bullet(sections):
                break

        # Drop now-empty NON-seeded sections (all their bullets were trimmed);
        # seeded headings stay so the skeleton survives.
        sections = [
            sec
            for sec in sections
            if sec[0].rstrip() in seeded
            or any(ln.strip() and not ln.startswith("#") for ln in sec[1])
        ]

        new = _render(header, sections)
        return new, (new != text)
    except Exception:
        return text, False


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
                new, changed = _curate_text(cur)
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
