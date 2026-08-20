# Boot-update policy — provider CLI auto-update moved to cockpit boot (#313)

**Status:** implemented. Companion to
`docs/audit/2026-08-20-issue-313-spawn-deadlock.md` (the root-cause proof for
the incident this policy exists to prevent) — that doc fixed the *symptom*
(a corrupted mid-write binary wedging the GIL on spawn); this one removes the
*trigger* (a provider updating itself while a pane could be spawning it) by
moving every provider's update to one bounded pass at cockpit boot, before
any pane exists.

## Three parts

**Part 1 — suppress self-update for the lifetime of a pane.** Every provider
CLI that ships its own background self-updater gets that updater disabled via
env (or, where no env knob exists, its own config file) on every spawn.

**Part 2 — update once, at boot, before any pane exists.** A splash window
gates `MainWindow` construction behind one bounded pass over every
installed+enabled provider, each updated via its own package manager, then
verified before being reported done.

**Part 3 — refresh a stale pinned model in the same boot pass** (added
2026-08-20 after user follow-up: "gemini ออก 3.7 แล้วแต่ cockpit ยังรู้จักรุ่นเก่า").
A provider whose CLI exposes an official, real-output-verified model-list
command gets its PINNED model (if any) bumped to the latest release in the
same line.

---

## Part 1 — per-pane suppression (`pane_env.inject_provider_no_autoupdate_env`)

Wired into both real spawn paths in `spawn_engine.py` (the claude branch and
the generic non-claude branch — shell panes don't spawn a provider binary, so
they're excluded).

| provider | knob | mechanism | source |
|---|---|---|---|
| claude | `DISABLE_AUTOUPDATER=1` | env | user directive (confirmed) |
| gemini (agy) | `AGY_CLI_DISABLE_AUTO_UPDATE=true` | env | [Antigravity CLI troubleshooting docs](https://antigravity.google/docs/cli/troubleshooting), "Resolve self-updater locks and failures" — verified via direct fetch of the doc page, 2026-08-20 |
| kimi | `KIMI_CLI_NO_AUTO_UPDATE=1` + `KIMI_CODE_NO_AUTO_UPDATE=1` | env (both set) | [kimi-cli FAQ](https://moonshotai.github.io/kimi-cli/en/faq.html) — verified via direct fetch, 2026-08-20. Both variables set because the same FAQ entry names the second as the current kimi-code rebrand of the identical switch; setting both costs nothing and avoids guessing which binary generation is actually installed |
| opencode | `{"autoupdate": false}` merged into `<XDG_CONFIG_HOME>/opencode/opencode.json` | **config file, not env** | [OpenCode config docs](https://opencode.ai/docs/config/) — verified via direct fetch; opencode has NO env-var override for this, only the `autoupdate` JSON key. An earlier AI-summarized web search claimed `OPENCODE_DISABLE_AUTOUPDATE=true` existed — a direct fetch of the actual doc page contradicted that (env var not mentioned anywhere), so the summarized claim was discarded rather than trusted (never guess policy) |
| codex | *(none)* | — | **GAP** — see below |
| cursor | *(none)* | — | **GAP** — see below |

### opencode's config-file write is scoped, not global

`_ensure_opencode_no_autoupdate_config` only ever touches the cockpit's OWN
isolated config home (`config.provider_home_env("opencode")["XDG_CONFIG_HOME"]`,
populated only for an installed build — see `config.py`'s
`_PROVIDER_HOME_SUBDIRS`). A **dev checkout** has no isolated home
(`provider_home_env` returns `{}` there by design, same as every other
provider-isolation gap in that module) — this is a deliberate no-op there,
never a fallback to the user's real `~/.config/opencode/opencode.json`. The
merge is read-modify-write (never clobbers other keys already in the file)
and idempotent (skips the write once `autoupdate` already reads `false`).

### GAP — codex

`openai/codex` GitHub issues [#3855](https://github.com/openai/codex/issues/3855)
and [#4375](https://github.com/openai/codex/issues/4375) both ask for exactly
this feature and are still open/unimplemented as of 2026-08-20. No
config/env surface exists in the shipped CLI to disable its own update
check. Flagged to issue #103 (comment posted 2026-08-20) — revisit once
either upstream issue ships.

### GAP — cursor

Only an **unofficial community workaround** exists (removing the exec
permission on `cursor-agent`'s own `versions` directory —
[Cursor forum thread](https://forum.cursor.com/t/how-to-disable-auto-updates/69271)).
No vendor-documented env/config knob. A filesystem-permission hack is out of
proportion for this fix and not "documented" in the sense this policy
requires (never guess/hack around an undocumented mechanism) — left as a gap,
flagged to issue #103.

Both gaps are tracked in `pane_env.NO_AUTOUPDATE_KNOB_GAPS` (source of
truth, mirrors `config.PROVIDER_ISOLATION_GAPS`'s existing "document the gap
instead of looking like an oversight" pattern) so they stay visible in code,
not just in this doc.

---

## Part 2 — boot-time update pass

### Flow

```
app.main()
  └─ _boot_main_window()
       ├─ TAKKUB_BOOT_UPDATE=0 → MainWindow() directly (old behavior, unchanged)
       └─ else → boot_update_window.run_boot_update_gate(MainWindow)
            ├─ BootUpdateWindow() constructed + shown (frameless, gold-themed splash)
            ├─ QTimer.singleShot(0, splash.start) — see "event-loop ordering bug" below
            ├─ local QEventLoop().exec() — blocks THIS function only, not app.exec()
            │    └─ splash.start():
            │         for each of the 6 PROVIDER_REGISTRY entries:
            │           - not installed OR disabled → row marked skipped, ZERO cost
            │             (provider_update.eligibility_gap is a PATH-probe + a
            │             provider_state.json read only — no subprocess, no network)
            │           - eligible → dispatched to QThreadPool as one
            │             _ProviderUpdateWorker each, running in parallel
            │         overall bounded timeout armed (TAKKUB_BOOT_UPDATE_TIMEOUT_S,
            │         default 240s) — a stuck/slow updater force-fails its row
            │         and the phase still ends
            │    └─ loop.quit() once every row is terminal (or timeout fires)
            └─ MainWindow() constructed, splash closed
       (both branches converge back into app.main()'s existing
        _install_signal_handlers / _start_deadman_watchdog / w.show() / app.exec())
```

### Eligibility (user directive 2026-08-20)

A provider is only ever probed/updated when it is **both installed AND
enabled** (`provider_state.is_disabled`). Not-installed or disabled →
instant "skipped" row, zero network/subprocess cost — `eligibility_gap()` is
the single function both the splash and `update_provider()` itself defend
with, so the "never touch a disabled provider" rule can't be bypassed by
calling the update function directly.

### Update mechanism dispatch (`provider_update._generic_update_argv`)

Dispatches on the **package manager**, not the provider's name, so a future
`PROVIDER_REGISTRY` entry gets update support for free:

- **claude** — special-cased (see below): `claude_update.py`'s existing
  `current_version`/`latest_version`/`apply_update` (`npm install -g
  @anthropic-ai/claude-code@latest`), already built for the Settings
  "⬆ Claude CLI" button and reused verbatim here.
- **npm** (codex, opencode) — `spec.install_command` re-run **verbatim**.
  `npm install -g <pkg>` with no version pin already resolves to (and
  upgrades to) the latest published version on every run — the SAME command
  already used to install the provider IS the update command.
- **uv** (kimi) — `spec.install_command` is `uv tool install`, which does
  **not** re-check for a newer version once a tool is already installed.
  `uv tool upgrade <pkg>` is uv's own documented subcommand for that,
  confirmed by running `uv tool upgrade --help` directly on this machine
  (uv 0.11.23) — not assumed from memory. The package name is
  `install_command[-1]` (works for both npm and uv shapes in this registry).
- **gemini, cursor** — no automated mechanism (`NO_UPDATE_MECHANISM_GAPS`):
  gemini/agy ships as a GUI installer download only (`install_command is
  None`); cursor's official install is a remote curl/irm script — re-running
  an arbitrary remote script unattended at every boot is out of proportion
  and has no idempotent "ensure latest" semantics the way npm/uv do. Both
  report `skipped_no_mechanism`, never attempted.

### claude's placeholder-binary post-verify

Live incident, same day (2026-08-20, 09:11): claude's npm package ships
`bin/claude.exe` as a ~500-byte **placeholder**; `postinstall` (`install.cjs`)
copies the real binary from the optional dependency
`@anthropic-ai/claude-code-win32-x64`. If that optional-dep fetch fails
mid-update, npm still exits 0 and the placeholder is left in place — every
future spawn dies. `_update_claude()` re-runs the SAME pre-flight header
check `_pty_backend.spawn_pty` already uses for issue #313
(`_looks_like_valid_executable`) against the freshly-resolved claude
executable before reporting the row `updated`; a failed check reports
`failed` with the exact recovery command (`npm i -g
@anthropic-ai/claude-code-win32-x64` then `node install.cjs`) instead of a
silently-broken "success".

### Progress: no fake percentages

None of claude/codex/opencode/kimi's update commands expose parseable
progress output (`npm install -g` and `uv tool upgrade` both print
install-log lines, not a stable `N%` token) — per the no-guessing policy,
each row shows an **indeterminate** `QProgressBar` (`setRange(0, 0)`) plus
static phase text ("กำลังอัพเดต…") while running, never a fabricated number.
Overall progress is "เสร็จ n/m ตัว" (count of terminal rows), which IS a real
number.

### Event-loop ordering bug (found and fixed during testing)

The first implementation called `splash.start()` **before**
`loop.exec()`. When there are zero eligible providers (a real state — a
fresh machine with no provider CLI installed yet, or every provider
disabled), `start()` finishes and emits `finished` **synchronously**, inside
the same call that constructs the splash. `QEventLoop.quit()` called before
the loop is actually running is a documented no-op (nothing to quit yet), so
the subsequent `loop.exec()` then blocks forever waiting for a signal that
already fired — reproduced directly in `tests/test_boot_update_window.py`'s
gate test, which hung the test process before the fix. Fixed by deferring
`start()` one tick via `QTimer.singleShot(0, splash.start)`, which guarantees
`splash.start()` — and therefore any synchronous `finished` it might emit —
only ever runs from inside an event loop that is genuinely executing.

### Cross-thread safety

`_ProviderUpdateWorker` (a `QRunnable` run by `QThreadPool`) never touches a
widget. It calls `provider_update.update_provider()` (pure Python,
subprocess-only) on the pool thread and reports back exclusively through its
`finished` Qt signal — the standard PyQt6-safe pattern (mirrors
`update_worker.UpdateCheckWorker`'s existing sibling-`QObject`-for-signals
shape). `BootUpdateWindow._on_provider_finished`/`_on_timeout` — the only
places that mutate row widgets — run on the main thread as ordinary
slot invocations.

### `_log_event` import gotcha (found and fixed during testing)

`boot_update_window.py`'s per-row logging originally did
`from .orchestrator import _log_event` (the pattern used elsewhere in
`app.py`). `orchestrator.py` transitively imports `agent_pane` →
`terminal_widget` → `QtWebEngineWidgets`, which raises
`ImportError: QtWebEngineWidgets must be imported ... before a
QCoreApplication instance is created` if a `QCoreApplication`/`QApplication`
already exists at the moment it's first imported. In the real `app.main()`
boot sequence `orchestrator` is already imported earlier (via
`_log_instance_boot()`, which runs **before** `QApplication` is constructed),
so this was accidentally safe there — but this splash's callbacks fire
**after** its own `QApplication` already exists, making that ordering an
accident this module shouldn't depend on. Reproduced directly in tests
(isolating individual test cases via `pytest -k` hit the ImportError).
Fixed by importing `_log_event` from `orchestrator_text.py` instead — the
module that actually defines it, with zero Qt imports of its own (confirmed:
importing it pulls in 96 modules, none Qt-related, vs. 231 modules including
`PyQt6.QtWebEngineWidgets` for the `orchestrator` re-export facade).

### Env overrides

- `TAKKUB_BOOT_UPDATE=0` — skip the whole splash, open exactly as before
  this feature existed.
- `TAKKUB_BOOT_UPDATE_TIMEOUT_S` — overall bounded ceiling (default `240`,
  sized for a slow-network cold pull of claude's ~330 MB optional-dep binary
  — the exact payload that made the #313 incident's race window so wide —
  with real margin). Providers update in parallel via `QThreadPool`, so this
  is ~one provider's worst case, not a sum across all six.

### Cross-platform / multi-provider

- `_looks_like_valid_executable` (reused, not reimplemented) already gates on
  `sys.platform` for the Windows-PE vs. POSIX-ELF/Mach-O/shebang check — no
  new platform-specific code added here.
- The generic-dispatch design (branch on `install_command[0]`, never on
  provider name) means a future `PROVIDER_REGISTRY` entry installed via npm
  or uv gets update support automatically; anything else lands in
  `NO_UPDATE_MECHANISM_GAPS` visibly rather than silently doing nothing.

---

## Residual risks (explicitly not fixed)

- **`uv tool upgrade`'s own update-check cost is untimed by this policy
  beyond the overall ceiling** — if uv's own resolver is slow on a given
  machine, kimi's row just eats more of the shared `TAKKUB_BOOT_UPDATE_TIMEOUT_S`
  budget; it doesn't have its own per-provider timeout distinct from that
  ceiling (deliberately — `provider_update._UPDATE_TIMEOUT_S=180` is a
  per-`subprocess.run` safety net, not a UX-tuned value).
- **codex and cursor's self-update is never suppressed while a pane runs**
  (Part 1 gap) — the residual exposure is the SAME class of race #313
  itself, just for two providers whose own CLI doesn't expose a way to turn
  it off. No cockpit-side mitigation beyond the pre-flight header check in
  `_pty_backend.py` (which already covers every provider, including these
  two, per the #313 fix).
- **gemini and cursor are never auto-updated at boot** (Part 2 gap) — stay
  on whatever version the user manually installed/updated. The doctor
  `[providers]` check already surfaces install state per provider; this
  policy doesn't add a "you're behind" nudge for these two beyond that.

---

## Part 3 — model-catalog refresh at boot (user directive 2026-08-20)

Motivating example: gemini shipped 3.7 while a pinned cockpit role config
still pointed at 3.5 — nothing in the boot-update pass so far touches model
*selection*, only CLI *binaries*. Runs in the SAME boot phase, same worker,
right after that provider's binary-update outcome (`boot_update_window._ProviderUpdateWorker.run`).

### Only touches a PIN — never invents one

`provider_model_refresh.refresh_provider_model` only acts when
`provider_models.model_for(name)` is already set (the user, or a previous
boot's bump, chose a specific model). An unpinned provider already always
rides its own CLI's default model, which is inherently fresh — there is
nothing to refresh, and this module never *sets* a pin that didn't already
exist.

### Per-provider research (real CLIs on this dev machine, 2026-08-20)

Every one of the 6 providers' actual installed `--help` output was read
before writing any code — no provider was assumed to have (or lack) a
models-list command from memory or docs alone:

| provider | `--help` result | verdict |
|---|---|---|
| gemini (agy) | `models   List available models` subcommand | **implemented** — see below |
| opencode | `opencode models [provider]   list all available models` subcommand, real output captured | **gap, deliberately not wired** — see below |
| claude | no models-list subcommand anywhere in `claude --help`'s Commands list | **gap** — has an alias-based alternative instead, see below |
| codex | no models-list subcommand anywhere in `codex --help`'s Commands list | **gap** |
| kimi | no models-list subcommand anywhere in `kimi --help`'s Commands list | **gap** |
| cursor | not installed on this machine; `cursor.com` CLI reference documents `agent models` but its real output was never captured | **gap** (mechanism confirmed, output unverified — same "not yet calibrated" caution this codebase already applies elsewhere, e.g. kimi's busy markers) |

### gemini — implemented

`agy models` was run for real (read-only, no state mutated). Actual
captured output:

```
Fetching available models...
gemini-3.7-flash-high	Gemini 3.7 Flash (High)
gemini-3.7-flash-medium	Gemini 3.7 Flash (Medium)
gemini-3.7-flash-low	Gemini 3.7 Flash (Low)
gemini-3.6-flash-high	Gemini 3.6 Flash (High)
...
gemini-3.1-pro-high	Gemini 3.1 Pro (High)
gemini-3.1-pro-low	Gemini 3.1 Pro (Low)
claude-sonnet-4-6	Claude Sonnet 4.6 (Thinking)
claude-opus-4-6-thinking	Claude Opus 4.6 (Thinking)
gpt-oss-120b-medium	GPT-OSS 120B (Medium)
```

Two properties of this REAL output drive the design, neither assumed:

1. Tab-separated `<id>\t<display name>`; the first line is a status message
   with no tab (skipped by the parser).
2. **Newest release listed BEFORE older ones within the same family**
   (3.7 → 3.6 → 3.5 for the flash-high/medium/low lines, directly observed —
   not inferred from a naming convention).

`provider_model_refresh._family_key_gemini` strips the version-number token
from a model id (`gemini-3.7-flash-medium` → `("gemini", "flash",
"medium")`) so `-high`/`-medium`/`-low` (real, distinct tiers) and `-pro`
(a different product line) are never conflated with each other, only with
their own version history. `_pick_latest_per_family` then takes the FIRST
occurrence per family in the CLI's own listing order — property 2 above,
proven not assumed — as the latest in that line. A pin whose model id
doesn't match the observed `<name>-<version>-...` shape, or whose family
isn't present in the current catalog at all (e.g. a discontinued tier), is
left untouched (`unmatched`) rather than guessed at.

When a bump does happen it goes through `provider_models.set_model()` — the
exact same persistence Settings' model dropdown uses, so it's
indistinguishable from the user picking the newer model themselves, and
Settings can still override it manually afterward (user directive: "Settings
ยัง override manual ได้เหมือนเดิม").

No pin-intent tracking exists this wave (no way to tell "user deliberately
pinned this old version to avoid a regression" from "this pin is just
stale") — per explicit user instruction (2026-08-20), every stale pin is
bumped unconditionally when this ambiguity can't be resolved. Distinguishing
intentional pins is a real, separate follow-up, not attempted here.

### claude — gap, with a stated alternative

`claude --help` has no models-list subcommand. It DOES document tier
ALIASES on `--model` ("Provide an alias for the latest model (e.g. 'fable',
'opus', or 'sonnet')... always resolves to that tier's latest release") —
an architecturally cleaner fix than discover-then-bump, since an alias never
goes stale by construction. But resolving an alias to a concrete id requires
an actual (billed) generation; the cockpit's own role-tier defaults
(`orchestrator_text._ROLE_MODEL_TIERS`) also currently hardcode
version-pinned ids in a way this feature's scope doesn't touch — migrating
those to aliases is a real, separate, larger change (many call sites) out of
proportion for this wave. Flagged to issue #103.

### codex, kimi — gap

No models-list subcommand exists in either CLI's `--help` output as
installed on this machine (2026-08-20). Flagged to issue #103.

### cursor — gap

`cursor.com`'s CLI parameter reference documents `agent models` (also
already noted in `provider_spec.py`'s own comment on `cursor_spec`), but
cursor is not installed on any machine this feature was built against — its
real output was never captured, so no parser was written against an assumed
format (same "never guess a screen you haven't seen" policy the rest of this
codebase already applies, e.g. `auth_error_markers`/`tool_running_markers`
comments in `provider_spec.py`). Flagged to issue #103.

### opencode — gap, DELIBERATELY not wired despite a confirmed, captured mechanism

`opencode models [provider]` exists and its real output was captured:

```
opencode/big-pickle
opencode/deepseek-v4-flash-free
...
anthropic/claude-opus-4-5
anthropic/claude-opus-4-6
anthropic/claude-opus-4-7
anthropic/claude-opus-4-8
anthropic/claude-opus-5
...
```

This is the ONE case where the mechanism is both confirmed AND captured but
still not implemented — for a reason distinct from every other gap above:
the list spans 75+ third-party model backends (Anthropic, OpenAI, Kimi,
local Ollama, ...) and is listed **alphabetically**, not by recency (unlike
agy's proven newest-first ordering) — there is no observed property this
module could use to determine "latest" without guessing a cross-vendor
version-comparison scheme. A WRONG bump here is qualitatively worse than
every other gap's "stays as configured" status quo: it would silently
redirect a role to a *different, unintended LLM vendor's model entirely*.
Given that asymmetric risk, this was judged not worth attempting without a
real, vendor-aware "latest" signal opencode itself doesn't expose. Flagged
to issue #103 as a candidate for a future, more careful pass.

### Splash display

A row that gets bumped shows a second line under its binary-update status,
e.g. `model: gemini-3.7-flash-medium ⬆ updated` (matches the exact format
the user asked for). Every other outcome (`no_pin`, `up_to_date`,
`unmatched`, `discovery_failed`, `gap`) shows nothing extra — only a change
worth the user's attention adds a line, per the same "don't clutter the
splash with routine status" instinct the binary-update rows already follow.

---

## Test coverage

All targeted, no real subprocess/network/spawn (every `subprocess.run` call
and every `QThreadPool.start` dispatch is monkeypatched):

- `tests/test_provider_update.py` — eligibility gating, npm/uv dispatch,
  claude special-case (up-to-date / updated / npm-failure / placeholder /
  version-check-failure), generic-provider success/failure/timeout/vanished-
  after-update, no-mechanism providers never touch subprocess.
- `tests/test_pane_env_no_autoupdate.py` — per-provider env injection,
  `setdefault` non-clobber, opencode config-file merge (write / merge /
  idempotent / corrupt-JSON recovery / dev-checkout no-op).
- `tests/test_boot_update_window.py` — per-provider row state machine,
  pending-set bookkeeping, timeout force-fail, `finished` emitted at most
  once, zero-eligible-providers instant finish, the event-loop-ordering gate
  test (`run_boot_update_gate`) proving no nested `app.exec()` and no hang.
- `tests/test_boot_main_window_gate.py` — `TAKKUB_BOOT_UPDATE=0` bypasses the
  gate; default routes through it.
- `tests/test_provider_model_refresh.py` — family-key parsing, latest-per-family
  selection, real-captured-format parsing, discovery-failure reporting,
  bump/up-to-date/unmatched outcomes, every gap provider reports a documented
  reason without touching subprocess.
