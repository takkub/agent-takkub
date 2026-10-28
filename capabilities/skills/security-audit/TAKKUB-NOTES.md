# security-audit — vendored

Source: https://github.com/cloudflare/security-audit-skill @ c1c8a8c1471069fb0e188eeaff69b8e8db6564a8 (MIT, see LICENSE).

agent-takkub changes vs upstream:
- `SKILL.md` description shortened: fires only on an explicit audit request (catalog cost + no accidental heavy loads).
- `validate-*.cjs` `readFileWithinLimit`: Windows fallback (lstat no-symlink + fstat dev/ino match) because Node has no `O_NOFOLLOW` on win32 — upstream rejected every input there.

Seeded into the cockpit's global skill store (`DATA_HOME/skills`) by `core/capabilities/skill_store.seed_global_shipped_skills` so every project pane sees it; exclude per project with `skills_exclude`.
