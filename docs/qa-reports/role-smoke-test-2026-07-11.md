# Role smoke test — 2026-07-11

## Scope

Tested every role registered in the local agent-takkub cockpit by assigning a
minimal acknowledgement task. The `shell` role was tested by opening its pane
and confirming that a PowerShell prompt appeared. The already-active `lead`
role was verified through `takkub list` and `takkub status`.

## Results

| Role | Result | Evidence |
|---|---|---|
| lead | PASS | Reported `active` by `takkub list` |
| frontend | PASS | Ran `takkub done "frontend OK"` |
| backend | PASS | Ran `takkub done "backend OK"` |
| mobile | PASS | Ran `takkub done "mobile OK"` |
| devops | PASS | Ran `takkub done "devops OK"` |
| gemini | PASS | Ran `takkub done "gemini OK"` |
| qa | PASS | Ran `takkub done "qa OK"` |
| reviewer | PASS | Ran `takkub done "reviewer OK"` |
| codex | PASS | Returned `ok: codex reported done` |
| critic | PASS | Ran `takkub done "critic OK"` |
| shell | PASS | Pane opened with a PowerShell prompt |
| maintainer (custom) | PASS | Ran `takkub done "maintainer OK"` |

## Operational note

Opening all roles concurrently triggered the cockpit over-capacity warning at
the ninth teammate pane (comfortable limit approximately eight panes). All
test panes exited afterward; the final `takkub list` showed only `lead` active.
For normal work, roles should be assigned in waves to avoid excess RAM and
process load.
