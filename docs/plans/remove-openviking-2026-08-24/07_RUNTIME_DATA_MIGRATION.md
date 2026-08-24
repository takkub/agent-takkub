# Existing v1.5.0 user data
Possible managed runtime under `~/.agent-takkub/services/openviking/`.

Do not silently delete user data.
Do not kill arbitrary process on port 1933.
Never kill external OpenViking.

Recommended explicit command:
`takkub cleanup openviking`
- prove Takkub ownership
- show paths/size
- explicit confirmation
- stop owned process
- remove managed venv/log/state
- optionally preserve config/knowledge data
