# Update / Repair

Update:
- explicit user action
- compare current version against Takkub-tested range
- don't auto-upgrade every boot
- preserve prior version metadata for rollback

Repair:
- recreate managed venv
- preserve config/data

Remove:
- stop owned process
- remove managed runtime
- ask separately whether config/indexed data should also be deleted.
