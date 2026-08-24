# Rollback / Failure Drill

Run in dev/disposable instance.

1. OpenViking enabled -> disabled
2. Stop OpenViking server during use
3. Break optional design integration credentials
4. Disable Figma/21st/Penpot
5. Close/reopen Editor and Preview
6. restart Cockpit

Expected:
- native Brain/Conversation/Graft still work
- Cockpit boots
- assignment continues
- doctor reports optional failure clearly
- no destructive data migration required

This is NOT the Phase 10/V2 rollback procedure.
