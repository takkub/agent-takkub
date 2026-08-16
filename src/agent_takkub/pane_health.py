"""Pane-health accumulator — watch quietly, report at the end.

The cockpit watches every pane it spawns: how long the provider took to boot,
whether the task paste was ever confirmed, whether delivery had to be retried,
whether a watchdog respawned it. That watching is worth keeping — it is what
stops work from vanishing silently. Narrating it to Lead *while it happens*
was not.

Every observation used to be its own Lead-facing notice, fired the moment a
watchdog noticed something: `[delivery-busy-wait]`, `[delivery-boot-stall]`,
`[delivery-unconfirmed]`, degrade/respawn notes, `[delivery-superseded]`. Each
one interrupts Lead with a status update about a pane that is still running and
may well finish normally a moment later — and by the time Lead reads it, it is
usually already out of date (which is why `_revalidate_system_notice` had to
exist at all). The result was a Lead pane whose scrollback was mostly the
cockpit talking about itself.

So: the watchdogs still run, still recover, still fail a task explicitly when
it can no longer succeed. What changes is *when Lead hears about it* — the
observations accumulate here, per pane lifecycle, and are folded into that
pane's report at its terminal event (`done` / `done --fail` / `close`), which
is the moment Lead is actually reading about that role anyway.

Two classes of event never come here, because "report at the end" cannot work
for them:

  * there will BE no end — `[spawn-failed]` (no pane exists), `[spawn-stuck]`
    (nothing ever spawned), `[respawn-capped]` (permanently down);
  * the pane cannot proceed without a human — an auth wall, or a modal
    waiting on a keypress. These block until answered, so a report at close
    would arrive only after somebody had already noticed by hand.

Those keep firing immediately. Everything else waits for the report.

Policy is `TAKKUB_PANE_WATCH_NOTICES`: ``terminal`` (default, the behaviour
above), ``live`` (the old one notice per observation), or ``off`` (record
nothing, report nothing — for a session that wants the pane report and nothing
else).

Layer rule: pure model + policy. MUST NOT import orchestrator / main_window /
app / cli.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Cap per pane lifecycle. A pane in a retry loop can generate the same
# observation repeatedly; the summary counts them rather than listing them, so
# this only bounds worst-case memory, never what Lead is told.
MAX_EVENTS_PER_PANE = 50

POLICY_TERMINAL = "terminal"
POLICY_LIVE = "live"
POLICY_OFF = "off"
_VALID_POLICIES = (POLICY_TERMINAL, POLICY_LIVE, POLICY_OFF)


def watch_policy() -> str:
    """Current notice policy. Read at call time (not import) so the cockpit
    and tests can change it without a rebuild; an unrecognised value falls back
    to the default rather than silencing anything by accident."""
    raw = (os.environ.get("TAKKUB_PANE_WATCH_NOTICES") or "").strip().lower()
    return raw if raw in _VALID_POLICIES else POLICY_TERMINAL


@dataclass
class PaneHealthEvent:
    """One thing a watchdog noticed about a pane."""

    kind: str  # stable slug, e.g. "boot-slow" — grouped by this in the summary
    detail: str = ""  # short human phrase; the FIRST one seen wins per kind
    ts: float = 0.0


@dataclass
class PaneHealth:
    events: list[PaneHealthEvent] = field(default_factory=list)

    def record(self, kind: str, detail: str = "", ts: float = 0.0) -> None:
        if len(self.events) >= MAX_EVENTS_PER_PANE:
            return
        self.events.append(PaneHealthEvent(kind=kind, detail=detail, ts=ts))


def summarize(health: PaneHealth | None) -> str:
    """One line for the pane's report, or "" when nothing notable happened.

    Grouped by kind with a count, so a delivery that retried eleven times is
    one clause reading `×11` rather than eleven notices. The first detail seen
    for a kind is kept — for the events that carry a number (how long the boot
    took, how long delivery waited) the first is the threshold that tripped the
    watchdog, which is the informative one.
    """
    if health is None or not health.events:
        return ""
    order: list[str] = []
    details: dict[str, str] = {}
    counts: dict[str, int] = {}
    for ev in health.events:
        if ev.kind not in counts:
            order.append(ev.kind)
            details[ev.kind] = ev.detail
            counts[ev.kind] = 0
        counts[ev.kind] += 1
    parts = []
    for kind in order:
        label = details.get(kind) or kind
        parts.append(f"{label} ×{counts[kind]}" if counts[kind] > 1 else label)
    return "🩺 [pane health] " + " · ".join(parts)
