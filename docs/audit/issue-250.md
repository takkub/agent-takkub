# #250 — gate `_DEV_SERVER_HYGIENE` / `STALE_FILE_GUARD` per role

## What changed

Two of the four "central hygiene blocks" baked unconditionally into every teammate's
staged `runtime/agents/<role>/CLAUDE.md` are now gated per role, following the same
pattern already used for `GRAFT_TOOL_CAVEATS` (spawn_engine.py) — inject only when the
role can actually use the guard, fail-safe (unknown/custom role → guard stays):

- `_DEV_SERVER_HYGIENE` (592 chars, `config.py`) — the `next build && next start` rule.
  Only makes sense for a role that might run/build a web app.
- `STALE_FILE_GUARD` (933 chars, `lead_context.py`, injected from `spawn_engine.py`) —
  the "File has been modified since read" retry-loop guard. Only makes sense for a role
  that holds an Edit/Write tool.

`_NON_INTERACTIVE_HYGIENE`, `_TOKEN_DISCIPLINE_HYGIENE`, and `BIG_FILE_GUARD` are **not**
touched — out of scope per the issue (every role can hit a `y/N` shell prompt or read a
giant file, so those stay unconditional).

## Single source of truth

`config.py` declares both capability sets + accessor functions once; `spawn_engine.py`
imports `role_needs_stale_file_guard` (one new import, one `if` around the existing
`_appendix += STALE_FILE_GUARD` line) instead of re-declaring anything:

```python
# config.py
_NO_DEV_SERVER_ROLES: frozenset[str] = frozenset({...})   # 9 roles
_NO_FILE_EDIT_ROLES: frozenset[str] = frozenset({...})    # 2 roles

def role_needs_dev_server_guard(role: str) -> bool:
    return role not in _NO_DEV_SERVER_ROLES

def role_needs_stale_file_guard(role: str) -> bool:
    return role not in _NO_FILE_EDIT_ROLES
```

`agent_role_dir()` (config.py, called for every claude-backed teammate spawn) uses
`role_needs_dev_server_guard` directly. `spawn_engine.py`'s appendix builder (which
writes into the *same* staged `CLAUDE.md` file, right after `agent_role_dir()` runs —
see that function's `_appendix` block) now reads `role_needs_stale_file_guard`.

**Fail-safe by construction**: both sets are *exclude* lists, never *include* lists.
A role not in either set — a typo, a not-yet-classified registered role, or an A6
custom role from the Role Manager (`CUSTOM_AGENTS_DIR`, never appears in either set) —
falls through to "needs the guard" automatically, no extra code required.

Shard suffixes (`backend#2`, `qa#3`) were already resolved to the base role name
*before* reaching either gate (`base_role, shard_idx = _split_shard(role_name)` in
`spawn_engine.py`, and `agent_role_dir()`'s only production caller already passes
`base_role`) — no new resolution logic needed there.

Non-claude provider panes (`spawn_engine.py:1702`'s `ensure_agents_md()` path — codex/
gemini/opencode/kimi/cursor running their own native CLI) are **not** touched; this gate
only applies on the claude-spawn path, i.e. Lead itself and any pane running claude
(including claude-substitute spawns for an unavailable non-claude provider, which is
exactly when `codex`/`gemini`/`opencode`/`kimi`/`cursor` route through `agent_role_dir()`
under their own role name). Flagged for #103, not fixed here.

## Role classification (read `.claude/agents/<role>.md` for each, not guessed)

| Role | Runs/builds a dev server? | Holds Edit/Write? | dev-server guard | stale-file guard |
|---|---|---|---|---|
| frontend | yes (own role) | yes | kept | kept |
| backend | yes (own role) | yes | kept | kept |
| mobile | yes (own role) | yes | kept | kept |
| devops | yes (own role) | yes | kept | kept |
| qa | drives browser against a live app, may bring it up | yes | **kept** | kept |
| critic | drives browser against a live app, may bring it up | yes | **kept** | kept |
| designer | drives browser against a live app, may bring it up | yes | **kept** | kept |
| reviewer | no — Read/Bash only, reviews code | **no** — no Write/Edit at all | **cut** | **cut** |
| gemini (claude-substitute) | no — Read/Bash only, planning | **no** — no Write/Edit at all | **cut** | **cut** |
| docs | no — writes docs only | yes (Write, own dir) | **cut** | kept |
| analyst | no — writes specs only | yes (Write, own dir) | **cut** | kept |
| security | no — writes findings only | yes (Write, own dir) | **cut** | kept |
| codex (claude-substitute) | no — refactor/cross-check, no dev-server step | yes ("Read/Grep/Glob/Bash/Edit") | **cut** | kept |
| opencode (claude-substitute) | no | yes (same shape as codex) | **cut** | kept |
| kimi (claude-substitute) | no | yes (same shape as codex) | **cut** | kept |
| cursor (claude-substitute) | no | yes (same shape as codex) | **cut** | kept |
| lead | n/a — never goes through `agent_role_dir()`/this appendix path | n/a | n/a | n/a |
| *any unrecognised/custom role* | unknown → assume yes (fail-safe) | unknown → assume yes (fail-safe) | **kept** | **kept** |

`qa`/`critic`/`designer` are deliberately **not** cut, per the issue's explicit warning:
they drive a browser (Playwright/chrome-devtools MCP) against a running app and may have
to bring that app up themselves when devops isn't in the loop — cutting the guard there
would reintroduce the worst failure mode it exists to prevent (a `next dev` foreground
run wedging an unattended pane overnight).

## Before/after size

### `_DEV_SERVER_HYGIENE` component (measured — `config.agent_role_dir()` staged `CLAUDE.md`, isolated `RUNTIME_DIR`/`AGENTS_DIR`, no project-memory/skill/permission-gate noise)

| role | source `.md` (bytes) | staged before (always +592 chars) | staged after | saved |
|---|---:|---:|---:|---:|
| frontend | 14,234 | 17,440 | 17,440 | 0 |
| backend | 13,809 | 17,015 | 17,015 | 0 |
| mobile | 14,051 | 17,279 | 17,279 | 0 |
| devops | 16,488 | 19,715 | 19,715 | 0 |
| qa | 15,323 | 18,542 | 18,542 | 0 |
| critic | 16,078 | 19,346 | 19,346 | 0 |
| designer | 11,325 | 14,523 | 14,523 | 0 |
| reviewer | 14,336 | 17,545 | **16,599** | 946 |
| gemini | 10,379 | 13,501 | **12,555** | 946 |
| docs | 14,813 | 18,108 | **17,162** | 946 |
| analyst | 13,437 | 16,719 | **15,773** | 946 |
| security | 14,561 | 17,857 | **16,911** | 946 |
| codex | 9,821 | 12,935 | **11,989** | 946 |
| opencode | 9,504 | 12,616 | **11,670** | 946 |
| kimi | 9,506 | 12,628 | **11,682** | 946 |
| cursor | 9,580 | 12,702 | **11,756** | 946 |
| unrecognised/custom (e.g. `data-eng`) | ~24 | 3,185 | **3,185** | 0 (fail-safe) |

(946 bytes = UTF-8 size of `_DEV_SERVER_HYGIENE`, 592 chars — Thai text is multi-byte.)

### `STALE_FILE_GUARD` component (analytic — lives in `spawn_engine.py`'s appendix, written into the same staged file; not reproduced via a full spawn simulation, size is fixed and unconditional per role)

933 chars / 1,615 UTF-8 bytes, cut only for `reviewer` and `gemini` (the two roles with
no Edit/Write tool at all) — every other role, including the 7 "dev-server-cut" roles
above (docs/analyst/security/codex/opencode/kimi/cursor) still writes files, so it stays.

### Combined effect (both guards)

| role | dev-server guard saved | stale-file guard saved | total saved (chars / bytes) |
|---|---:|---:|---:|
| reviewer | 592 | 933 | 1,525 chars / 2,561 bytes |
| gemini | 592 | 933 | 1,525 chars / 2,561 bytes |
| docs | 592 | 0 | 592 chars / 946 bytes |
| analyst | 592 | 0 | 592 chars / 946 bytes |
| security | 592 | 0 | 592 chars / 946 bytes |
| codex | 592 | 0 | 592 chars / 946 bytes |
| opencode | 592 | 0 | 592 chars / 946 bytes |
| kimi | 592 | 0 | 592 chars / 946 bytes |
| cursor | 592 | 0 | 592 chars / 946 bytes |
| frontend / backend / mobile / devops / qa / critic / designer | 0 | 0 | 0 (both proven necessary) |
| unrecognised/custom role | 0 | 0 | 0 (fail-safe: guard kept) |

## Tests

`tests/test_config.py::TestRoleGuardCapability` (new):
- both accessor functions return `False` for every proven-safe role, `True` for every
  role proven to need the guard (frontend/backend/mobile/devops/qa/critic/designer/lead)
- **fail-safe regression**: `role_needs_dev_server_guard`/`role_needs_stale_file_guard`
  return `True` for an unrecognised name (`"totally-unrecognised-role-xyz"`), an A6-style
  custom role name (`"data-eng"`), and `""`
- `agent_role_dir()` integration: `next build && next start` text is absent from
  reviewer's staged `CLAUDE.md`, present for backend's, and present for an unrecognised
  custom role's (proves the fail-safe path end-to-end, not just the accessor function)

`role_needs_stale_file_guard`'s use inside `spawn_engine.py` is a single `if` around an
existing line delegating to the now-tested `config` function — no separate integration
test added (the surrounding spawn function is a large, heavily-mocked monolith with no
existing precedent for testing individual `_appendix` lines in isolation; `GRAFT_TOOL_CAVEATS`,
the prior art for this pattern, isn't unit-tested at that call site either — only its
underlying policy function is).

Ran: `ruff check`, `ruff format --check`, `lint-imports` (25/25 contracts kept),
`pytest tests/test_config.py tests/test_project_memory_inject.py` — all green.
