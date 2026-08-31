# Copy claude/codex login credentials into the takkub-sim sandbox's
# bind-mounted volumes/auth/ (never the container's provider config
# directly — see docs/guides/2026-08-31-docker-sandbox.md). Run from the
# repo root, before or after `docker compose --profile sim up -d`.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

New-Item -ItemType Directory -Force -Path "volumes/auth/claude", "volumes/auth/codex" | Out-Null

# claude: on Windows the OAuth token lives in Credential Manager, not a file
# — there is nothing to copy (see doctor.py's platform-gated auth check).
# Confirmed on this machine: ~/.claude/.credentials.json does not exist.
$claudeCreds = Join-Path $HOME ".claude/.credentials.json"
if (Test-Path $claudeCreds) {
    Copy-Item $claudeCreds "volumes/auth/claude/.credentials.json" -Force
    Write-Host "seed-creds: copied claude credentials"
} else {
    Write-Warning "no $claudeCreds found (expected on Windows — claude keeps its token in Credential Manager, not a file)."
    Write-Warning "  Log in inside the container instead: docker compose --profile sim exec takkub-sim claude login"
}

$codexAuth = Join-Path $HOME ".codex/auth.json"
if (Test-Path $codexAuth) {
    Copy-Item $codexAuth "volumes/auth/codex/auth.json" -Force
    Write-Host "seed-creds: copied codex credentials"
} else {
    Write-Warning "no $codexAuth found — log in inside the container instead:"
    Write-Warning "  docker compose --profile sim exec takkub-sim codex login"
}
