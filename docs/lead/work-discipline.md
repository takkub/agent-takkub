# Task work discipline (#655512b2)

The cockpit enforces these controls for every provider:

- `takkub assign` blocks numeric business rules (for example, commission/turnover with a percentage or amount). The cockpit shows the full task and waits for the user to click confirm. CLI digest fields cannot satisfy this gate. The same user-click gate applies before reassigning a task stopped at its hard budget.
- Every assignment gets a hard time budget. Claude, Codex, and OpenCode also get a token budget based on observed provider usage samples. A cap sends Ctrl+C, records `task_work_budget_exceeded` in `runtime/events.log`, and alerts the user in the cockpit. The same task cannot be assigned again until the user approves in the cockpit's confirmation dialog.
- Quota hits use the existing bounded provider reroute flow. Its audit events are `pane_quota_rerouted` and `quota_reroute_respawn`.

Defaults by scope are tiny 30 minutes / 50k tokens, normal 120 minutes / 200k tokens, and deep 360 minutes / 500k tokens. Adjust per-scope values in `SETTINGS_HOME/work-discipline.json`:

```json
{
  "limits": {
    "tiny": {"minutes": 30, "tokens": 50000},
    "normal": {"minutes": 120, "tokens": 200000},
    "deep": {"minutes": 360, "tokens": 500000}
  },
  "lead_cached_input_tokens": 100000
}
```

The provider capability matrix is authoritative about remaining gaps. Gemini and Cursor do not expose task token totals through the current meter. Codex Lead sessions compact when cached input crosses the configured threshold; Claude currently uses its idle-time compact policy, while Gemini, OpenCode, and Cursor do not yet have cached-input-triggered Lead recovery. The matrix marks these states unsupported or partial.
