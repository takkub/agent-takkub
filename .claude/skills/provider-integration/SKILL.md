---
name: provider-integration
description: The checklist every CLI provider (claude/codex/gemini-agy/opencode/kimi/cursor + any new one) must satisfy in agent-takkub — spawn, prod-state isolation under DATA_HOME, Remote mirror adapter, resume picker, and the upstream-schema-drift guard that keeps a provider from going silently blank. Read BEFORE adding a provider, wiring a ProviderSpec, touching pane_env/spawn_engine provider branches, editing any *_helper.py transcript resolver, or changing remote/notify.py's scanner registry. Trigger when the user says "เพิ่ม provider", "add a provider / CLI", "provider ใหม่", or reports that one provider works while another shows nothing (blank mobile chat, empty history, empty resume picker).
---

# Adding / maintaining a provider

A provider is not "supported" because a pane spawns. It is supported when
**every row below is either done or explicitly declared as a gap**. Two
outages in 2026-08 came from rows nobody filled in — and both were silent:
the cockpit kept working, only the phone went blank.

## The 6 rows

| # | Row | Where it lives | Done when |
|---|-----|----------------|-----------|
| 1 | **Spawn** | `provider_spec.PROVIDER_REGISTRY` + the generic branch in `spawn_engine.py` | binary discovery, autonomy flags per `sys.platform`, ready markers, `context_strategy` |
| 2 | **Prod isolation** | `config._PROVIDER_HOME_SUBDIRS` → `pane_env.inject_provider_home_env` | an installed cockpit keeps this provider's sessions/auth/config **inside DATA_HOME**, or the provider is listed in `config.PROVIDER_ISOLATION_GAPS` with the reason |
| 3 | **Seeding** | `provider_bootstrap.ensure_provider_home` | first spawn under the isolated home copies auth/config + recent **Lead** sessions, bounded and atomic; never moves the original |
| 4 | **Remote mirror** | `remote/notify._HISTORY_SCANNERS` + `ProviderSpec.supports_remote_history` | `resolve_session`, `read_messages`, `live_texts`, `live_users` all return real data on a live session |
| 5 | **Resume picker** | `list_sessions` on the same scanner | Lead sessions only (`[ROLE:` / `[SESSION GOAL` filtered), preview = something a human recognises |
| 6 | **Doctor** | `doctor.check_providers` / `check_provider_isolation` | install, auth and isolation state are all reportable without reading code |

## The rule that keeps costing us: providers change their store

Both silent outages were upstream schema/layout changes, not our bugs — and
both passed CI:

* **codex 0.147** replaced `event_msg.agent_message` with
  `event_msg.item_completed` + typed `item`s. `codex exec` (what probes and
  tests use) kept writing the OLD shape, so every test stayed green while
  every TUI pane — the only thing users actually run — mirrored nothing.
* **agy (Antigravity CLI)** moved from `~/.gemini/tmp/<x>/chats/session-*.jsonl`
  to `~/.gemini/antigravity-cli/{conversations/<id>.db, brain/<id>/…/transcript.jsonl}`
  with a new record schema. The old resolver kept happily resolving a
  two-month-old file, so the mirror was "working" and empty.

So, whenever you touch a provider adapter:

1. **Verify against a live store, not a fixture.** Resolve the path and read
   messages from the user's real `~/.codex` / `~/.gemini` / opencode db and
   assert a non-zero message count. A fixture only proves the parser matches
   the fixture you wrote.
2. **Parse old AND new schemas.** Never replace a record shape — add the new
   branch beside the old one, with a comment naming the version that changed
   it. Users are spread across CLI versions, and headless modes often lag the
   TUI.
3. **Check the whole store, not the newest file.** A store that stopped being
   written still has files; "newest by mtime" will resolve them forever. Order
   candidate *stores* by preference, not by mtime.
4. **A `None`/empty result is a diagnosis, not a state.** Route it through
   `notify.lead_mirror_diagnosis` so the phone says *why* (`provider_unsupported`
   / `no_session_uuid` / `transcript_missing`) instead of showing a blank chat.

## Non-negotiables

* **Read side and spawn side must resolve the same directory.** `pane_env`
  exports the home; `*_helper.py` resolves transcripts. If they disagree the
  mirror reads a directory nothing writes to — blank phone, no error. Both go
  through `config.provider_home_env`; never re-read the env var separately.
* **Text only.** Tool arguments, terminal bytes and hidden reasoning
  (`thinking`, `Reasoning`, `CHECKPOINT`, …) never reach the phone.
* **Never mirror another project's conversation.** Every resolver matches on
  the recorded workspace/cwd before returning a path.
* **Cross-platform.** Windows + macOS both, `pathlib.Path`, no hardcoded
  separators; platform-specific branches always have the other side present.
* **Declare gaps, don't hide them.** A capability you cannot implement goes in
  a registry the doctor prints (`PROVIDER_ISOLATION_GAPS`, `supports_*` flags)
  with the reason — see CLAUDE.md's multi-provider directive (#103).

## Tests to write (minimum)

* isolation: env injection == read-side resolution, dev checkout unchanged
  (`tests/test_provider_isolation.py`)
* mirror: history + live push for the CURRENT schema, plus a regression test
  keeping the previous schema alive (`tests/test_remote_notify.py`,
  `tests/test_remote_agy_mirror.py`)
* picker: Lead-only filtering, and a preview that is not a machine-generated
  opener
* privacy: a reasoning/tool record must NOT appear in mirrored output
