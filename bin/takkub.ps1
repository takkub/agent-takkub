#!/usr/bin/env pwsh
# `takkub` CLI PowerShell shim (#789)
# Avoids cmd.exe metacharacter corruption (| & < > ^) when running under PowerShell.

$ErrorActionPreference = 'Stop'
$HERE = Split-Path -Parent $MyInvocation.MyCommand.Path
$REPO = Split-Path -Parent $HERE
$PY = Join-Path $REPO ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PY)) {
    [Console]::Error.WriteLine("[takkub] cockpit interpreter missing: $PY")
    exit 1
}

& $PY -m agent_takkub.cli @args
exit $LASTEXITCODE
