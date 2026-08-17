# #273 — task-pointer delivery fails on file-read-less panes + boot-stall hides the real provider error

Two problems from the same saas_admin incident (2026-08-16), fixed independently.

## 1) `_task_handoff_pointer` breaks a pane with no file-read tool

### Symptom

`Lead` assigned a long task to `frontend` (codex-backed). Cockpit's
`_task_handoff_pointer` (issue #1) wrote it to a handoff file and pasted a
short pointer instead: *"อ่าน task spec เต็มจากไฟล์: ... เปิดอ่านไฟล์นี้ด้วย
เครื่องมืออ่านไฟล์ของคุณ (file-read tool) — ห้ามรัน path เป็นคำสั่ง shell
(#104)"*. The pane replied instantly:

```
[frontend FAILED] ไม่มี file-read tool ใน pane นี้ จึงเปิด task spec ตาม
ข้อห้าม shell ไม่ได้
```

The task never started — one whole spawn burned on a delivery mechanism
the pane couldn't act on. Worse, the `FAILED` report triggered the same
fix-loop-propose flow a genuine work failure gets, sending Lead to hunt for
a "root cause" in work that was never attempted.

Root cause: codex's tool set is shell/apply_patch only — it has no
structured file-read tool distinct from the shell exec the pointer itself
forbids using on the path (#104's convention). The pointer's instruction
("read this file yourself, but don't shell out to read it") is an
unsatisfiable pair of constraints for that CLI. This was flagged as a
latent risk in the original #1 design review
(`docs/reviews/2026-07-10-roadmap-audit-codex.md`: *"downstream agents
still need a real file-read path... provider-specific tools may differ"*)
but never confirmed or acted on until this incident.

### Fix, part A — never hand codex an unusable pointer

`ProviderSpec` gained `supports_agent_file_read: bool = True` (default —
most agent CLIs, including claude, have one). Set `False` on `codex_spec`
with the incident cited as proof (never guessed — see the module's own
"never guess a marker from docs alone" precedent for auth markers).

`_task_handoff_pointer(task, project_ns, role_name, *, supports_file_read:
bool = True)` — new keyword-only param. `False` skips the pointer
unconditionally regardless of task length, returning `(task, None)`: a
plain, full inline paste, exactly the pre-#1 behavior. `_assign_dispatch`
passes `PROVIDER_REGISTRY[effective_provider].supports_agent_file_read`.

No chunking was added (`ponytail`: the issue's own proposal says chunking
"may" be needed, not that it is — `ProviderSpec.enter_delay_per_kb_ms`
already scales the PTY paste delay by content size specifically to absorb
KB-scale pastes, so this is a return to a previously-working delivery
path, not a new risk. Upgrade path: chunked delivery, if a provider
without file-read AND with genuine paste-swallow trouble ever surfaces).

### Fix, part B — a delivery failure must not look like a task failure

Even with part A closing the known gap, the issue explicitly asked for a
belt-and-suspenders net (a future provider, a mis-set capability flag, ...
could hit an equivalent wall). `orchestrator_text.is_delivery_pointer_failure
(note, task_file, elapsed_sec)` classifies a `done --fail` report as a
DELIVERY failure (not a work failure) when BOTH:

- **structural**: this assign actually used the pointer (`task_file` is
  not `None`) AND the report landed within `DELIVERY_POINTER_FAILURE_WINDOW_SEC`
  (120s) of the assign — real work, successful or not, essentially never
  completes that fast;
- **textual**: the note echoes the pointer's own wording ("file-read
  tool" / "เครื่องมืออ่านไฟล์") — provider-agnostic on purpose, since it
  matches what COCKPIT ITSELF said in the pointer, not any one CLI's own
  error phrasing.

Both signals are required so a genuinely fast real failure is never
misclassified. `Orchestrator.done()` routes a match to the new
`_build_delivery_pointer_failure_notice` (states plainly this is a
delivery failure, no root cause to hunt, just reassign) instead of
`_build_verify_fail_handoff`'s fix-loop-propose wording, and — critically —
skips the `role_memory.append_failure_entry` capture, so this class of
report never poisons the role's future-spawn "past mistakes" context with
a failure that was never about anything the pane did.

## 2) Boot-stall notice is generic even when the provider can say exactly what's wrong

### Symptom

Cockpit repeated *"[delivery-boot-stall] pane ค้างอยู่ที่ boot phase
(กำลังโหลด MCP server)"* dozens of times over the session with no further
detail, while `codex mcp list` answered directly and instantly:

```
Error: failed to load bootstrap configuration
Caused by: invalid transport in ...
```

~40 minutes were spent manually guessing which config file was wrong.
(Side note surfaced by the same session, worth recording so it isn't
mis-attributed later: part of the later boot-stall run was Lead's own
doing — an `[mcp_servers.context7]` block with `startup_timeout_sec` but no
`transport` broke the WHOLE config.toml; removing it let codex boot
normally. The EARLIER boot-stalls, before that edit, are real and still
unexplained — this fix is about giving Lead the tool to diagnose either
case in one shot instead of guessing.)

Lead separately flagged a concrete lead worth checking:
`mcp_bridge.py:83`'s `_CODEX_SERVER_KEYS = ("command", "args", "env")`
only forwards stdio-shaped MCP server config, while the shared MCP config
can contain `http`-type servers (e.g. `pms`) that have no `command` key at
all — those get forwarded as an entry with no working transport, which
would read as exactly this "invalid transport" error. This was **not**
independently re-verified as part of this fix (out of scope — a boot-stall
diagnostic mechanism is useful regardless of which specific config path
produces the error); it's exactly the kind of thing the new diagnostic
follow-up below would have surfaced immediately instead of needing 40
minutes of guessing.

### Fix

`ProviderSpec.boot_diagnostic_argv: tuple[str, ...] | None = None` — argv
(relative to the discovered binary) for a confirmed-safe, read-only,
one-shot health-check command. Set `("mcp", "list")` for codex (confirmed
via `codex --help`: read-only, lists configured servers — safe to run
unconditionally). `None` for every other provider — never guessed from
docs alone, same discipline the auth-marker fields already document.

`LeadInboxMixin._warn_lead_delivery_boot_stall` (the existing #254 notice —
**unchanged**, still fires first, still generic, so #254/#271 do not
regress) now ALSO fires `_run_boot_diagnostic_async` as a follow-up. Design
constraints:

- **Never blocks the Qt main thread.** Runs via `QProcess`, mirroring
  `Orchestrator._check_uncommitted_async`'s existing async-subprocess
  pattern — not a new idiom in this codebase. A prior instinct to use a
  short blocking `subprocess.run(timeout=5)` was rejected: this codebase
  has a documented incident (#133) where multi-second Qt-main-thread
  stalls caused cascading QTimer misbehavior; a one-time few-second block
  is not a risk worth taking when the async pattern already exists.
- **Best-effort, additive only.** No argv confirmed / binary not found /
  process errors or times out (8s) / clean exit (0) → silently no-op. The
  original boot-stall notice was ALREADY sent unchanged before this method
  is even called — a missing or failed diagnostic changes nothing about
  it. Only a non-zero exit with real output produces a follow-up notice
  (`🔎 [boot-diagnostic] ...`).
- **Decision logic extracted as a pure staticmethod**
  (`_boot_diagnostic_notice_text`), mirroring `_uncommitted_warning` next
  to `_check_uncommitted_async` — unit-testable without spawning a
  process.

### A real regression caught and fixed during this work

The first version of this fix did NOT guard `tests/test_delivery_boot_stall_notice.py`
against the new diagnostic follow-up. That file's fixture uses role name
`"codex"` (a FORCED-provider role — `provider_config._FORCED_PROVIDER`
always resolves it to codex, unaffected by config). On a machine with a
real `codex` binary on PATH (this dev machine), the unpatched test would
have started a REAL `codex mcp list` subprocess during the unit test
suite — confirmed live: running the combined targeted battery produced a
silent `exit 127` (the exact PyQt6-abort-in-a-QTimer-slot signature this
project's CLAUDE.md already documents as a known full-suite-only failure
mode). Fixed by adding `monkeypatch.setattr(o, "_run_boot_diagnostic_async",
MagicMock())` to that file's `orch` fixture — it tests the notice text
only, not the diagnostic follow-up (which now has its own fully-mocked
test file, `test_boot_diagnostic.py`, that never spawns a real process).

## Tests (targeted, not full suite)

- `tests/test_task_handoff.py` — `supports_file_read=False` always pastes
  inline regardless of length; default/explicit `True` unchanged;
  `_assign_dispatch` integration proves a codex-mapped role's long task is
  never pointer-ized (`last_assigned_task_file is None`, full text pasted).
- `tests/test_delivery_pointer_failure.py` (new) — `is_delivery_pointer_failure`
  pure-function cases (match, no task_file, outside window, wrong wording,
  English echo, empty note) + `Orchestrator.done()` integration (delivery
  failure gets the dedicated notice and skips `role_memory` capture; a
  genuine fast failure without a task_file still gets the normal fix-loop
  wording and IS captured).
- `tests/test_boot_diagnostic.py` (new) — `_boot_diagnostic_notice_text`
  pure cases (nonzero+output reports, clean exit silent, nonzero+empty
  silent, truncation); `_run_boot_diagnostic_async` guard conditions (no
  argv / no binary / discovery exception never construct a `QProcess`;
  confirmed argv+binary does); `codex_spec.boot_diagnostic_argv ==
  ("mcp", "list")`, `claude_spec.boot_diagnostic_argv is None`.
- `tests/test_delivery_boot_stall_notice.py` — regression-guarded (see
  above); all 7 pre-existing cases still pass with the diagnostic
  follow-up mocked out.
- `tests/test_orchestrator_auto_respawn_replay.py::test_assign_rewrites_codex_task_with_override_notice`
  — updated: this test's own long codex task previously exercised the
  NOW-REMOVED pointer path for codex; assertions changed to confirm the
  full rewritten text pastes inline (`sent_task == cached`,
  `last_assigned_task_file is None`) instead of pointer-izing.

Full combined targeted battery re-run green after all changes (687 tests):
`test_provider_config.py test_cli.py test_cli_server.py test_subagent_mode.py
test_fanout_queue.py test_resource_governor.py test_spawn_gate.py
test_spawn_task_delivery.py test_worktree_assign.py test_spawn_codex_argv.py
test_orchestrator_shard.py test_qa_plan_fanout.py
test_orchestrator_auto_respawn_replay.py test_orchestrator_stall.py
test_spawn_queue_stuck.py test_spawn_queue_health.py test_lead_wait.py
test_task_handoff.py test_verify_fail_hint.py test_role_memory.py
test_delivery_pointer_failure.py test_delivery_boot_stall_notice.py
test_done_note_symmetrize.py test_boot_diagnostic.py`.

## Multi-provider / cross-platform

Both fixes are entirely `ProviderSpec`-driven — `supports_agent_file_read`
and `boot_diagnostic_argv` are per-provider fields consulted generically
(`PROVIDER_REGISTRY[effective_provider]`), so a future provider is
opt-in-only and never silently assumed to have either capability (both
default to the SAFE assumption: file-read `True` since that's the common
case, diagnostic `None` since a wrong guessed command is worse than none).
No platform-specific code touched — `QProcess`/binary discovery already go
through the existing cross-platform `custom_discovery_fn` machinery.
