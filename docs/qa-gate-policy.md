# QA gate policy — full detail (#516 token diet)

Root `CLAUDE.md` carries only the one-line headline of this rule (every pane
auto-loads that file, so it stays terse). Read this file when you actually
need the detail — the batch-gate mechanics, the Node/Python distinction, or
why `--targeted` doesn't exist on Node.

## Test tiers (user directive 2026-09-04 #485 — gate once per batch, never run it often)

Specialists **must not run `takkub qa-gate` themselves** — finish the work,
write a regression test for what you touched (#478), run at most
**targeted tests for the files you changed**, then `takkub done`.

Batch flow: every dev pane finishes → **qa pane tests what just changed**
(targeted functional/e2e) → once that's clean, run **`takkub qa-gate
--auto` once** covering everything changed in that batch, before
merge/push. Not a gate per `done` — full pytest/vitest saturates the whole
machine's CPU and stalls the user's machine for the day.

`--auto` (#436) picks the tier from `git diff` itself and prints its
reasoning:
- style/asset/i18n/markdown only → no test run at all (Node: typecheck
  only)
- logic in a mappable module → targeted
- api/auth/schema/shared/tooling/lockfile → full (a signature-drift fake
  raises inside a QTimer slot → a silent PyQt6 abort, exit 127, that a
  targeted run never catches — full tier exists to catch exactly this)

CI is the final full-gate arbiter. One entrypoint: `takkub qa-gate`
(`--targeted <paths>` = narrow, `--auto` = pick automatically, no flag =
full; #325). Reports live in `~/.agent-takkub/runtime/qa-reports/` (never in
the repo) — never invoke `pytest`/`ruff check`/`lint-imports` raw yourself.

## Scope: this repo (Python) only

Every other kind of project still calls `takkub qa-gate` the same way, but
it delegates to that project's own checks (Node #329/#368: **typecheck
always** — `verify` script › `typecheck` script › `tsc -p tsconfig.json
--noEmit` (root or every workspace package) › no TS = `test` only — then
`test` › eslint; package manager follows the lockfile: pnpm/yarn/bun/npm;
a red typecheck is a FAIL even with green tests). `--targeted` is
Python-only — on a Node project the gate says plainly that the paths you
gave don't narrow anything (typecheck+test still run the whole project).
