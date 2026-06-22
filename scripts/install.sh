#!/usr/bin/env bash
#
# Bootstrap installer + cockpit setup for agent-takkub on macOS (and Linux).
#
# The macOS/Linux counterpart of scripts/install.ps1. Installs every external
# dependency the cockpit needs, skips anything already present, then wires up
# cockpit-specific config so `python -m agent_takkub` runs cleanly.
#
#   Phase 1  System runtime : Python 3.11+, Git, Node.js LTS, Chrome, gh CLI
#   Phase 2  npm registry   : reset to the public registry (gates MCP fetch)
#   Phase 3  AI CLIs        : Claude Code (required), OpenAI Codex + Antigravity
#                             agy (both OPTIONAL — back the codex/gemini roles;
#                             absent → those roles run as Claude)
#   Phase 4  Claude plugins : superpowers, agent-skills, Pordee
#   Phase 4b MCP servers    : Playwright MCP + Chrome DevTools MCP + Playwright
#                             Chromium browser (~150 MB)
#   Phase 5  rtk            : Rust Token Killer (skipped if no cargo)
#   Phase 6  Cockpit setup  : venv + editable install (uv if present, else pip)
#   Phase 7  Cockpit config : role-providers.json + optional Obsidian vault dir
#
# Login (claude / codex / agy) is intentionally NOT automated — those open
# browser OAuth flows that read better in a separate shell when you're ready:
#       claude            # OAuth (required)
#       codex login       # optional (codex role)
#       agy               # optional (gemini role) — first run does Google Sign-In
#
# System tools (Phase 1) install via Homebrew. If Homebrew is absent the script
# prints how to get it and continues with the phases that don't need it.
#
# Flags:
#   --update            re-install / upgrade everything even if already present
#   --skip-mcp-prewarm  skip Phase 4b (MCP packages still auto-download via npx)
#   --vault-dir <path>  Obsidian vault skeleton location (default: ~/WebstormProjects/second-brain)
#   --no-vault          skip the vault skeleton
#   -h | --help         show this help
#
# Re-runnable safely — every step short-circuits if already done.
# Run from repo root:  bash scripts/install.sh

set -uo pipefail

# ── flags ────────────────────────────────────────────────────────────────
UPDATE=0
SKIP_MCP_PREWARM=0
VAULT_DIR="$HOME/WebstormProjects/second-brain"
while [ $# -gt 0 ]; do
  case "$1" in
    --update) UPDATE=1 ;;
    --skip-mcp-prewarm) SKIP_MCP_PREWARM=1 ;;
    --vault-dir) VAULT_DIR="${2:-}"; shift ;;
    --no-vault) VAULT_DIR="" ;;
    -h|--help) grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# ── pretty output + summary tracking ──────────────────────────────────────
if [ -t 1 ]; then
  C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_GRAY=$'\033[90m'
  C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_RST=$'\033[0m'
else
  C_CYAN=""; C_GREEN=""; C_GRAY=""; C_YELLOW=""; C_RED=""; C_RST=""
fi
INSTALLED=(); SKIPPED=(); UPGRADED=(); FAILED=()
step()  { printf '\n%s==> %s%s\n' "$C_CYAN" "$1" "$C_RST"; }
ok()    { printf '  %s[OK]%s   %s\n' "$C_GREEN" "$C_RST" "$1"; }
skip()  { printf '  %s[SKIP]%s %s\n' "$C_GRAY" "$C_RST" "$1"; }
doing() { printf '  %s[..]%s   %s\n' "$C_CYAN" "$C_RST" "$1"; }
fail()  { printf '  %s[FAIL]%s %s\n' "$C_RED" "$C_RST" "$1"; }
warn()  { printf '  %s~ %s%s\n' "$C_YELLOW" "$1" "$C_RST"; }
have()  { command -v "$1" >/dev/null 2>&1; }

OS="$(uname -s)"

# ── Homebrew discovery ────────────────────────────────────────────────────
BREW=""
detect_brew() {
  if have brew; then BREW="$(command -v brew)"; return 0; fi
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$b" ] && "$b" --version >/dev/null 2>&1; then BREW="$b"; return 0; fi
  done
  return 1
}

# brew_install <display> <formula> <probe-cmd> [--cask]
brew_install() {
  local display="$1" formula="$2" probe="$3" cask="${4:-}"
  if [ -n "$probe" ] && have "$probe" && [ "$UPDATE" -eq 0 ]; then
    skip "$display already present"; SKIPPED+=("$display"); return
  fi
  if [ -z "$BREW" ]; then
    fail "$display — Homebrew not found (install from https://brew.sh then re-run)"
    FAILED+=("$display (no brew)"); return
  fi
  if [ -n "$probe" ] && have "$probe" && [ "$UPDATE" -eq 1 ]; then
    doing "upgrading $display"
    "$BREW" upgrade $cask "$formula" >/dev/null 2>&1 && { ok "$display upgraded"; UPGRADED+=("$display"); } \
      || { skip "$display already newest"; SKIPPED+=("$display"); }
    return
  fi
  doing "installing $display via brew"
  if "$BREW" install $cask "$formula"; then ok "$display installed"; INSTALLED+=("$display")
  else fail "$display brew install failed"; FAILED+=("$display"); fi
}

# npm_global <package> <probe-cmd>   (probe="" → always (re)install)
npm_global() {
  local pkg="$1" probe="$2"
  if ! have npm; then fail "$pkg — npm not on PATH"; FAILED+=("$pkg (no npm)"); return; fi
  if [ -n "$probe" ] && have "$probe" && [ "$UPDATE" -eq 0 ]; then
    skip "$pkg already present"; SKIPPED+=("$pkg"); return
  fi
  doing "npm install -g $pkg"
  if npm install -g "$pkg" >/dev/null 2>&1; then
    ok "$pkg installed"; [ "$UPDATE" -eq 1 ] && UPGRADED+=("$pkg") || INSTALLED+=("$pkg")
  else
    fail "$pkg npm install failed (if it's a permissions error, prefer a Homebrew node so npm -g needs no sudo)"
    FAILED+=("$pkg")
  fi
}

# claude_plugin <github:owner/repo>
claude_plugin() {
  local spec="$1" short
  short="${spec##*/}"
  if ! have claude; then fail "$spec — claude CLI not on PATH"; FAILED+=("plugin:$spec"); return; fi
  if claude plugin list 2>/dev/null | grep -qiF "$short" && [ "$UPDATE" -eq 0 ]; then
    skip "$short plugin already installed"; SKIPPED+=("plugin:$short"); return
  fi
  doing "installing claude plugin $spec"
  claude plugin marketplace add "${spec#github:}" >/dev/null 2>&1 || true
  if claude plugin install "$spec" >/dev/null 2>&1 \
     || claude plugin install "$short@$short" >/dev/null 2>&1 \
     || claude plugin install "$short" >/dev/null 2>&1; then
    ok "$short plugin installed"; INSTALLED+=("plugin:$short")
  else
    fail "$short plugin install failed — try: claude plugin marketplace add ${spec#github:} && claude plugin install $short@$short"
    FAILED+=("plugin:$short")
  fi
}

# node_ok — true when an active `node` is v20+.
node_ok() {
  have node || return 1
  local maj
  maj="$(node -v 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/')"
  [ -n "$maj" ] && [ "$maj" -ge 20 ] 2>/dev/null
}

# install_node_fallback — when Homebrew is unavailable/broken (or an old Node
# earlier in PATH can't be displaced via brew), fetch the official Node LTS
# tarball straight from nodejs.org into ~/.local (which is on PATH and needs no
# sudo). npm's global prefix is pointed at ~/.local too, so `npm i -g` bins
# (claude, codex) land on PATH. This is what makes the installer self-heal on a
# machine whose system Node is too old to displace.
install_node_fallback() {
  local arch os ver pkg url tmp
  case "$(uname -m)" in
    arm64|aarch64) arch="arm64" ;;
    x86_64) arch="x64" ;;
    *) warn "unsupported arch $(uname -m) for Node fallback"; return 1 ;;
  esac
  case "$OS" in Darwin) os="darwin" ;; Linux) os="linux" ;; *) return 1 ;; esac
  doing "fetching latest Node LTS index from nodejs.org"
  ver="$(curl -fsSL https://nodejs.org/dist/index.json 2>/dev/null \
        | python3 -c "import sys,json; print(next(r['version'] for r in json.load(sys.stdin) if r['lts']))" 2>/dev/null)"
  [ -n "$ver" ] || { fail "could not resolve latest Node LTS"; return 1; }
  pkg="node-${ver}-${os}-${arch}"
  url="https://nodejs.org/dist/${ver}/${pkg}.tar.gz"
  tmp="$(mktemp -d)"
  doing "downloading $pkg"
  if ! curl -fsSL -o "$tmp/node.tar.gz" "$url"; then fail "Node download failed"; rm -rf "$tmp"; return 1; fi
  tar -xzf "$tmp/node.tar.gz" -C "$tmp" || { fail "Node extract failed"; rm -rf "$tmp"; return 1; }
  mkdir -p "$HOME/.local/bin" "$HOME/.local/lib/nodejs"
  rm -rf "$HOME/.local/lib/nodejs/$pkg"
  mv "$tmp/$pkg" "$HOME/.local/lib/nodejs/"
  ln -sf "$HOME/.local/lib/nodejs/$pkg/bin/node" "$HOME/.local/bin/node"
  ln -sf "$HOME/.local/lib/nodejs/$pkg/bin/npm"  "$HOME/.local/bin/npm"
  ln -sf "$HOME/.local/lib/nodejs/$pkg/bin/npx"  "$HOME/.local/bin/npx"
  rm -rf "$tmp"
  hash -r 2>/dev/null || true
  # Global installs → ~/.local so their bins are on PATH (no sudo).
  npm config set prefix "$HOME/.local" >/dev/null 2>&1 || true
  if node_ok; then ok "Node $(node -v) installed to ~/.local/bin"; INSTALLED+=("Node.js ${ver} (nodejs.org)"); return 0; fi
  warn "Node fallback installed but 'node' still resolves to $(node -v 2>/dev/null) — ensure ~/.local/bin precedes it in PATH"
  return 1
}

# ───────────────────────────────────────────────────────────────────────────
# Phase 1 — System runtime
# ───────────────────────────────────────────────────────────────────────────
step "Phase 1 - System runtime"
if ! detect_brew; then
  warn "Homebrew not found — system tools (python/git/node/chrome/gh) will be skipped."
  warn "Install it once with:"
  warn '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
fi
brew_install "Python 3.12" "python@3.12" "python3"
brew_install "Git"         "git"         "git"
brew_install "Node.js"     "node"        "node"
if [ "$OS" = "Darwin" ]; then
  if [ -d "/Applications/Google Chrome.app" ] && [ "$UPDATE" -eq 0 ]; then
    skip "Chrome already present"; SKIPPED+=("Chrome")
  else
    brew_install "Chrome" "google-chrome" "" "--cask"
  fi
else
  skip "Chrome cask is macOS-only — install Chrome/Chromium via your package manager"
fi
brew_install "GitHub CLI" "gh" "gh"

# Node must be v20+ (claude-code CLI + every MCP server need a modern Node).
# brew_install above may have skipped because an OLD node is on PATH, or brew
# may be broken — so verify the *active* version and self-heal if it's stale.
if node_ok; then
  ok "Node $(node -v) (v20+)"
else
  [ -n "$(command -v node)" ] && warn "active node is $(node -v) but v20+ is required."
  # Try brew first (if it actually produced a modern node, great)…
  if [ -n "$BREW" ]; then "$BREW" install node >/dev/null 2>&1 || true; hash -r 2>/dev/null || true; fi
  # …otherwise download the official LTS into ~/.local (no brew, no sudo).
  node_ok || install_node_fallback || { fail "Node v20+ required but could not be installed"; FAILED+=("Node.js v20+"); }
fi

# ───────────────────────────────────────────────────────────────────────────
# Phase 2 — npm registry
# ───────────────────────────────────────────────────────────────────────────
step "Phase 2 - npm registry"
if have npm; then
  CURRENT="$(npm config get registry 2>/dev/null | tr -d '[:space:]')"
  if [ "$CURRENT" != "https://registry.npmjs.org/" ]; then
    doing "switching npm registry to public (was $CURRENT)"
    npm config set registry https://registry.npmjs.org/ && { ok "npm registry → public"; INSTALLED+=("npm registry (public)"); }
  else
    skip "npm registry already public"; SKIPPED+=("npm registry")
  fi
else
  fail "npm not on PATH — open a new shell after Node install"; FAILED+=("npm registry")
fi

# ───────────────────────────────────────────────────────────────────────────
# Phase 3 — AI CLIs
# ───────────────────────────────────────────────────────────────────────────
step "Phase 3 - AI CLIs"
npm_global "@anthropic-ai/claude-code" "claude"   # required (Lead + all claude roles)
npm_global "@openai/codex"             "codex"    # optional (codex role)

# Antigravity (agy) — OPTIONAL, backs the gemini role. Native installer, not npm.
# Best-effort: a failure here is logged as an optional skip, never fatal.
if have agy && [ "$UPDATE" -eq 0 ]; then
  skip "Antigravity (agy) already installed"; SKIPPED+=("Antigravity (agy)")
else
  doing "installing Antigravity CLI (agy) - optional, backs the gemini role"
  if curl -fsSL "https://antigravity.google/cli/install.sh" 2>/dev/null | sh >/dev/null 2>&1 && have agy; then
    ok "Antigravity (agy) installed - sign in later with: agy"; INSTALLED+=("Antigravity (agy)")
  else
    warn "Antigravity (agy) optional install skipped — gemini role runs as Claude until installed."
    warn "Install manually: https://antigravity.google/download  (then run 'agy' once to sign in)"
    SKIPPED+=("Antigravity (agy) (optional - install manually)")
  fi
fi

# ───────────────────────────────────────────────────────────────────────────
# Phase 4 — Claude plugins
# ───────────────────────────────────────────────────────────────────────────
# ECC (everything-claude-code) intentionally NOT installed: its SessionStart
# hook crashed cockpit panes and added ~31k tokens/session.
step "Phase 4 - Claude plugins"
claude_plugin "github:jessevincent/superpowers"
claude_plugin "github:addyosmani/agent-skills"
claude_plugin "github:kerlos/pordee"

# ───────────────────────────────────────────────────────────────────────────
# Phase 4b — MCP servers (Playwright + Chrome DevTools)
# ───────────────────────────────────────────────────────────────────────────
step "Phase 4b - MCP servers (Playwright + Chrome DevTools)"
if [ "$SKIP_MCP_PREWARM" -eq 1 ]; then
  skip "MCP pre-warm skipped (--skip-mcp-prewarm). They'll auto-download via npx on first use."
  SKIPPED+=("MCP pre-warm")
elif ! have npm; then
  fail "npm not on PATH — open a new shell after Node install"; FAILED+=("MCP pre-warm")
else
  npm_global "@playwright/mcp@0.0.75"     ""
  npm_global "chrome-devtools-mcp@0.26.0" ""
  if have npx; then
    doing "downloading Playwright Chromium browser (~150 MB, idempotent)"
    if npx --yes playwright install chromium; then ok "Playwright Chromium ready"; INSTALLED+=("playwright chromium")
    else fail "playwright install chromium failed"; FAILED+=("playwright chromium"); fi
  fi
fi

# ───────────────────────────────────────────────────────────────────────────
# Phase 5 — rtk (optional)
# ───────────────────────────────────────────────────────────────────────────
step "Phase 5 - rtk (Rust Token Killer)"
if have rtk; then
  if [ "$UPDATE" -eq 1 ] && have cargo; then
    doing "upgrading rtk via cargo"; cargo install --force rtk && UPGRADED+=("rtk")
  else
    skip "rtk already on PATH"; SKIPPED+=("rtk")
  fi
elif have cargo; then
  doing "installing rtk via cargo"
  if cargo install rtk; then ok "rtk installed"; INSTALLED+=("rtk"); else fail "rtk install failed"; FAILED+=("rtk"); fi
else
  skip "rtk skipped - no cargo. Use cockpit's '[Install rtk]' button after launch, or grab a release binary."
  SKIPPED+=("rtk (no cargo)")
fi

# ───────────────────────────────────────────────────────────────────────────
# Phase 6 — Cockpit venv + editable install
# ───────────────────────────────────────────────────────────────────────────
step "Phase 6 - Cockpit (agent-takkub)"
cd "$REPO_ROOT" || { fail "cannot cd to repo root $REPO_ROOT"; FAILED+=("cockpit"); }
VENV="$REPO_ROOT/.venv"
if have uv; then
  # uv: standalone Python + venv + editable install, no system Python / brew needed.
  [ -d "$VENV" ] && skip "venv exists ($VENV)" || { doing "uv venv (Python 3.12)"; uv venv --python 3.12 >/dev/null 2>&1 || uv venv >/dev/null 2>&1; }
  doing "uv pip install -e ."
  if uv pip install -e . >/dev/null 2>&1; then ok "agent-takkub installed (editable, uv)"; INSTALLED+=("cockpit (uv)")
  else fail "uv pip install failed"; FAILED+=("cockpit"); fi
else
  # Fallback: stock venv + pip. Needs a python3 that satisfies requires-python>=3.11.
  PY="python3"
  if [ -d "$VENV" ]; then skip "venv exists ($VENV)"; else doing "python3 -m venv .venv"; "$PY" -m venv "$VENV" || { fail "venv create failed"; FAILED+=("cockpit"); }; fi
  if [ -x "$VENV/bin/python" ]; then
    doing "pip install -e ."
    if "$VENV/bin/python" -m pip install -e . --quiet; then ok "agent-takkub installed (editable)"; INSTALLED+=("cockpit")
    else fail "pip install failed"; FAILED+=("cockpit"); fi
  fi
fi

# ───────────────────────────────────────────────────────────────────────────
# Phase 7 — Cockpit config
# ───────────────────────────────────────────────────────────────────────────
step "Phase 7 - Cockpit config"
TAKKUB_DIR="$HOME/.takkub"
PROVIDERS="$TAKKUB_DIR/role-providers.json"
if [ -f "$PROVIDERS" ]; then
  skip "role-providers.json already exists at $PROVIDERS"; SKIPPED+=("role-providers.json")
else
  mkdir -p "$TAKKUB_DIR" && printf '{}' > "$PROVIDERS"
  ok "created empty $PROVIDERS (edit to map roles -> claude/codex)"; INSTALLED+=("role-providers.json")
fi
if [ -n "$VAULT_DIR" ]; then
  if [ -d "$VAULT_DIR/01-Projects" ]; then
    skip "vault already exists at $VAULT_DIR"; SKIPPED+=("vault")
  else
    mkdir -p "$VAULT_DIR/01-Projects" && { ok "vault skeleton created at $VAULT_DIR/01-Projects"; INSTALLED+=("vault skeleton"); }
  fi
fi

# ───────────────────────────────────────────────────────────────────────────
# Summary
# ───────────────────────────────────────────────────────────────────────────
printf '\n%s============================================%s\n' "$C_CYAN" "$C_RST"
printf '%s  Install summary%s\n' "$C_CYAN" "$C_RST"
printf '%s============================================%s\n' "$C_CYAN" "$C_RST"
[ ${#INSTALLED[@]} -gt 0 ] && { printf '\n%sInstalled:%s\n' "$C_GREEN" "$C_RST"; for i in "${INSTALLED[@]}"; do printf '%s  + %s%s\n' "$C_GREEN" "$i" "$C_RST"; done; }
[ ${#UPGRADED[@]}  -gt 0 ] && { printf '\n%sUpgraded:%s\n'  "$C_YELLOW" "$C_RST"; for i in "${UPGRADED[@]}"; do printf '%s  ^ %s%s\n' "$C_YELLOW" "$i" "$C_RST"; done; }
[ ${#SKIPPED[@]}   -gt 0 ] && { printf '\n%sSkipped (already present):%s\n' "$C_GRAY" "$C_RST"; for i in "${SKIPPED[@]}"; do printf '%s  = %s%s\n' "$C_GRAY" "$i" "$C_RST"; done; }
[ ${#FAILED[@]}    -gt 0 ] && { printf '\n%sFailed (review above):%s\n' "$C_RED" "$C_RST"; for i in "${FAILED[@]}"; do printf '%s  ! %s%s\n' "$C_RED" "$i" "$C_RST"; done; }

printf '\n%sNext:%s\n' "$C_CYAN" "$C_RST"
printf '  claude                    # OAuth login (required)\n'
printf '  codex login               # OAuth (optional - codex role)\n'
printf '  agy                       # Google Sign-In (optional - gemini role; first run)\n'
printf '  cd %s\n' "$REPO_ROOT"
printf '  .venv/bin/agent-takkub    # launch the cockpit\n\n'
if [ ${#FAILED[@]} -eq 0 ]; then
  printf '%sAll good. Re-run with --update later to refresh.%s\n' "$C_GREEN" "$C_RST"
else
  printf '%sSome steps failed - fix and re-run.%s\n' "$C_YELLOW" "$C_RST"
fi
