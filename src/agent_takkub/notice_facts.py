"""Pure fact-extraction helpers for Lead-facing notices (issue #244).

Digest content must lean on facts the cockpit itself computed, never on
prose the reporting pane typed — an agent can (and did, twice in one night,
see #244) mistype the issue number it is reporting on. `extract_issue_ref`
pulls the identifying reference from the ORIGINAL assign spec text Lead
sent (`PaneState.last_assigned_task`), not from the agent's own `done()`
note, so a mistyped headline can never mislabel which issue a report is
about.

Provider-neutral by construction (#103): the source text is cockpit state
(the assign spec), not anything read from a specific CLI's terminal output,
so this works identically regardless of which provider the reporting pane
runs.
"""

from __future__ import annotations

import re

_ISSUE_REF_RE = re.compile(r"#(\d{1,6})\b")


def extract_issue_ref(assign_task_text: str | None) -> str | None:
    """First ``#<number>`` token found in *assign_task_text*.

    *assign_task_text* must be the Lead's own ORIGINAL assign spec
    (``PaneState.last_assigned_task``) — never the agent's self-reported
    ``done()`` note, which is exactly the untrustworthy source #244 flagged.
    Returns ``None`` when the assign text carried no issue reference.
    """
    if not assign_task_text:
        return None
    m = _ISSUE_REF_RE.search(assign_task_text)
    return f"#{m.group(1)}" if m else None
