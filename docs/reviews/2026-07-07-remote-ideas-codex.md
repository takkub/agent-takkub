# Takkub Mobile Remote PWA Feature Ideas

Date: 2026-07-07  
Role: codex  
Scope: Power-user / technical features that fit the current PWA + cockpit architecture.

## Assumptions

- Mobile PWA already has project picker, pulse count, Lead read/control, and endpoints `/api/pulse`, `/api/lead` (SSE), `/api/lead/say`, `/api/lead/history`, `/api/projects`, `/api/open`.
- Cockpit already owns privileged operations through `cli_server`: `send`, `assign`, `spawn`, `close`, `list`, `status`, `restart`, `goal`, `pipeline`.
- Data-min principle currently means mobile should expose Lead-centric information by default, not raw teammate pane transcripts or full local paths.
- Control actions from mobile should be explicit, logged, and preferably routed through Lead or a narrow allowlist.

## Ranked Ideas

| Rank | Feature | What it does | Reuse / add | Effort | Security note | Feasibility |
|---:|---|---|---|---|---|---|
| 1 | Command Palette for Lead | Mobile `Cmd-K` style palette for common Lead actions: send text, set/clear goal, list panes, run pipeline, open current project, restart cockpit. | Reuse `/api/lead/say` for natural-language commands immediately. Add optional `/api/command` later as typed allowlisted actions backed by `cli_server`. | S | Control action. Start with "send command text to Lead" only; typed execution must be allowlisted and confirmed. | Doable now as Lead text UX; typed action endpoint needs design. |
| 2 | Mobile Model Switch / Model Pin | Pick model tier for the next Lead/teammate spawn: Opus, Sonnet, Haiku, Fable/default, plus effort/fallback presets. Could expose "spawn next codex as Opus" or "set teammate default to Sonnet high". | Reuse existing spawn support for `--model`, `--fallback-model`, per-role tier, and env/config concepts. Add a cockpit setting API or `/api/command` action that updates model override and optionally restarts/spawns. | M | Control action with cost/quality implications. Require confirm, show scope: current pane, next spawn, role default, or Lead fallback. Avoid arbitrary model ids initially; use presets. | Needs design for persistence/scope; implementation fits existing spawn engine. |
| 3 | Done Notifications | Push/mobile notification when any pane calls `takkub done`, with project, role, and one-line summary. | Reuse orch state/done events and `/api/pulse` or SSE. Add browser Push API or simpler in-app SSE notification queue first. | S/M | Low risk if summary only. Do not include full transcript or local path unless user opens detail. | Doable now in-app; OS-level push needs service worker/subscription work. |
| 4 | Proposal Approve / Reject from Mobile | Lead can send a proposal card to mobile; user taps Approve, Reject, or Ask follow-up. Useful for assign plans, worktree merge proposals, pipeline next step, model switch confirmations. | Reuse `/api/lead` SSE to render proposal markers and `/api/lead/say` to send approval text. Add structured proposal state later in orch state + `/api/proposals/:id/respond`. | M | High-value control action. Require nonce/id, expiry, project match, and explicit button labels. Avoid free-form hidden command execution. | MVP via text reply doable now; structured approval needs design. |
| 5 | Pane Status Board, Data-Min | Mobile board showing panes per project: role, working/idle/done, last heartbeat, model/provider badge, cwd label, worktree/isolation badge, blocked/error flag. No transcript. | Reuse `cli_server list/status`, pulse, orch state. Add `/api/panes` data-min endpoint or enrich `/api/pulse`. | M | Data-min sensitive. Show role/status/project and maybe sanitized cwd basename only; hide full command line, env, transcript, and paths by default. | Very feasible; needs careful response schema. |
| 6 | Quick Assign Templates | Mobile buttons for common orchestration prompts: "QA smoke current branch", "review diff", "spawn frontend", "run verify sequence", "ask codex to inspect failing test". | Reuse `takkub assign`, `pipeline`, role defaults. Initially send natural-language to Lead; later add `/api/assign` allowlisted endpoint. | M | Control action spawning panes. Require confirm and show role/cwd/task. In teammate panes, still Lead-only server-side gate should apply. | Doable; direct endpoint should reuse existing Lead-only semantics. |
| 7 | Lead Input Enhancements | Mobile composer with snippets, command history, multi-line mode, resend last, paste cleanup, and "send as instruction" vs "send as raw text". | Reuse `/api/lead/say` and `/api/lead/history`. Add local UI state and optional snippet storage. | S | Same security as current Lead control. Snippets should be local or project-scoped; no secrets in shared history. | Cheap win. |
| 8 | Voice Input to Lead | Dictate instructions on mobile, transcribe locally/browser-side where possible, preview text, then send to Lead. | Reuse `/api/lead/say`. Add Web Speech API integration with manual confirmation. | S/M | Control action. Never auto-send transcript; always preview/edit/confirm. Browser speech availability varies. | Feasible as progressive enhancement. |
| 9 | Project Deep Links / Open Actions | From mobile, open project in cockpit, switch active project, or open a file/URL on the desktop through `/api/open`. | Reuse `/api/projects` and `/api/open`. Add specific open intents: project, docs file, exported screenshot, artifact. | S | Control action on desktop. Restrict to known project roots/artifact directories; block arbitrary paths/URLs by default. | Cheap if `/api/open` already validates targets. |
| 10 | Pipeline Runner Lite | Mobile view of available pipeline templates and one-tap run with parameter prompts. Show current pipeline status and next hop. | Reuse `cli_server pipeline`, orch state, project picker. Add `/api/pipelines` read and `/api/pipeline/run` control endpoint. | M/L | Control action that can spawn many panes. Require confirm, estimate panes, project lock, and show template source. | Needs API/schema design but matches existing backend. |
| 11 | Worktree / Isolation Visibility | Show whether a pane is shared cwd or isolated worktree, branch/worktree basename, and merge proposal status. Helps power users see risk and parallelization shape. | Reuse worktree assign metadata and orch state. Add fields to pane status endpoint. | M | Data-min: avoid full absolute path by default. Full path only behind explicit reveal or desktop-only. | Feasible if metadata is already tracked. |
| 12 | Remote Restart / Recover Controls | Mobile "restart cockpit", "respawn Lead", "close stuck pane", or "send Enter to splash" actions with warnings. | Reuse `cli_server restart`, `close`, `spawn/status`. Add narrow `/api/recover` actions. | M | High-risk control action. Require double confirm, cooldown, and visible target. Consider local-network/session token requirement. | Technically easy, policy-heavy. |
| 13 | Focus / Watch Mode | Let user pin a pane/project to watch. Mobile home then shows Lead stream plus selected role status and done events, without full teammate transcript. | Reuse SSE + pane status. Add client-side pinned filters and optional server-side watch filter. | S/M | Low risk if status-only. Avoid streaming teammate text unless explicit future mode. | Feasible; mostly UI. |
| 14 | Mobile Goal Bar | View, set, clear, and copy the current session goal. Show "goal will be prepended to future assigns" warning. | Reuse `cli_server goal`. MVP can send `takkub goal ...` text to Lead; direct endpoint later. | S/M | Control action because it changes future task context. Require confirm for clear/overwrite. | Cheap via Lead text; direct endpoint needs small API. |
| 15 | Audit Trail / Action Log | Mobile timeline of control actions initiated remotely: say, approve, assign, model switch, restart, open. Useful for debugging "what did my phone just trigger?" | Reuse server request logs/orch events if available. Add append-only remote action log and `/api/remote/actions`. | M | Security-positive. Must avoid logging secrets from message bodies; redact or store summaries/hashes for sensitive text. | Needs small persistence/logging design. |

## Top 5 Recommended Cheap/High-Impact Builds

1. **Command Palette for Lead** - highest leverage because it can start as a thin UX over `/api/lead/say` and later grow typed actions.
2. **Done Notifications** - immediately useful for mobile remote usage; start with in-app SSE to avoid Push API complexity.
3. **Lead Input Enhancements** - cheap UI-only win that makes the current control path much better.
4. **Pane Status Board, Data-Min** - power-user visibility without transcript exposure; likely the best technical dashboard primitive.
5. **Mobile Model Switch / Model Pin** - strong differentiator; needs careful scope/confirm design but architecture already supports model pins at spawn.

## Suggested Implementation Order

1. Ship UI-only improvements first: Lead composer snippets/history, command palette that sends text, in-app done toasts.
2. Add one data-min read endpoint: `/api/panes` or enriched `/api/pulse` with role/status/project/model/worktree flags.
3. Add structured control primitives one at a time behind confirmations: goal, model override, approve/reject, assign template.
4. Add audit logging before enabling destructive or broad actions such as restart, close pane, pipeline run, or direct open.

## Security / Data-Min Guardrails

- Treat every mobile write as a control action unless it is purely local UI state.
- Prefer preset action types over arbitrary shell/CLI strings.
- Include project id, target role, action type, user-visible summary, and confirmation state in every control request.
- Default pane visibility to status metadata only. Do not expose teammate transcripts, env vars, command lines, full paths, or raw logs.
- For `open`, `restart`, `close`, `pipeline run`, `assign`, and `model switch`, require explicit confirmation and server-side allowlists.
- Keep a remote action audit log with redaction for message bodies and secrets.
