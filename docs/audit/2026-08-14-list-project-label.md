# #185 — `takkub list` project name flips to worktree basename

## Symptom

After `takkub assign --role <r> --cwd <worktree-path>` a few times, the
`takkub list` header flipped from

```
▸ dev · agent-takkub   (port 56919 · ...\agent-takkub\runtime)
```

to

```
▸ dev · frontend-1786615682   (port 56919 · ...\agent-takkub\runtime)
⚠ v1.0.57 ก็รันอยู่ด้วย (port 57893) — คำสั่งนี้คุม dev · frontend-1786615682 เท่านั้น
```

`port` and `port_file.parent` stayed on the real instance's runtime dir the
whole time — only the display name changed. That's the tell: the *identity*
label and the *runtime location* are computed from two different sources
that can drift apart, and the label picked the wrong one.

## Where the name is derived (checked every call site, not just one)

`▸ {label}   (port {port} · {port_file.parent})` and the `⚠ ... ก็รันอยู่ด้วย`
line are both built in **one place**: `cli.py::_instance_banner()`
(`src/agent_takkub/cli.py:73-109`). It is computed **client-side**, fresh, in
whatever process runs `takkub list`/`takkub status` — no server round trip
for this text at all (confirmed by reading `main()`, `cli.py:2375-2381`).

Grepped `REPO_ROOT.name` / `REPO_ROOT).name` across all of `src/` — exactly
two hits, both inspected:

1. **`config.py::instance_identity_label()` (line 225-236)** — the actual
   bug. `if DATA_HOME == REPO_ROOT: return f"dev · {REPO_ROOT.name}"`.
   `REPO_ROOT = Path(__file__).resolve().parents[2]` is a **per-process**
   constant: it's wherever *this specific* `python -m agent_takkub.cli`
   invocation's `agent_takkub` package happened to resolve from (which
   `bin/takkub`/`takkub.cmd` shim got hit on PATH, which `.venv` it points
   at, cwd-sensitive `-m` resolution edge cases, etc.) — not the actual
   registered project identity. `port`/`port_file`, by contrast, are backed
   by `TAKKUB_PORT_FILE`, an env var the orchestrator **stamps explicitly**
   into every spawned pane's env (`pane_env.py:256`,
   `env["TAKKUB_PORT_FILE"] = str(config._get_port_file())`) — which is why
   they stayed correct while the label alone drifted: they're simply not
   fed from the same source.
2. **`cli.py:104`, the `⚠` line's `other_label`** (peer-instance branch,
   `is_dev == False`) — a related but *different*, pre-existing quirk: it
   labels the *other* (peer) instance using *this* instance's own
   `config.REPO_ROOT`, which isn't the reported symptom (the reported
   instance is on the `is_dev == True` branch, where `other_label` is
   `f"v{instance_display_version()}"` and never touches `REPO_ROOT.name`).
   Left alone — out of scope for #185, tracked here only so it isn't
   mistaken for the same bug in a future pass.

No other GUI surface (`main_window.py`, `project_tab.py`, remote `api.py`)
derives a *displayed* project name from `REPO_ROOT.name` or from a pane's
`--cwd` — the multi-project registry (`projects.json`, `active_project()`)
is the single source of truth everywhere else already.

## The stable signal that already exists

`TAKKUB_PROJECT` is stamped into **every** cockpit-spawned pane's env —
Lead and teammates alike, worktree-isolated or not
(`spawn_engine.py:1538`, `:1673`, `:2159` — all three spawn paths). Its
value (`project_ns`) comes from `orchestrator._resolve_project()` →
`config.active_project()`, which reads the **registered** project name out
of `projects.json` (set once, at New-Project-Wizard time) — never
recomputed from any pane's `--cwd`. That's exactly the "runtime/DATA_HOME
of the instance" anchor the issue asks for, already flowing through the
env on every pane.

Per the project's `decode_project_dir` lossiness note, this fix does **not**
attempt to decode/compare cwd paths at all — it just prefers the
already-correct, already-stamped env value over the fragile
`REPO_ROOT.name` recomputation. No `decode_project_dir`/
`encode_path_for_claude` involved because no path decoding happens here.

## Fix

`config.py::instance_identity_label()`, dev-checkout branch only:

```python
if DATA_HOME == REPO_ROOT:
    name = os.environ.get("TAKKUB_PROJECT") or REPO_ROOT.name
    return f"dev · {name}"
```

- Cockpit-spawned pane (has `TAKKUB_PROJECT`) → always shows the real,
  registered project name, regardless of which worktree cwd it was
  assigned into.
- Manual terminal invocation (`TAKKUB_PROJECT` unset — "the debugging path"
  per the existing docstring) → unchanged, falls back to `REPO_ROOT.name`
  exactly as before.
- Installed-build branch (`v{version}`) untouched — never referenced
  `REPO_ROOT.name` to begin with.

## Why this can't collapse two real projects together

Two different projects live in two different tabs, each with its own
orchestrator instance and its own `TAKKUB_PROJECT` value stamped at spawn
(`project-a` vs `project-b`) — the env var is *per-pane*, scoped by
whichever tab spawned it, never shared across tabs/instances. Covered by
`test_dev_label_distinguishes_two_real_projects` below.

## Tests (targeted; full suite is QA's batch gate)

`tests/test_config.py::TestInstanceIdentityLabel`:
- `test_dev_label_prefers_takkub_project_over_worktree_cwd_basename` — the
  #185 repro: `REPO_ROOT`/`DATA_HOME` pointed at a worktree path named
  like the reported `frontend-1786615682`, `TAKKUB_PROJECT=agent-takkub`
  set → label is `"dev · agent-takkub"`. **Confirmed RED against
  pre-fix code** (via `git stash` of just the `config.py` hunk, rerun,
  `git stash pop`) — asserted `"dev · agent-takkub"`, got
  `"dev · frontend-1786615682"` before the fix.
- `test_dev_label_falls_back_to_repo_root_name_without_takkub_project` —
  manual-terminal path unchanged.
- `test_dev_label_distinguishes_two_real_projects` — regression guard for
  the "don't merge two real projects" constraint.
- `test_dev_checkout_label` (pre-existing) — now explicitly `delenv`s
  `TAKKUB_PROJECT` so it keeps asserting the fallback path, not an
  environment leak from a neighbouring test.

`tests/test_cli.py::TestInstanceBanner`:
- `test_banner_keeps_project_name_for_pane_assigned_into_worktree` —
  end-to-end through the real (unstubbed) `config.instance_identity_label()`
  and the actual `▸ ... (port ... · ...)` banner string Lead reads.

All four new/updated tests pass against the fix; `tests/test_cli.py` +
`tests/test_config.py` full files (133 tests) pass. `ruff check` +
`ruff format --check` clean on the three touched files.

## Cross-platform / multi-provider notes

- Fix is a pure string/env-var read (`os.environ.get`) — no path
  separators, no `pathlib` involved, identical on Windows/macOS.
- `TAKKUB_PROJECT` stamping is provider-agnostic (same `_build_pane_env`/
  `_build_lead_env` path for claude/codex/gemini/opencode/kimi/cursor) —
  not a claude-only shortcut.
