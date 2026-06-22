<#
.SYNOPSIS
    Bootstrap installer + cockpit setup for agent-takkub on Windows.

.DESCRIPTION
    Installs every external dependency the cockpit needs, skips
    anything already present, then wires up cockpit-specific config
    so `python -m agent_takkub` runs cleanly.

    Sections:
      1.  System runtime   : Python 3.11+, Git, Node.js, Chrome, gh CLI
      2.  npm registry     : reset to public registry (gates MCP fetch)
      3.  AI CLIs          : Claude Code (required), OpenAI Codex +
                             Antigravity agy (both OPTIONAL — back the
                             codex/gemini roles; absent → run as Claude)
      4.  Claude plugins   : superpowers, agent-skills, Pordee
      4b. MCP servers      : Playwright MCP + Chrome DevTools MCP +
                             Playwright Chromium browser (~150 MB)
      5.  rtk              : Rust Token Killer (skipped if no cargo)
      6.  Cockpit setup    : git clone (if needed) + pip install -e .
      7.  Cockpit config   : role-providers.json + optional vault dir

    Login (claude / codex / agy) is intentionally NOT automated — those
    open browser OAuth flows that read better in a separate shell
    when the user is ready. Run them manually after install:
        claude login
        codex login   # optional (codex role)
        agy           # optional (gemini role) — first run does Google Sign-In

.PARAMETER Update
    Re-install / upgrade everything even if already present.

.PARAMETER SkipMCPPrewarm
    Skip Phase 4b (MCP pre-install). MCP packages still auto-download
    via `npx -y` on first use; pre-warm only saves the wait at
    cockpit's first claude-pane spawn.

.PARAMETER VaultDir
    Path to create an Obsidian vault skeleton at. Defaults to
    `$HOME\WebstormProjects\second-brain`. Pass empty string to skip.

.EXAMPLE
    .\scripts\install.ps1
    .\scripts\install.ps1 -Update
    .\scripts\install.ps1 -SkipMCPPrewarm -VaultDir ""

.NOTES
    Run from repo root: `.\scripts\install.ps1`
    Re-runnable safely — every step short-circuits if already done.
#>
[CmdletBinding()]
param(
    [switch]$Update,
    [switch]$SkipMCPPrewarm,
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
    # Fast path: if the tool is already on PATH, no need to involve
    # winget at all — covers users who installed via .msi/.exe or by
    # other package managers (chocolatey, scoop, manual extract).
    if ($ProbeCmd -and (Test-Cmd $ProbeCmd)) {
        if ($Update -and (Test-Cmd winget)) {
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
    # Not on PATH — we need winget to install it. Bail early with a
    # friendly hint instead of throwing CommandNotFoundException all
    # over the user's terminal.
    if (-not (Test-Cmd winget)) {
        Write-Fail "$DisplayName not installed, and winget is unavailable on this system."
        Write-Host "         Install 'App Installer' from Microsoft Store to enable winget," -ForegroundColor DarkYellow
        Write-Host "         or install $DisplayName manually (id: $Id)." -ForegroundColor DarkYellow
        $script:Summary.Failed += "$DisplayName (no winget)"
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

function Install-Antigravity {
    # OPTIONAL — backs the `gemini` role (Google's "third brain": planning /
    # second opinion). Antigravity ships a NATIVE installer (not npm) that drops
    # `agy.exe` under %LOCALAPPDATA%\agy\bin. The cockpit degrades the gemini
    # role to Claude when agy is absent, so this is strictly best-effort: a
    # failure here is logged as an optional skip and NEVER fails the install.
    $agyExe = Join-Path $env:LOCALAPPDATA "agy\bin\agy.exe"
    $present = (Test-Cmd agy) -or (Test-Path $agyExe)
    if ($present -and -not $Update) {
        Write-Skip "Antigravity (agy) already installed"
        $script:Summary.Skipped += "Antigravity (agy)"
        return
    }
    Write-Doing "installing Antigravity CLI (agy) - optional, backs the gemini role"
    try {
        $tmp = Join-Path $env:TEMP "agy-install.cmd"
        curl.exe -fsSL "https://antigravity.google/cli/install.cmd" -o $tmp
        if (-not (Test-Path $tmp)) { throw "installer download failed" }
        cmd /c $tmp | Out-Null
        Remove-Item $tmp -ErrorAction SilentlyContinue
        if ((Test-Cmd agy) -or (Test-Path $agyExe)) {
            Write-Ok "Antigravity (agy) installed - sign in later with: agy"
            $script:Summary.Installed += "Antigravity (agy)"
        } else {
            throw "agy not found after install"
        }
    } catch {
        Write-Host "  ~ Antigravity (agy) optional install skipped: $_" -ForegroundColor Yellow
        Write-Host "    gemini role will run as Claude until you install it manually:" -ForegroundColor DarkGray
        Write-Host "    https://antigravity.google/download  (then run 'agy' once to sign in)" -ForegroundColor DarkGray
        $script:Summary.Skipped += "Antigravity (agy) (optional - install manually)"
    }
}

function Install-ClaudePlugin {
    <#
    Claude Code's plugin system needs a *marketplace* registered
    before you can `plugin install`. A `github:owner/repo` spec
    might be the marketplace OR a single-plugin repo — we try the
    `marketplace add` path first (idempotent), then `plugin install`.
    Each step is best-effort; a failure on either is logged but
    doesn't abort the whole installer.
    #>
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
    # Step 1: register the spec as a marketplace if it isn't already.
    # `claude plugin marketplace add` accepts both `github:owner/repo`
    # and bare `owner/repo`. Strip the `github:` prefix so the bare
    # form is what gets logged when claude prints the marketplace name.
    $marketplaceArg = $Spec -replace '^github:', ''
    & claude plugin marketplace add $marketplaceArg 2>$null | Out-Null
    # Step 2: install. Three forms tried in order (each only fires
    # if the previous one fails) so different plugin layouts all work:
    #   a) `<spec>` — for marketplaces named differently from their plugin
    #   b) `<shortName>@<shortName>` — single-plugin repo convention
    #      (e.g. `kerlos/pordee` → `pordee@pordee`)
    #   c) `<shortName>` — fallback for legacy marketplaces
    Write-Doing "installing claude plugin $Spec"
    & claude plugin install $Spec 2>$null
    if ($LASTEXITCODE -ne 0) {
        & claude plugin install "$shortName@$shortName" 2>$null
    }
    if ($LASTEXITCODE -ne 0) {
        & claude plugin install $shortName 2>$null
    }
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "$shortName plugin installed"
        $script:Summary.Installed += "plugin:$shortName"
    } else {
        Write-Fail "$shortName plugin install failed"
        Write-Host "         try manually:" -ForegroundColor DarkYellow
        Write-Host "           claude plugin marketplace add $marketplaceArg" -ForegroundColor DarkYellow
        Write-Host "           claude plugin install $shortName@$shortName" -ForegroundColor DarkYellow
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
Write-Step "Phase 3 - AI CLIs (Claude required; Codex + Antigravity optional)"
Install-NpmGlobal -Package "@anthropic-ai/claude-code" -ProbeCmd "claude"
Install-NpmGlobal -Package "@openai/codex"             -ProbeCmd "codex"
Install-Antigravity

# ─────────────────────────────────────────────────────────────
# Phase 4 — Claude plugins
# ─────────────────────────────────────────────────────────────
Write-Step "Phase 4 - Claude plugins"
# ECC (everything-claude-code) intentionally NOT installed: its SessionStart
# prompt-hook crashed cockpit panes and added ~31k tokens/session. The cockpit
# still defensively mutes ECC hooks if it's present from another source
# (see pane_env ECC-mute + `takkub doctor` warning), but we don't pull it in.
Install-ClaudePlugin -Spec "github:jessevincent/superpowers"
Install-ClaudePlugin -Spec "github:addyosmani/agent-skills"
Install-ClaudePlugin -Spec "github:kerlos/pordee"

# ─────────────────────────────────────────────────────────────
# Phase 4b — MCP servers (Playwright + Chrome DevTools)
# ─────────────────────────────────────────────────────────────
# Cockpit's `runtime/shared-mcp.json` references these two servers
# via `npx -y <pkg>@<version>`. By default claude pulls them on
# first use (5-30 s wait at Lead pane spawn). Pre-installing into
# the npm global cache + downloading Playwright's bundled Chromium
# means the MCP servers are warm and ready the moment cockpit asks
# for them. Skip with -SkipMCPPrewarm.
Write-Step "Phase 4b - MCP servers (Playwright + Chrome DevTools)"
if ($SkipMCPPrewarm) {
    Write-Skip "MCP pre-warm skipped (-SkipMCPPrewarm). They'll auto-download via npx on first use."
    $script:Summary.Skipped += "MCP pre-warm"
} elseif (-not (Test-Cmd npm)) {
    Write-Fail "npm not on PATH - open a new shell after Node install"
    $script:Summary.Failed += "MCP pre-warm"
} else {
    Install-NpmGlobal -Package "@playwright/mcp@0.0.75"      -ProbeCmd ""
    Install-NpmGlobal -Package "chrome-devtools-mcp@0.26.0"  -ProbeCmd ""
    # Playwright MCP drives a bundled Chromium (not the system Chrome).
    # `playwright install chromium` is idempotent — exits fast if the
    # browser is already downloaded under %USERPROFILE%\AppData\Local\ms-playwright\.
    if (Test-Cmd npx) {
        Write-Doing "downloading Playwright Chromium browser (~150 MB, idempotent)"
        npx --yes playwright install chromium
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Playwright Chromium ready"
            $script:Summary.Installed += "playwright chromium"
        } else {
            Write-Fail "playwright install chromium exit $LASTEXITCODE"
            $script:Summary.Failed += "playwright chromium"
        }
    }
}

# ─────────────────────────────────────────────────────────────
# Phase 5 — rtk (optional)
# ─────────────────────────────────────────────────────────────
Write-Step "Phase 5 - rtk (Rust Token Killer)"
# NOTE: the cockpit's rtk is rtk-ai/rtk (Rust Token Killer — ships the
# `rtk hook claude` PreToolUse processor). The crates.io crate plainly named
# `rtk` is an UNRELATED tool, so `cargo install rtk` installs the WRONG binary —
# always build from the repo with `cargo install --git https://github.com/rtk-ai/rtk`
# (or grab the prebuilt rtk-x86_64-pc-windows-msvc.zip from its Releases page).
if (Test-Cmd rtk) {
    if ($Update -and (Test-Cmd cargo)) {
        Write-Doing "upgrading rtk via cargo (rtk-ai/rtk)"
        cargo install --force --git https://github.com/rtk-ai/rtk
        $script:Summary.Upgraded += "rtk"
    } else {
        Write-Skip "rtk already on PATH"
        $script:Summary.Skipped += "rtk"
    }
} elseif (Test-Cmd cargo) {
    Write-Doing "installing rtk via cargo (rtk-ai/rtk)"
    cargo install --git https://github.com/rtk-ai/rtk
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
# Verify — sanity-check key invariants; push to Summary.Failed
# ─────────────────────────────────────────────────────────────
Write-Step "Verify - installation sanity check"

# 1. Package importable (no venv on Windows — check system/user install)
python -c "import agent_takkub" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Ok "agent_takkub package importable"
} else {
    Write-Fail "agent_takkub not importable — pip install -e . may have failed"
    $script:Summary.Failed += "verify: agent_takkub import"
}

# 2. claude CLI on PATH
if (Test-Cmd claude) {
    Write-Ok "claude CLI on PATH ($(( Get-Command claude ).Source))"
} else {
    Write-Fail "claude not found on PATH — run: npm install -g @anthropic-ai/claude-code"
    $script:Summary.Failed += "verify: claude"
}

# 3. Node v20+
if (Test-Cmd node) {
    $nodeVer   = node -v                                    # e.g. "v22.1.0"
    $nodeMajor = [int]($nodeVer -replace '^v(\d+).*', '$1')
    if ($nodeMajor -ge 20) {
        Write-Ok "Node $nodeVer (v20+)"
    } else {
        Write-Fail "Node $nodeVer found but v20+ required"
        $script:Summary.Failed += "verify: node v20+"
    }
} else {
    Write-Fail "node not found on PATH"
    $script:Summary.Failed += "verify: node"
}

# 4. takkub list (fallback: python -m agent_takkub.cli list if takkub not on PATH)
if (Test-Cmd takkub) {
    takkub list 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "takkub list: exit 0"
    } else {
        Write-Fail "takkub list returned non-zero — CLI may not be installed correctly"
        $script:Summary.Failed += "verify: takkub list"
    }
} else {
    python -m agent_takkub.cli list 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "takkub CLI: exit 0 (via python -m agent_takkub.cli list)"
    } else {
        Write-Fail "takkub not on PATH and python -m agent_takkub.cli list failed"
        $script:Summary.Failed += "verify: takkub list"
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
Write-Host "  claude login              # OAuth (required)"
Write-Host "  codex login               # OAuth (optional - codex role)"
Write-Host "  agy                       # Google Sign-In (optional - gemini role; first run)"
Write-Host "  cd $cockpitDir"
Write-Host "  python -m agent_takkub"
Write-Host ""
if ($script:Summary.Failed.Count -eq 0) {
    Write-Host "All good. Re-run with -Update later to refresh." -ForegroundColor Green
} else {
    Write-Host "Some steps failed - fix and re-run." -ForegroundColor Yellow
}
