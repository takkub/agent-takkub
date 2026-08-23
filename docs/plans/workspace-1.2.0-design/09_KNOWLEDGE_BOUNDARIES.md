# Knowledge Boundaries

Single-owner policy prevents storage chaos.

- Conversation V2 owns session/summary/checkpoint.
- Brain V2 owns operational memory/decisions/findings/preferences with scope/trust/confidence.
- Obsidian owns curated durable human-readable knowledge.
- OpenViking optionally indexes/retrieves knowledge/resources; it is not a second uncontrolled operational-memory owner.
- Graft owns structural code intelligence.
- Capability Hub owns skills/MCP/plugins/permissions.

Other layers may READ / INDEX / REFERENCE canonical data, not independently rewrite the same fact.

Preferred OpenViking boundary:
`Takkub (MIT) -> HTTP/MCP -> OpenViking service (AGPL)`.
Pin supported versions/capabilities; do not follow floating `main`.
