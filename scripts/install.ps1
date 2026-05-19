<#
.SYNOPSIS
    Bootstrap installer + cockpit setup for agent-takkub on Windows.

.DESCRIPTION
    Installs every external dependency the cockpit needs, skips
    anything already present, then wires up cockpit-specific config
    so `python -m agent_takkub` runs cleanly.

    Sections:
      1. System runtime    : Python 3.11+, Git, Node.js, Chrome, gh CLI
      2. npm registry      : reset to public registry (gates MCP fetch)
      3. AI CLIs           : Claude Code, OpenAI Codex
      4. Claude plugins    : superpowers, agent-skills, ECC, Pordee
      5. rtk               : Rust Token Killer (skipped if no cargo)
      6. Cockpit setup     : git clone (if needed) + pip install -e .
      7. Cockpit config    : role-providers.json + optional vault dir
      8. Login (optional)  : claude login + codex login (interactive)

.PARAMETER Update
    Re-install / upgrade everything even if already present.

.PARAMETER SkipLogin
    Skip the interactive `claude login` / `codex login` prompts at
    the end. Useful for re-runs after the first one.

.PARAMETER VaultDir
    Path to create an Obsidian vault skeleton at. Defaults to
    `$HOME\WebstormProjects\second-brain`. Pass empty string to skip.

.EXAMPLE
    .\scripts\install.ps1
    .\scripts\install.ps1 -Update
    .\scripts\install.ps1 -SkipLogin -VaultDir ""

.NOTES
    Run from repo root: `.\scripts\install.ps1`
    Re-runnable safely — every step short-circuits if already done.
#>
[CmdletBinding()]
param(
    [switch]$Update,
    [switch]$SkipLogin,
    [string]$VaultDir = "$env:USERPROFILE\WebstormProjects\second-brain"
)

$ErrorActionPreference = "Continue"

# Track what happened so we can print a summary at the end.
$script:Summary = @{
    Installed = @()
    Skipped   = @()
    Upgraded  = @()
    Failed    = @()
}

function Write-Step {
    param([string]$Msg)
    Write-Host ""
    Write-Host "==> $Msg" -ForegroundColor Cyan
}

function Write-Ok    { param([string]$M) Write-Host "  [OK]   $M" -ForegroundColor Green }
function Write-Skip  { param([string]$M) Write-Host "  [SKIP] $M" -ForegroundColor DarkGray }
function Write-Doing { param([string]$M) Write-Host "  [...]  $M" -ForegroundColor Yellow }
function Write-Fail  { param([string]$M) Write-Host "  [FAIL] $M" -ForegroundColor Red }

function Test-Cmd {
    param([Parameter(Mandatory)][string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-WingetPackage {
    param([Parameter(Mandatory)][string]$Id)
    $output = winget list --id $Id --exact 2>$null | Out-String
    return ($LASTEXITCODE -eq 0) -and ($output -match [regex]::Escape($Id))
}

function Install-Winget {
    param(
        [Parameter(Mandatory)][string]$DisplayName,
        [Parameter(Mandatory)][string]$Id,
        [string]$ProbeCmd
    )
    if ($ProbeCmd -and (Test-Cmd $ProbeCmd)) {
        if ($Update) {
            Write-Doing "upgrading $DisplayName via winget"
            winget upgrade --id $Id --silent --accept-package-agreements --accept-source-agreements | Out-Null
            $script:Summary.Upgraded += $DisplayName
            Write-Ok "$DisplayName upgraded (or already latest)"
        } else {
            Write-Skip "$DisplayName already installed"
            $script:Summary.Skipped += $DisplayName
        }
        return
    }
    if (Test-WingetPackage -Id $Id) {
        Write-Skip "$DisplayName already installed (winget)"
        $script:Summary.Skipped += $DisplayName
        return
    }
    Write-Doing "installing $DisplayName"
    winget install --id $Id --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "$DisplayName installed"
        $script:Summary.Installed += $DisplayName
    } else {
        Write-Fail "$DisplayName winget exit $LASTEXITCODE"
        $script:Summary.Failed += $DisplayName
    }
}

function Install-NpmGlobal {
    param(
        [Parameter(Mandatory)][string]$Package,
        [string]$ProbeCmd
    )
    if (-not (Test-Cmd npm)) {
        Write-Fail "$Package - npm not on PATH yet (open a new shell after Node install)"
        $script:Summary.Failed += $Package
        return
    }
    if ($ProbeCmd -and (Test-Cmd $ProbeCmd)) {
        if ($Update) {
            Write-Doing "upgrading $Package"
            npm install -g $Package | Out-Null
            $script:Summary.Upgraded += $Package
            Write-Ok "$Package upgraded"
        } else {
            Write-Skip "$Package already installed"
            $script:Summary.Skipped += $Package
        }
        return
    }
    Write-Doing "installing $Package globally"
    npm install -g $Package
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "$Package installed"
        $script:Summary.Installed += $Package
    } else {
        Write-Fail "$Package npm exit $LASTEXITCODE"
        $script:Summary.Failed += $Package
    }
}

function Install-ClaudePlugin {
    param([Parameter(Mandatory)][string]$Spec)
    if (-not (Test-Cmd claude)) {
        Write-Fail "$Spec - claude CLI not on PATH"
        $script:Summary.Failed += "plugin:$Spec"
        return
    }
    $installed = & claude plugin list 2>$null | Out-String
    $shortName = ($Spec -split '/')[-1]
    if ($installed -match [regex]::Escape($shortName)) {
        if ($Update) {
            Write-Doing "re-installing $Spec to refresh"
            & claude plugin install $Spec 2>$null
            $script:Summary.Upgraded += "plugin:$shortName"
        } else {
            Write-Skip "$shortName plugin already installed"
            $script:Summary.Skipped += "plugin:$shortName"
        }
        return
    }
    Write-Doing "installing claude plugin $Spec"
    & claude plugin install $Spec
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "$shortName plugin installed"
        $script:Summary.Installed += "plugin:$shortName"
    } else {
        Write-Fail "$shortName plugin install failed (exit $LASTEXITCODE)"
        $script:Summary.Failed += "plugin:$shortName"
    }
}

# ─────────────────────────────────────────────────────────────
# Phase 1 — System runtime
# ─────────────────────────────────────────────────────────────
Write-Step "Phase 1 - System runtime"
Install-Winget -DisplayName "Python 3.11" -Id "Python.Python.3.11"  -ProbeCmd "python"
Install-Winget -DisplayName "Git"         -Id "Git.Git"             -ProbeCmd "git"
Install-Winget -DisplayName "Node.js LTS" -Id "OpenJS.NodeJS.LTS"   -ProbeCmd "node"
Install-Winget -DisplayName "Chrome"      -Id "Google.Chrome"       -ProbeCmd "chrome"
Install-Winget -DisplayName "GitHub CLI"  -Id "GitHub.cli"          -ProbeCmd "gh"

# ─────────────────────────────────────────────────────────────
# Phase 2 — npm registry
# ─────────────────────────────────────────────────────────────
Write-Step "Phase 2 - npm registry"
if (Test-Cmd npm) {
    $current = (npm config get registry).Trim()
    if ($current -ne "https://registry.npmjs.org/") {
        Write-Doing "switching npm registry to public (was $current)"
        npm config set registry https://registry.npmjs.org/
        $script:Summary.Installed += "npm registry (public)"
    } else {
        Write-Skip "npm registry already public"
        $script:Summary.Skipped += "npm registry"
    }
} else {
    Write-Fail "npm not on PATH - open a new shell after Node install"
    $script:Summary.Failed += "npm registry"
}

# ─────────────────────────────────────────────────────────────
# Phase 3 — AI CLIs
# ─────────────────────────────────────────────────────────────
Write-Step "Phase 3 - AI CLIs (Claude + Codex)"
Install-NpmGlobal -Package "@anthropic-ai/claude-code" -ProbeCmd "claude"
Install-NpmGlobal -Package "@openai/codex"             -ProbeCmd "codex"

# ─────────────────────────────────────────────────────────────
# Phase 4 — Claude plugins
# ─────────────────────────────────────────────────────────────
Write-Step "Phase 4 - Claude plugins"
Install-ClaudePlugin -Spec "github:jessevincent/superpowers"
Install-ClaudePlugin -Spec "github:addyosmani/agent-skills"
Install-ClaudePlugin -Spec "github:everything-claude-code/marketplace"
Install-ClaudePlugin -Spec "github:kerlos/pordee"

# ─────────────────────────────────────────────────────────────
# Phase 5 — rtk (optional)
# ─────────────────────────────────────────────────────────────
Write-Step "Phase 5 - rtk (Rust Token Killer)"
if (Test-Cmd rtk) {
    if ($Update -and (Test-Cmd cargo)) {
        Write-Doing "upgrading rtk via cargo"
        cargo install --force rtk
        $script:Summary.Upgraded += "rtk"
    } else {
        Write-Skip "rtk already on PATH"
        $script:Summary.Skipped += "rtk"
    }
} elseif (Test-Cmd cargo) {
    Write-Doing "installing rtk via cargo"
    cargo install rtk
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "rtk installed"
        $script:Summary.Installed += "rtk"
    } else {
        Write-Fail "rtk install failed"
        $script:Summary.Failed += "rtk"
    }
} else {
    Write-Skip "rtk skipped - no cargo. Use cockpit's '[Install rtk]' button after launch, or grab a release binary."
    $script:Summary.Skipped += "rtk (no cargo)"
}

# ─────────────────────────────────────────────────────────────
# Phase 6 — Cockpit clone + pip install
# ─────────────────────────────────────────────────────────────
Write-Step "Phase 6 - Cockpit (agent-takkub)"
$cockpitDir = Join-Path $env:USERPROFILE "WebstormProjects\agent-takkub"
if (Test-Path (Join-Path $cockpitDir ".git")) {
    Write-Skip "agent-takkub already cloned at $cockpitDir"
    $script:Summary.Skipped += "agent-takkub clone"
    if ($Update) {
        Write-Doing "git pull --ff-only origin main"
        Push-Location $cockpitDir
        git pull --ff-only origin main
        Pop-Location
        $script:Summary.Upgraded += "agent-takkub (pulled)"
    }
} else {
    $parent = Split-Path $cockpitDir
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    Write-Doing "cloning agent-takkub to $cockpitDir"
    git clone https://github.com/takkub/agent-takkub.git $cockpitDir
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "agent-takkub cloned"
        $script:Summary.Installed += "agent-takkub"
    } else {
        Write-Fail "git clone failed"
        $script:Summary.Failed += "agent-takkub"
    }
}
if (Test-Path $cockpitDir) {
    Push-Location $cockpitDir
    Write-Doing "pip install -e ."
    python -m pip install -e . --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "agent-takkub Python package installed (editable)"
    } else {
        Write-Fail "pip install failed"
        $script:Summary.Failed += "pip install -e"
    }
    Pop-Location
}

# ─────────────────────────────────────────────────────────────
# Phase 7 — Cockpit config
# ─────────────────────────────────────────────────────────────
Write-Step "Phase 7 - Cockpit config"

# ~/.takkub/role-providers.json
$takkubDir = Join-Path $env:USERPROFILE ".takkub"
$providersFile = Join-Path $takkubDir "role-providers.json"
if (Test-Path $providersFile) {
    Write-Skip "role-providers.json already exists at $providersFile"
    $script:Summary.Skipped += "role-providers.json"
} else {
    New-Item -ItemType Directory -Path $takkubDir -Force | Out-Null
    '{}' | Out-File -FilePath $providersFile -Encoding utf8 -NoNewline
    Write-Ok "created empty $providersFile (edit to map roles -> claude/codex)"
    $script:Summary.Installed += "role-providers.json"
}

# Obsidian vault skeleton (optional)
if ($VaultDir -and (-not (Test-Path (Join-Path $VaultDir "01-Projects")))) {
    New-Item -ItemType Directory -Path (Join-Path $VaultDir "01-Projects") -Force | Out-Null
    Write-Ok "vault skeleton created at $VaultDir\01-Projects"
    $script:Summary.Installed += "vault skeleton"
} elseif ($VaultDir) {
    Write-Skip "vault already exists at $VaultDir"
    $script:Summary.Skipped += "vault"
}

# ─────────────────────────────────────────────────────────────
# Phase 8 — Login (optional, interactive)
# ─────────────────────────────────────────────────────────────
if (-not $SkipLogin) {
    Write-Step "Phase 8 - Auth login (interactive)"
    Write-Host "  About to run 'claude login' and 'codex login'." -ForegroundColor Yellow
    Write-Host "  Each opens a browser for OAuth. Skip with -SkipLogin." -ForegroundColor Yellow
    $resp = Read-Host "  Proceed? [Y/n]"
    if ($resp -ne 'n') {
        if (Test-Cmd claude) {
            Write-Doing "claude login"
            claude login
        }
        if (Test-Cmd codex) {
            Write-Doing "codex login"
            codex login
        }
    } else {
        Write-Skip "logins skipped - run 'claude login' and 'codex login' manually later"
    }
}

# ─────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Install summary" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
if ($script:Summary.Installed.Count -gt 0) {
    Write-Host ""
    Write-Host "Installed:" -ForegroundColor Green
    $script:Summary.Installed | ForEach-Object { Write-Host "  + $_" -ForegroundColor Green }
}
if ($script:Summary.Upgraded.Count -gt 0) {
    Write-Host ""
    Write-Host "Upgraded:" -ForegroundColor Yellow
    $script:Summary.Upgraded | ForEach-Object { Write-Host "  ^ $_" -ForegroundColor Yellow }
}
if ($script:Summary.Skipped.Count -gt 0) {
    Write-Host ""
    Write-Host "Skipped (already present):" -ForegroundColor DarkGray
    $script:Summary.Skipped | ForEach-Object { Write-Host "  = $_" -ForegroundColor DarkGray }
}
if ($script:Summary.Failed.Count -gt 0) {
    Write-Host ""
    Write-Host "Failed (review above):" -ForegroundColor Red
    $script:Summary.Failed | ForEach-Object { Write-Host "  ! $_" -ForegroundColor Red }
}

Write-Host ""
Write-Host "Next:" -ForegroundColor Cyan
Write-Host "  cd $cockpitDir"
Write-Host "  python -m agent_takkub"
Write-Host ""
if ($script:Summary.Failed.Count -eq 0) {
    Write-Host "All good. Re-run with -Update later to refresh." -ForegroundColor Green
} else {
    Write-Host "Some steps failed - fix and re-run." -ForegroundColor Yellow
}
