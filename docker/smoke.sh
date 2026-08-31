#!/usr/bin/env bash
# Regression check for the takkub-sim sandbox: build, boot, verify
# /api/bootstrap answers, then tear down (volumes/ state is left alone — no
# `docker compose down -v`). Run from repo root or anywhere; cd's to the
# repo root itself. Works under Git Bash on Windows and bash on macOS/Linux.
#
# Usage: docker/smoke.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PORT="${TAKKUB_BIND_PORT:-8899}"

echo "== build =="
docker compose --profile sim build takkub-sim

echo "== up =="
docker compose --profile sim up -d takkub-sim

cleanup() {
    echo "== down (volumes/ kept) =="
    docker compose --profile sim down
}
trap cleanup EXIT

echo "== waiting for /api/bootstrap (up to 90s) =="
deadline=$((SECONDS + 90))
code=""
secret=""
while [ "$SECONDS" -lt "$deadline" ]; do
    secret=$(docker compose --profile sim exec -T takkub-sim \
        python3.11 -c "from agent_takkub.remote.config import RemoteConfig; print(RemoteConfig.load().secret_path)" \
        2>/dev/null || true)
    if [ -n "$secret" ]; then
        code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/${secret}/api/bootstrap" || true)
        if [ "$code" = "200" ]; then
            break
        fi
    fi
    sleep 3
done

if [ "$code" != "200" ]; then
    echo "FAILED: /api/bootstrap never returned 200 within 90s (last: ${code:-<none>})" >&2
    docker compose --profile sim logs --tail 150 takkub-sim
    exit 1
fi
echo "bootstrap OK (200)"

echo "== takkub status (inside container) =="
docker compose --profile sim exec -T takkub-sim takkub status || true

echo "== smoke OK =="
