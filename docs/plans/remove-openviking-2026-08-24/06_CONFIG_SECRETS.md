# Config / Secrets
Remove reads/registrations for TAKKUB_OPENVIKING_* and OPENVIKING_API_KEY, OpenViking settings/state, SecretManager backend and process env injection.
Old env vars/files must be safely ignored, not crash startup.
Preserve generic SecretManager/redaction.
