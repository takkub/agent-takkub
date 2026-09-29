# Task work discipline (#655512b2)

The cockpit enforces these controls for every provider:

- `takkub assign` blocks numeric business rules (for example, commission/turnover with a percentage or amount). The cockpit shows the full task and waits for the user to click confirm. CLI digest fields cannot satisfy this gate. The same user-click gate applies before reassigning a task stopped at its hard budget.
- Every assignment gets a hard time budget. Claude, Codex, and OpenCode also get a token budget based on observed provider usage samples. A cap sends Ctrl+C, records `task_work_budget_exceeded` in `runtime/events.log`, and alerts the user in the cockpit. The same task cannot be assigned again until the user approves in the cockpit's confirmation dialog.
- Quota hits use the existing bounded provider reroute flow. Its audit events are `pane_quota_rerouted` and `quota_reroute_respawn`.

The token budget counts fresh input, cache creation, and output from completed turns. It excludes cache reads, which replay earlier context and can otherwise make normal work appear many times larger. The meter's latest-turn/context snapshot is never added repeatedly; assignment records a baseline and later turns are counted once. A resumed session's pre-assignment usage is excluded. Gemini and Cursor have no confirmed token schema, and Kimi/other providers without a meter adapter are unsupported, so they receive the time limit only.

Defaults by scope are tiny 30 minutes / 100k tokens, normal 120 minutes / 1M tokens, and deep 360 minutes / 5M tokens. These leave room for normal work while retaining a hard stop well below runaway usage such as 77.5M tokens. Adjust per-scope values in the `work-discipline.json` file under the cockpit's SETTINGS_HOME directory:

```json
{
  "limits": {
    "tiny": {"minutes": 30, "tokens": 100000},
    "normal": {"minutes": 120, "tokens": 1000000},
    "deep": {"minutes": 360, "tokens": 5000000}
  },
  "lead_cached_input_tokens": 100000
}
```

The provider capability matrix is authoritative about remaining gaps. Gemini and Cursor do not expose task token totals through the current meter. Codex Lead sessions compact when cached input crosses the configured threshold; Claude currently uses its idle-time compact policy, while Gemini, OpenCode, and Cursor do not yet have cached-input-triggered Lead recovery. The matrix marks these states unsupported or partial.
