# Security audit — agent-takkub

**Date:** 2026-05-21
**Reference:** codex OMA review findings (HIGH checklist)
**Scope:** 5 checks against `src/agent_takkub/`

---

## Check 1 — Env leak: sensitive env vars to teammate panes

**Verification method:** Read `src/agent_takkub/orchestrator.py` lines 1054–1067 and `src/agent_takkub/pty_session.py` lines 123–158. Look at `env=` argument passed to `winpty.PtyProcess.spawn`.

**Evidence:**

`orchestrator.py:1054` builds env via `os.environ.copy()` and selectively injects cockpit-specific keys before passing to `PtySession.spawn()`:

```python
env = os.environ.copy()            # line 1054 — full copy of parent env
env["TAKKUB_ROLE"] = role_name     # line 1055
env["TAKKUB_PROJECT"] = project_ns # line 1061
if role_name == LEAD.name:
    env["TAKKUB_LEAD_TOKEN"] = self._lead_token  # line 1066–1067
```

`pty_session.py:144` passes `env` verbatim to `winpty.PtyProcess.spawn(env=env)`.

This means any `ANTHROPIC_API_KEY`, `AWS_*`, `GH_TOKEN`, or `OPENAI_API_KEY` present in the cockpit's own environment is **inherited by every teammate pane** via `os.environ.copy()`.

**Status: FIXED** *(2026-05-21 round 2: codex/gemini paths; 2026-05-21 round 3: claude teammate path)*

**Resolution:**

`_build_pane_env()` added to `src/agent_takkub/orchestrator.py` (near top of module). All three `os.environ.copy()` calls in the spawn path replaced with filtered env builds:

- **Codex path** (~line 1000): `_build_pane_env()` — round-2 fix
- **Gemini path** (~line 1044): `_build_pane_env()` — round-2 fix
- **Claude path** (~line 1120): `os.environ.copy() if role_name == LEAD.name else _build_pane_env()` — round-3 fix (this commit). Lead retains full env so user-level tools (gh, docker, etc.) work; teammates get the allowlist-filtered env.

The function keeps only keys whose `.upper()` form appears in `_PANE_ENV_ALLOWLIST` — a `frozenset` of ~25 OS-essential, Node-tooling, and cockpit-injected variables. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GH_TOKEN`, `AWS_*`, and any other secret-bearing vars are dropped for non-Lead panes.

Test coverage:
- `tests/test_orchestrator_env_allowlist.py` (9 tests): unit-tests `_build_pane_env()` directly — inclusion/exclusion/case-insensitivity/dict-type
- `tests/test_orchestrator_claude_env_leak.py` (12 tests): **spawn-level regression** — monkeypatches `PtySession.spawn` to capture the actual `env` dict passed at spawn time; asserts teammate panes exclude all four secret vars, Lead pane retains them, and both paths always carry `TAKKUB_ROLE`/`TAKKUB_PROJECT`

---

## Check 2 — Auto-approve flags in pane spawns

**Verification method:** Grep `src/` and `.claude/agents/*.md` for `--dangerously-skip-permissions`, `--yolo`, `--full-auto`.

**Evidence:**

`orchestrator.py:1131` — **teammate** panes spawn with `--dangerously-skip-permissions` intentionally:

```python
else:
    argv: list[str] = [
        claude,
        "--dangerously-skip-permissions",   # line 1131
        "--setting-sources",
        sources,
    ]
```

`orchestrator.py:1115–1127` — **Lead** pane explicitly omits `--dangerously-skip-permissions` and uses `--permission-mode acceptEdits` instead, so Lead is subject to `permissions.deny` rules enforced via `--settings` (lead guard file).

`orchestrator.py:985–994` — codex pane uses `--ask-for-approval never -s workspace-write` (codex-specific flags, not claude flags).

`.claude/agents/*.md` — no auto-approve flags found (grep returned no matches).

**Status: clean** (by design — teammates are intentionally trusted with `--dangerously-skip-permissions`; Lead is correctly protected)

---

## Check 3 — Shell-string injection: `subprocess.run(shell=True)` with user-controlled input

**Verification method:** Grep `src/` for `shell=True`. For each hit, verify the command source.

**Evidence:**

Module doc comments in `codex_helper.py:17`, `gemini_helper.py:17`, and `update_helper.py:19` explicitly state `never shell=True`. No `shell=True` found in any `subprocess.run()` call in `src/`.

The one indirect concern: `pty_session.py:132` uses `subprocess.list2cmdline(list(argv))` to produce a command string for `winpty.PtyProcess.spawn(cmd, ...)`. `list2cmdline` is Windows-safe (proper quoting) and `argv` is constructed from hardcoded literals in orchestrator, not user input. No injection vector.

**Status: clean**

---

## Check 4 — Broad HOME-level mutation outside `~/.takkub/` and `~/.claude/`

**Verification method:** Grep `src/` for `Path.home()` and `os.path.expanduser`. For each hit, check if `write_text`, `unlink`, or `rmtree` is called on the resulting path.

**Evidence:**

Hits reviewed:

| Location | Path constructed | Write/Delete? | Assessment |
|---|---|---|---|
| `provider_config.py:46` | `~/.takkub/role-providers.json` | read only | safe |
| `chatlog_scanner.py:36` | `~/.claude/projects` | read only | safe |
| `orchestrator.py:183` | `~/WebstormProjects/second-brain` | write (vault mirror) | within declared namespace |
| `orchestrator.py:657` | `~/.claude/plugins/cache` | read only | safe |
| `orchestrator.py:1097` | `~/AppData/Local/Google/Chrome/...` | read (path probe) | safe |
| `rtk_helper.py:33–34` | `~/bin/rtk*` | read (which-equivalent) | safe |
| `provider_state.py:29` | `~/.takkub/disabled-providers.json` | write | within `~/.takkub/` |

The vault mirror at `~/WebstormProjects/second-brain` is written by `orchestrator.py`. This is **outside** the `~/.takkub/` / `~/.claude/` namespaces but is user-configured (documented in CLAUDE.md) and mirrors session logs to the user's own Obsidian vault. Not a security issue — user controls the path via `TAKKUB_VAULT_DIR` env var.

**Status: clean**

---

## Check 5 — Unexpected network calls at startup

**Verification method:** Grep `src/` for `urllib`, `requests`, `http.client`, `curl`, `wget`.

**Evidence:**

No `urllib`, `requests`, `http.client`, `curl`, or `wget` usage found in `src/agent_takkub/`. The only network activity is:
- `socket.create_connection("127.0.0.1", port)` in `cli.py:47` — loopback TCP to the orchestrator's CLI server (intentional, not startup/import-time).
- `update_helper.py` uses `subprocess.run(["git", "fetch", ...])` — git over SSH/HTTPS, user-triggered only (not at import or orchestrator init time).

**Status: clean**

---

## Summary

| # | Check | Status |
|---|---|---|
| 1 | Env leak: sensitive vars inherited by panes | **fixed** (`_build_pane_env()` allowlist, round 3: 2026-05-21 — all three spawn paths covered) |
| 2 | Auto-approve flags in pane spawns | clean (by design) |
| 3 | `shell=True` with user-controlled input | clean |
| 4 | HOME-level mutation outside namespaces | clean |
| 5 | Unexpected network calls at startup | clean |

**Result: 5/5 clean.** Check 1 fixed 2026-05-21 via round-3 hardening (claude teammate path now uses `_build_pane_env()`; all three spawn paths covered).
