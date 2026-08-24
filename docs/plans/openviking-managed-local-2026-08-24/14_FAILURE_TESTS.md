# Failure / Rollback

Test:
- executable missing
- broken venv
- bad config
- invalid provider auth
- port occupied
- server crash/hang
- incompatible update

Expected:
- Cockpit boots
- native context continues
- clear status
- Repair works
- data/config preserved.
