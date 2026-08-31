#!/usr/bin/env bash
# Copy claude/codex login credentials into the takkub-sim sandbox's
# bind-mounted volumes/auth/ (never the container's provider config
# directly — see docs/guides/2026-08-31-docker-sandbox.md). Run from the
# repo root, before or after `docker compose --profile sim up -d`.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

mkdir -p volumes/auth/claude volumes/auth/codex

# claude: on macOS/Linux the OAuth token is a plain file
# (~/.claude/.credentials.json); on Windows it lives in Credential Manager
# with no file to copy — see doctor.py's platform-gated auth check.
CLAUDE_CREDS="$HOME/.claude/.credentials.json"
if [ -f "$CLAUDE_CREDS" ]; then
    cp "$CLAUDE_CREDS" volumes/auth/claude/.credentials.json
    echo "seed-creds: copied claude credentials"
else
    echo "seed-creds: no $CLAUDE_CREDS found (normal on Windows — claude keeps its" >&2
    echo "  token in Credential Manager, not a file). Log in inside the container" >&2
    echo "  instead: docker compose --profile sim exec takkub-sim claude login" >&2
fi

CODEX_AUTH="$HOME/.codex/auth.json"
if [ -f "$CODEX_AUTH" ]; then
    cp "$CODEX_AUTH" volumes/auth/codex/auth.json
    echo "seed-creds: copied codex credentials"
else
    echo "seed-creds: no $CODEX_AUTH found — log in inside the container instead:" >&2
    echo "  docker compose --profile sim exec takkub-sim codex login" >&2
fi
