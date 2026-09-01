"""Pure digest-bullet fact model + renderer for the Lead Inbox Digest (#245,
follow-up to #244).

#244 made the identifying `[ref #N]` badge computed-not-prose. #245 carries
that further: the ENTIRE digest bullet becomes a table of facts the cockpit
itself measured (git state + orchestrator state), with the agent's own note
reduced to a one-line, clearly-labelled headline that supplies context only
— never identity. `DigestFacts` is built once by `Orchestrator.done()`
(never per-tick — see the module docstring in worktree_manager.py and
#229/#244 for why that boundary matters on the Qt main thread) and threaded
through the digest queue so `format_digest_fact_line` never has to re-derive
anything by parsing text.

Provider-neutral by construction (#103): every field here is git state or
cockpit-owned PaneState/session data — nothing read from a specific CLI's
terminal output, so it works identically for a codex/gemini/opencode/kimi/
cursor pane as it does for claude.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .worktree_manager import parse_porcelain_paths


@dataclass(frozen=True)
class DigestFacts:
    """Everything one digest bullet needs, pre-computed by `done()` —
    `format_digest_fact_line` never re-derives any of it from prose."""

    role: str
    # "done" is the only verdict that ever reaches the digest today — FAILED
    # and blocked notices bypass the debounce queue entirely (see
    # `_is_blocking_lead_notice` in lead_inbox.py; re-verified structurally
    # for #245, not re-tested here since #244 already covers it). Kept as an
    # explicit field — not hardcoded in the formatter — so a future digest
    # policy change can't silently mislabel a verdict the formatter never
    # expected to see.
    verdict: str = "done"
    ref: str | None = None  # e.g. "#245", from notice_facts.extract_issue_ref
    branch: str | None = None
    commits_ahead: int | None = None  # None = not computed
    uncommitted: int | None = None  # None = couldn't check
    # True = `branch` is currently present as `origin/<branch>` (#462 — a
    # worktree-isolated pane may push its own `wt/*` branch, #438). Only ever
    # meaningful for a worktree pane; a shared-tree pane commits directly on
    # a branch Lead already sees, so this stays False there.
    pushed: bool = False
    # True = merge-tree conflicts with the CURRENT base, False = clean,
    # None = not applicable (nothing to merge) or genuinely unverifiable —
    # `merge_note` explains which.
    merge_conflicts: bool | None = None
    merge_note: str = ""
    # None = unverifiable — NEVER a bare 0 standing in for "couldn't check"
    # (the #245 requirement: a fake zero is more misleading than an honest
    # "ตรวจไม่ได้"). `files_note` carries the caveat/reason either way.
    files_touched: int | None = None
    files_dirs: tuple[str, ...] = field(default_factory=tuple)
    files_note: str = ""
    report_path: str | None = None
    headline: str = ""  # first line of the agent's own note — CONTEXT ONLY


def _merge_bit(facts: DigestFacts) -> str:
    if facts.merge_conflicts is True:
        return "merge:CONFLICT"
    if facts.merge_conflicts is False:
        return "merge:clean"
    return f"merge:{facts.merge_note}" if facts.merge_note else "merge:ไม่ทราบ"


def _files_bit(facts: DigestFacts) -> str:
    if facts.files_touched is None:
        suffix = f" ({facts.files_note})" if facts.files_note else ""
        return f"ไฟล์ที่แตะ:ตรวจไม่ได้{suffix}"
    note = f" · {facts.files_note}" if facts.files_note else ""
    if facts.files_touched == 0:
        # #278: a measured (not unverifiable) zero is the one file count that
        # has to be loud. The incident it comes from: a pane reported done
        # with a complete-looking summary and Lead only found out nothing had
        # been built by opening the target file by hand. `0` sitting quietly
        # in the middle of a fact row reads as just another number, so it is
        # marked — advisory, never blocking, because analysis/QA/research
        # tasks legitimately finish having touched nothing.
        return f"⚠️ ไฟล์ที่แตะ:0 — ยังไม่มีอะไรเปลี่ยน (ถ้าเป็นงาน implementation ให้ตรวจก่อนปิด){note}"
    dirs = f" ({', '.join(facts.files_dirs[:5])})" if facts.files_dirs else ""
    return f"ไฟล์ที่แตะ:{facts.files_touched}{dirs}{note}"


def format_digest_fact_line(facts: DigestFacts, *, stamp: str = "") -> str:
    """Render one digest bullet as a fact table, never a prose sentence.

    Every clause up to the headline is a token the cockpit itself computed —
    the reader never has to trust anything the reporting pane typed to know
    WHAT happened (verdict/ref/branch/commits/files). The trailing headline
    (``↳``) and report path are visually set apart and documented as
    context/pointer, never identity.
    """
    head = f"[{facts.role}] {facts.verdict}"
    if facts.ref:
        head += f" [ref {facts.ref}]"
    bits = [head]
    if facts.branch:
        bits.append(f"branch:{facts.branch}")
    if facts.commits_ahead is not None:
        bits.append(f"{facts.commits_ahead} commit ahead")
    if facts.uncommitted is not None:
        bits.append(
            f"⚠{facts.uncommitted} ไฟล์ยังไม่ commit" if facts.uncommitted else "0 ไฟล์ค้าง commit"
        )
    if facts.pushed and facts.branch:
        bits.append(f"pushed:origin/{facts.branch}")
    bits.append(_merge_bit(facts))
    bits.append(_files_bit(facts))
    line = f"• {stamp}" + " · ".join(bits)
    extra: list[str] = []
    if facts.headline:
        extra.append(f"   ↳ {facts.headline}")
    if facts.report_path:
        extra.append(f"   report: {facts.report_path}")
    return "\n".join([line, *extra]) if extra else line


def union_files_touched(
    diffstat_text: str, uncommitted: str | Iterable[str]
) -> tuple[int, list[str]]:
    """Combine committed-diff paths (since a baseline SHA) with currently
    changed dirty paths into one deduped "files touched" count + top-level
    dir list.

    A porcelain string is still accepted for the isolated legacy callers and
    pure tests.  Shared-tree done reports pass the already-filtered path list
    produced by the assign/done metadata snapshots (#251), so pre-existing
    dirty files that did not change during the assignment are excluded.
    """
    committed_paths = [
        line.split("|", 1)[0].strip() for line in diffstat_text.strip().splitlines() if "|" in line
    ]
    uncommitted_paths = (
        parse_porcelain_paths(uncommitted) if isinstance(uncommitted, str) else uncommitted
    )
    all_paths = {p for p in committed_paths if p} | {p for p in uncommitted_paths if p}
    dirs: list[str] = []
    for path in sorted(all_paths):
        top = path.split("/", 1)[0] if "/" in path else path
        if top and top not in dirs:
            dirs.append(top)
    return len(all_paths), dirs
