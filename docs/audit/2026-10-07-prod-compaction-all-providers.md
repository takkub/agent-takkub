# Production compaction audit — 2026-10-07

## Follow-up: exact v2.2.3 installation window

The production migration journal records `app -> 2.2.3` at **2026-10-03 12:07:34 Bangkok time**. The next app marker is `app -> 2.2.5` at **2026-10-07 09:02:00**. The boot updater explicitly reports **2.2.3 → 2.2.5 at 09:00:42** on October 7. There is no intervening 2.2.4 marker: production skipped that version, as the user recalled.

Two compactions **completed successfully after the v2.2.3 marker**:

| Project / role / provider | Takkub sent command (Bangkok) | Provider confirmed completion | Context before → after |
|---|---|---|---|
| `saas_admin_amb` / Lead / Claude | Oct 3, 13:03:16 | Oct 3, 13:04:09 | 189,903 → 5,794 tokens |
| `unirecon` / Lead / Claude | Oct 6, 01:09:18 | Oct 6, 01:10:09 | 175,270 → 9,075 tokens |

Each has three matching evidence records: a `proactive_idle_compact` event, `session_report` with `source=compact`, and a Claude JSONL `compact_boundary` with before/after token counts. Claude labels these as `trigger=manual` because Takkub sends the CLI's `/compact` command; this does not mean the user typed it manually.

Exact files: `.agent-takkub/claude-config/projects/takkub-project-saas_admin_amb/0b6b095f-4805-450d-96c9-4d1e8acd2c16.jsonl` and `takkub-project-unirecon/7a6f87e2-dd95-4461-a839-0a1bcb1b94a8.jsonl`. Provider timestamps are UTC; the table converts them to Bangkok time.

Between the v2.2.3 marker and the Oct 7 update, production logged 101 idle-clock resets, 46 skips (26 specialist-working, 13 unread-inbox, 6 waiting-Lead, 1 nothing-new), and two compact dispatches. Provider spawns were Claude 67, Codex 12, and Gemini 7; none for OpenCode or Cursor.

### Codex: supported command versus observed execution

Official OpenAI documentation confirms `/compact` and automatic history compaction at `model_auto_compact_token_limit` (model defaults when unset): <https://learn.chatgpt.com/docs/developer-commands?surface=cli> and <https://learn.chatgpt.com/docs/config-file/config-reference>.

Takkub v2.2.3 already contained the Codex Lead watchdog branch. It sends `/compact` only when the Lead is ready, cached input reaches the configured threshold (default 100,000), a full threshold has accumulated above the last marker, no specialist/wait/inbox work remains, and no composer draft is detected. A targeted existing regression test of this dispatch branch passed during this audit. That test uses a fake session and proves command dispatch logic, not successful provider compaction in production.

No `lead_cached_context_compacted` event was found in the retained production logs, and no `compacted` / `context_compacted` record was found in the nine unique retained Codex sessions from the relevant October 5 activity (the other account-home tree duplicates those files). One Codex Lead session reached 141,184 cached tokens; therefore reaching 100,000 alone did not result in a recorded compact. The precise blocking gate is not retained in the current diagnostics. Native usage records reported a context window of 258,400; configuration inspected did not override `model_auto_compact_token_limit`. The exact native automatic threshold for the historical model was not established.

The finding is **Claude compact did work twice during v2.2.3; Codex supports compact, but successful automatic compact in these production sessions remains unverified**.

An additional scan of older production Codex rollouts found actual `compacted` records for `saas_admin_amb` on **2026-09-29 at 11:34:42 and 14:52:28 Bangkok time** (Codex CLI 0.158.0). These prove that Codex compaction has successfully executed in this production environment, before the v2.2.3 window. The retained records inspected do not establish whether these older compactions were initiated by a user command, Takkub, or the provider's automatic threshold. This does not establish successful Codex watchdog compaction after v2.2.3.

Exact older files under `.agent-takkub/codex-home/sessions/2026/09/29`: `rollout-2026-09-29T11-21-44-01a0eb65-adb2-7031-97a0-9c8b9a72d8bc.jsonl` and `rollout-2026-09-29T13-50-44-01a0ebee-1614-7532-ba3a-e36e86748a5b.jsonl`.

## Verified production instance

- Read-only IPC `instance-identity` returned version **2.2.5**, PID **19136**, port **63179**, data home `C:\Users\monch\.agent-takkub`.
- Inspected installed files under `.agent-takkub/venv/Lib/site-packages/agent_takkub`, production configuration, production events, and provider transcripts. No application restart, forced compact, provider switch, or production configuration change was performed.
- The installed registry contains five providers: `claude`, `codex`, `gemini` (AGY/Antigravity), `opencode`, and `cursor`.

## Behavior implemented in production

| Provider | Takkub compact trigger | Coverage |
|---|---|---|
| Claude | Continuous idle at the ready prompt for 55 minutes by default; 4 minutes when cached usage reports overage | Lead and teammates, subject to work/inbox/wait/draft gates |
| Codex | `current_usage()['cache_read']` reaches the configured threshold; default 100,000. Another compact requires at least another threshold above the previous marker | Lead only; still requires readiness, no orchestration work, and no draft |
| Gemini / AGY | None | Exits the watchdog's provider branch without sending compact |
| OpenCode | None | Exits the watchdog's provider branch without sending compact |
| Cursor | None | Exits the watchdog's provider branch without sending compact |

Sources in installed `orchestrator.py`: `_check_proactive_compact` at line 14756, provider branching near line 14979, Codex dispatch near line 15006, Claude draft guard near line 15089. `provider_spec.py` marks `lead_context_recovery` as partial for Claude, supported for Codex, and unsupported for the other three providers (near line 1672).

The 100,000 cached-input policy is **not a shared trigger for every provider**. It does not trigger Claude compact, Gemini compact, or Codex teammate compact.

`TEAMMATE_AUTOCOMPACT_TOKENS` is `None`: Takkub does not pass an early `--autocompact` window by default. Source comments document that the previous early window was reverted because it interrupted work. This is independent of provider-native compaction; the absence of a Takkub trigger does not prove that a provider never compacts internally.

## Observations today

- Production `events.log` plus `events.log.old`, filtered to 2026-10-07, contained **zero** `proactive_idle_compact` and **zero** `lead_cached_context_compacted` events when inspected.
- They contained **34** `proactive_idle_compact_clock_reset` events and **6** `proactive_idle_compact_skipped` events. Logged skip reasons were `lead-specialist-working` and `waiting-lead`.
- `mcp_handshake_argv` events showed **20 Claude spawns** and **2 Gemini spawns** today. No Codex, OpenCode, or Cursor spawn appeared in today's production records inspected. Their entries above are installed-code findings, not claims of live reproduction.
- `unirecon` Lead was spawned with **Gemini at 13:38:55**, followed by a Gemini backend at 13:52:06. Current routing also selects Gemini for that project's roles. Its Lead was executing commands during inspection; the watchdog has no Gemini compact trigger even after it becomes idle.
- Live IPC status reported Claude Leads in `ai-vdo` and `wash-locker` as ready. Their latest Claude assistant records had prompt components of approximately **209,896** and **104,666** tokens respectively. Neither session contained a `compact_boundary` record. These sizes exceed the Codex cached-input threshold, but that policy does not apply to Claude.
- Across **28 Claude JSONL files updated today**, no `compact_boundary` record was found at inspection time.
- Raw PTY tails for the two ready Claude Leads showed text in the input area. Drafts are a possible compact blocker; the public status response does not expose the live compact clock or draft verdict. Offline screen reconstruction was sensitive to unknown terminal dimensions, so it was **not used to claim a definitive per-pane draft verdict**.
- The two most recently modified AGY provider transcripts contained ordinary user/planner/tool/system-message records. No dedicated record whose type indicated compaction/compression was found. That limited schema check cannot exclude internal provider compaction.

## Diagnosis and remaining limits

The main confirmed coverage gap is that compact policy is provider-specific: Claude uses idle time, Codex Lead uses cached input, and Gemini/AGY, OpenCode, and Cursor have no Takkub trigger. The currently active `unirecon` Gemini Lead falls into this gap.

Claude's logged activity resets its idle clock and orchestration gates defer compaction. Exact blockers for all currently ready Claude panes remain unconfirmed because several watchdog branches are silent: draft present, not yet at threshold, already compacted marker, and readiness/background-work branches do not all emit a current decision snapshot. A ready status by itself does not prove that compact gates have passed.

A future implementation should define explicit commands/capabilities per provider and expose the compact decision and blocker in diagnostics. No universal command was guessed or injected during this investigation.

## Graph lookup

The existing graphify graph covers `src/agent_takkub/settings_management`, not the compaction engine. A graph query did not provide compaction evidence; conclusions above come from production code and logs.
