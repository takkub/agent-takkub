"""Pure fact-extraction helpers for Lead-facing notices (issue #244).

Digest content must lean on facts the cockpit itself computed, never on
prose the reporting pane typed — an agent can (and did, twice in one night,
see #244) mistype the issue number it is reporting on. `extract_issue_ref`
pulls the identifying reference from the ORIGINAL assign spec text Lead
sent (`PaneState.last_assigned_task`), not from the agent's own `done()`
note, so a mistyped headline can never mislabel which issue a report is
about.

#733: the stored assign text is NOT Lead's text verbatim — the orchestrator
prepends its own preambles to it before storing/delivering it. The codex
`task_notice_preamble` (ProviderSpec) is such a preamble and cites `#641`,
so on every codex pane the "first `#N` in the assign text" was the
orchestrator's own issue number, never Lead's: a codex digest for a task
about #725/#731 shipped `[ref #641]`. The ref must therefore be read from
Lead's own words only — orchestrator-authored preambles are removed first.

Provider-neutral by construction (#103): the source text is cockpit state
(the assign spec), not anything read from a specific CLI's terminal output,
so this works identically regardless of which provider the reporting pane
runs. The preamble removal is registry-driven rather than codex-specific,
so a future provider notice can't reintroduce the same mislabel.
"""

from __future__ import annotations

import re

_ISSUE_REF_RE = re.compile(r"#(\d{1,6})\b")


def _orchestrator_preambles() -> tuple[str, ...]:
    """Every orchestrator-authored preamble a provider may prepend to a task.

    Sourced from ``ProviderSpec.task_notice_preamble`` (the single source of
    truth `orchestrator_text._rewrite_task_for_codex` prepends) so a notice
    edited there can never silently drift away from what is stripped here.
    """
    from .provider_spec import PROVIDER_REGISTRY

    out: list[str] = []
    for spec in PROVIDER_REGISTRY.values():
        notice = getattr(spec, "task_notice_preamble", "") or ""
        if notice.strip() and notice not in out:
            out.append(notice)
    return tuple(out)


def lead_owned_text(assign_task_text: str | None) -> str | None:
    """*assign_task_text* with orchestrator-injected preambles removed.

    The preambles are removed as EXACT text, not by marker scanning: the
    stored payload is byte-for-byte ``<preamble><Lead's spec>``, so a whole-
    string removal is both exact and immune to any issue number the preamble
    happens to cite. A preamble that ends the line keeps its own newline
    boundary, so what is left is Lead's spec verbatim; one that ends mid-line
    gets a newline instead of being glued to the first word of the spec.
    Anything the removal can't account for is left alone — an unrecognised
    prefix must never be silently reinterpreted as Lead's spec.
    """
    if not assign_task_text:
        return assign_task_text
    text = assign_task_text
    for preamble in _orchestrator_preambles():
        if preamble in text:
            text = text.replace(preamble, "" if preamble.endswith("\n") else "\n", 1)
    return text


def extract_issue_ref(assign_task_text: str | None) -> str | None:
    """First ``#<number>`` token in LEAD's part of *assign_task_text*.

    *assign_task_text* must be the Lead's own ORIGINAL assign spec
    (``PaneState.last_assigned_task``) — never the agent's self-reported
    ``done()`` note, which is exactly the untrustworthy source #244 flagged,
    and passed through :func:`lead_owned_text` first so an orchestrator
    preamble ahead of it (#733) cannot win the match. Returns ``None`` when
    Lead's own text carried no issue reference — an orchestrator-only
    reference is not a reference to this task's issue, so nothing is shown.
    """
    text = lead_owned_text(assign_task_text)
    if not text:
        return None
    m = _ISSUE_REF_RE.search(text)
    return f"#{m.group(1)}" if m else None
