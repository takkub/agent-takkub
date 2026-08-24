# Adaptive Escalation

Never freeze complexity at assign time.

Escalate when:
- impacted files exceed prediction
- dependency graph expands
- schema/API change appears
- tests expose cross-module effects
- privileged/high-risk capability needed
- design/reference sources become necessary

Prefer incremental context enrichment over restarting the agent.

Trace:
initial=small
final=medium
reason="impacted_files 1 -> 7"

Never de-escalate high-risk domains below MEDIUM.
