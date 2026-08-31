# Copy claude/codex login credentials into the takkub-sim sandbox's
# bind-mounted volumes/auth/ (never the container's provider config
# directly — see docs/guides/2026-08-31-docker-sandbox.md). Run from the
# repo root, before or after `docker compose --profile sim up -d`.
#
# Best-effort only: if claude has no local .credentials.json (common on
# Windows), this skips it quietly — log in inside the container instead:
# `docker compose --profile sim exec -it takkub-sim claude login`.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

New-Item -ItemType Directory -Force -Path "volumes/auth/claude", "volumes/auth/codex" | Out-Null

$claudeCreds = Join-Path $HOME ".claude/.credentials.json"
if (Test-Path $claudeCreds) {
    Copy-Item $claudeCreds "volumes/auth/claude/.credentials.json" -Force
    Write-Host "seed-creds: copied claude credentials"
}

# codex: prefer the cockpit's own codex-home (the identity actually logged
# in today) over the bare ~/.codex default, which may hold a stale/expired
# refresh token from a prior non-cockpit login.
$codexAuth = Join-Path $HOME ".agent-takkub/codex-home/auth.json"
if (-not (Test-Path $codexAuth)) {
    $codexAuth = Join-Path $HOME ".codex/auth.json"
}
if (Test-Path $codexAuth) {
    Copy-Item $codexAuth "volumes/auth/codex/auth.json" -Force
    Write-Host "seed-creds: copied codex credentials from $codexAuth"
}
