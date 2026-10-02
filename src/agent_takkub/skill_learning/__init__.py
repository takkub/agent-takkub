"""Skill Learning — the cockpit distills reusable skills from real work,
for every provider (backlog 2c6cb77c, design: docs/architecture/skill-learning.md).

Idea credit: tigerless-labs/autoharness (a Claude-only plugin). This package
is our own provider-neutral rewrite: the trigger is `takkub done` (every
provider passes through it), evidence comes from `core.conversation.ingest`
adapters (all five providers) with the PTY transcript as fallback, the
reflector is a one-shot exec of whichever provider is installed and has
quota, and learned skills land in the cockpit's central skill store so the
existing link/appendix machinery carries them to every pane.

Qt-free on purpose: `pipeline.on_done` runs on a `bg_pool` worker and reports
back through a callback the orchestrator turns into a queued signal.
"""

from __future__ import annotations

MANAGED_BY = "takkub-skill-learning"
