# #349 — enabling the Windows Resource-Exhaustion-Detector log

`takkub qa-gate` now sets `PYTHONFAULTHANDLER=1` and samples memory every 30s
during each step (see `qa_gate._sample_memory_line`) — both are on by default
and need nothing from the machine. Neither can prove an OOM kill though: the
memory samples only show what was available *between* 30s snapshots, and
Windows itself doesn't report "a process was killed for memory pressure"
anywhere the gate can read it unless this Event Tracing channel is turned on
first.

This channel is **disabled by default** on Windows and enabling it is a
system-config change (not something `qa-gate` or any test should do for you)
— run these yourself, once, on the machine that keeps hitting #349:

## Enable the channel

PowerShell, as Administrator:

```powershell
wevtutil set-log "Microsoft-Windows-Resource-Exhaustion-Detector/Operational" /enabled:true
```

Verify it took:

```powershell
wevtutil get-log "Microsoft-Windows-Resource-Exhaustion-Detector/Operational"
```

## After the next #349 occurrence

Read the channel for events around the time the gate died (cross-reference
against the `pytest-memory.log` written into that run's
`runtime/exports/qa-gate-<timestamp>/` — the last sample before the process
went silent is the most useful anchor):

```powershell
Get-WinEvent -LogName "Microsoft-Windows-Resource-Exhaustion-Detector/Operational" |
  Where-Object { $_.TimeCreated -gt (Get-Date).AddHours(-2) } |
  Format-List TimeCreated, Id, Message
```

Event ID 2004 ("low virtual memory" resolution — which process got trimmed
or killed) is the one that would actually confirm or rule out the OOM
suspect from #349. No event in that window during the next occurrence rules
OOM out cleanly, same as this run's memory-sample log ruling it in or out on
its own — the two together turn "unprovable" into a real answer either way.

## Turning it back off (optional)

```powershell
wevtutil set-log "Microsoft-Windows-Resource-Exhaustion-Detector/Operational" /enabled:false
```

This channel is low-volume (only writes on real trim/kill events) — there is
no real cost to leaving it enabled indefinitely on a dev machine that runs
the full gate regularly.
