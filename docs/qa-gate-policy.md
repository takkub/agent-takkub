# QA gate policy — full detail (#516 token diet)

Root `CLAUDE.md` carries only the one-line headline of this rule (every pane
auto-loads that file, so it stays terse). Read this file when you actually
need the detail — the batch-gate mechanics, the Node/Python distinction, or
why `--targeted` doesn't exist on Node.

## Test tiers (user directives #485, #585 — gate tied to task scope, never run it often)

Specialists **must not run `takkub qa-gate` themselves** (for `scope=tiny` tasks,
`pane_guard` actively blocks `takkub qa-gate`). Finish the work, test the change
for real (run it, open it in browser, or invoke function; write regression tests
only when `scope=deep` or explicitly requested), run at most **targeted tests for
the files you changed**, then `takkub done`.

Batch flow is tied to the **highest scope in the batch** (#585):
- **tiny / normal only**: **No `takkub qa-gate` is run.** Dev panes test their
  own changes directly and Lead reviews the diff. Full-suite testing is skipped.
- **contains `deep` tasks**: Once dev panes finish and qa tests what changed,
  the qa pane runs **`takkub qa-gate --auto` once** at the end of the batch
  before merge/push. Not a gate per `done` — full pytest/vitest saturates the
  whole machine's CPU and stalls the user's machine for the day.


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
