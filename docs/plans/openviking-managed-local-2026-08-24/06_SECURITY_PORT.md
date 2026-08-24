# Security / Port

Managed server binds `127.0.0.1` only.
Never default to `0.0.0.0`.
No tunnel.

Port:
- prefer 1933
- if occupied, detect whether healthy OpenViking owns it
- otherwise choose free loopback port
- persist runtime URL

Secrets through SecretManager / service state with redaction.
Keep strict project-scope checks already present.
