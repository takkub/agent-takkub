#!/usr/bin/env bash
# Copy claude/codex login credentials into the takkub-sim sandbox's
# bind-mounted volumes/auth/ (never the container's provider config
# directly — see docs/guides/2026-08-31-docker-sandbox.md). Run from the
# repo root, before or after `docker compose --profile sim up -d`.
#
# Best-effort only: if claude has no local .credentials.json, this skips it
# quietly — log in inside the container instead:
# `docker compose --profile sim exec -it takkub-sim claude login`.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

mkdir -p volumes/auth/claude volumes/auth/codex

CLAUDE_CREDS="$HOME/.claude/.credentials.json"
if [ -f "$CLAUDE_CREDS" ]; then
    cp "$CLAUDE_CREDS" volumes/auth/claude/.credentials.json
    echo "seed-creds: copied claude credentials"
fi

# codex: prefer the cockpit's own codex-home (the identity actually logged
# in today) over the bare ~/.codex default, which may hold a stale/expired
# refresh token from a prior non-cockpit login.
CODEX_AUTH="$HOME/.agent-takkub/codex-home/auth.json"
[ -f "$CODEX_AUTH" ] || CODEX_AUTH="$HOME/.codex/auth.json"
if [ -f "$CODEX_AUTH" ]; then
    cp "$CODEX_AUTH" volumes/auth/codex/auth.json
    echo "seed-creds: copied codex credentials from $CODEX_AUTH"
fi
