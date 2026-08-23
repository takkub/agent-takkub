# Obsidian Hardening

Keep existing concepts:
- `01-Projects/` durable project knowledge,
- `02-Areas/` cross-project durable knowledge,
- `99-Logs/` temporary/session material.

Improve:
1. canonical metadata (`knowledge_id`, `project_id`, `source`, `kind`, `trust`, timestamps, `content_hash`),
2. persistent dedup across restarts,
3. stable project ID instead of display name as identity,
4. raw transcript/log exclusion from OpenViking,
5. OpenViking allowlist curated folders only.

Default deny for indexing: `99-Logs`, `.obsidian`, raw transcripts, runtime, secrets.
