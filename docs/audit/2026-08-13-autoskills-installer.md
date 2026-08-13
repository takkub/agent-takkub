# autoskills installer bridge — 2026-08-13

New module: `src/agent_takkub/autoskills_installer.py`. Bridges the
[autoskills](https://www.autoskills.sh) CLI (scans a project's
`package.json`/config, guesses tech stack, fetches matching skills from the
`skills.sh` registry into `.claude/skills/<name>/`) into the cockpit.

## Why two functions, not one

A skill written to `.claude/skills/` is a prompt every pane in the project
auto-loads — installing one is equivalent to importing external content
straight into the team's shared context. So the API is split so the UI
*must* show the user what would land before anything is written:

- **`preview(project_root, timeout=60.0) -> PreviewResult`** — runs
  `autoskills --dry-run --agent claude-code` only. Writes nothing, ever.
  Safe to call speculatively/repeatedly.
- **`install(project_root, selected_names, timeout=120.0) -> InstallResult`**
  — the ONLY function that writes files. **Must be called only after the
  user has explicitly confirmed a selection in the UI.** Never call it
  automatically or as a side effect of `preview()`.

Both are synchronous, blocking calls (subprocess + wait, bounded by
`timeout`) — the docstrings on both functions say explicitly: call from a
worker thread, never the Qt main thread. Threading itself is the UI layer's
job; this module doesn't spawn one.

## API

```python
@dataclass(frozen=True)
class SkillCandidate:
    name: str
    source: str = ""          # registry URL, "" if the CLI output didn't include one

@dataclass(frozen=True)
class PreviewResult:
    ok: bool
    stack: list[str]          # detected tech stack, best-effort parsed
    skills: list[SkillCandidate]
    raw_output: str           # full stdout+stderr, always kept for debugging
    error: str = ""

@dataclass(frozen=True)
class InstallResult:
    ok: bool
    written: list[str]        # names actually kept on disk (== selected_names ∩ what the CLI wrote)
    skipped: list[str]        # names the CLI wrote but the user did NOT select (deleted again)
    raw_output: str
    error: str = ""

def preview(project_root: str | Path, timeout: float = 60.0) -> PreviewResult: ...
def install(project_root: str | Path, selected_names: Iterable[str], timeout: float = 120.0) -> InstallResult: ...
```

## CLI resolution (cross-platform)

`_resolve_autoskills_cmd()`:
1. A direct `autoskills` (or `autoskills.cmd` on Windows, checked first —
   `subprocess.run(shell=False)` can't reliably launch a bare `.cmd` name via
   `CreateProcess`) on `PATH` wins.
2. Otherwise falls back to `npx --yes autoskills@latest`, resolving
   `npx`/`npx.cmd` the same way.
3. Neither found → both `preview()` and `install()` return `ok=False` with a
   readable Thai error (`ไม่พบ autoskills และไม่พบ npx บนเครื่องนี้ — ติดตั้ง
   Node.js ก่อน`). Never raises, never hangs.

All resolution goes through `shutil.which` — no hardcoded paths, so this
works unmodified on Windows and macOS.

## Safety properties

- **No silent auto-install.** `install()` writes files; its docstring says
  explicitly it must be gated behind user confirmation. Enforcement is a
  docstring contract, same as every other install-side function in this
  codebase (`plugin_installer.install_plugin`, etc.) — there is no
  code-level lock, because the "confirm first" boundary is a UI-flow
  concern, not something a backend function can verify on its own.
- **Selective install via diff, not a CLI flag.** `autoskills` documents no
  per-skill filter flag. `install()` snapshots `.claude/skills/` entry names
  before running the CLI with `--yes --agent claude-code`, diffs after, then
  deletes any newly-written entry NOT in `selected_names`. Net effect on
  disk == exactly the user's selection. A pre-existing skill directory
  (something already there before the call) is never touched either way.
- **Path-escape guard.** Every newly-written entry is resolved
  (`Path.resolve()`) and checked against the resolved `.claude/skills/` dir
  (`_escaped_entries`). If the CLI ever wrote a symlink/junction pointing
  outside that directory, **all** newly-written entries are rolled back and
  `install()` returns `ok=False` with the offending names listed — nothing
  partial is left behind.
- **No missing-runtime crash.** Both entry points check
  `_resolve_autoskills_cmd()` first and return a structured error instead of
  raising when neither `autoskills` nor `npx` is on `PATH`.
- **Bounded, non-blocking subprocess calls.** Every invocation goes through
  `_run()`: `timeout=` enforced, `stdin=subprocess.DEVNULL` (a stray
  interactive prompt fails fast via `TimeoutExpired` instead of hanging
  forever), `creationflags=SUBPROCESS_NO_WINDOW` (no console flash on
  Windows), and `npm_config_yes=true` / `GIT_TERMINAL_PROMPT=0` set via
  `setdefault` — the same two blocking-prompt-prevention vars
  `pane_env._apply_non_interactive_env` sets for panes, applied here since
  the `npx` fallback path may need to fetch the package on first run.

## Known limitation: output parsing is best-effort

`autoskills` has no documented `--json` output mode. `_parse_preview_output`
does a heuristic text parse (header-block splitting + bulleted-line
extraction, with a URL-carrying-bullet fallback when no headers match) to
fill in `PreviewResult.stack` / `.skills`. This was written against a
plausible sample format, **not** verified against the real CLI's actual
output (no network access in this environment to invoke the real registry).
`raw_output` is always populated regardless of parse success, specifically
so the UI has a fallback to show the user the literal CLI output if the
parsed fields look wrong or come back empty. If/when the real CLI's exact
output format is confirmed, tighten `_parse_preview_output` accordingly.

## Tests

`tests/test_autoskills_installer.py` — 24 tests (1 skips gracefully on a
machine where creating symlinks isn't permitted; everything else always
runs), all subprocess calls mocked, no network:

- CLI resolution (direct binary / npx fallback / Windows `.cmd` shim
  priority / nothing available).
- `preview()`: exact argv (`--dry-run --agent claude-code`, confirms `--yes`
  is NOT passed), non-interactive env vars, timeout/OSError/non-zero-exit
  handling, output parsing (stack + skills + source URLs), empty-output
  case.
- `install()`: empty selection short-circuits without calling the CLI,
  missing-runtime error, exact argv (`--yes --agent claude-code`),
  selective-write-and-cleanup behavior (verified via real tmp-dir
  filesystem state, not just mocks), timeout/OSError/non-zero-exit
  handling, path-escape rollback (via a real symlink where the OS permits
  it; the guard function is also exercised directly).
- `_parse_preview_output` edge cases (no headers, empty string).

Run: `pytest tests/test_autoskills_installer.py -q` — 24 passed / 1 skipped
locally.

## Not done (out of scope for this task)

No UI wiring (New Project Wizard step, confirm dialog, worker-thread
plumbing) — this task was the backend bridge module only.
