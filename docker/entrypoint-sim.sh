#!/bin/sh
# Entrypoint for the takkub-sim sandbox service (docker-compose.yml, profile
# "sim"). Seeds first-boot state (idempotent), then hands off to the same
# headless entrypoint the prod image uses. See
# docs/guides/2026-08-31-docker-sandbox.md.
set -e

python3.11 /app/docker/seed_sim.py

# remote/http_server.py binds 127.0.0.1 only (assumes a co-located tunnel
# process, not a published Docker port — see seed_sim.py's INTERNAL_BIND_PORT
# comment). Relay the container's externally-published 8899 to that loopback
# port so `docker compose ... -p 8899:8899` actually reaches it.
socat TCP-LISTEN:8899,fork,reuseaddr TCP:127.0.0.1:18899 &

exec agent-takkub-headless
