#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# agent-takkub installer wrapper (.command) — double-click friendly on macOS.
#
# Why this exists:
#   A .command file runs in Terminal when double-clicked in Finder, so
#   non-CLI users can install without typing anything. It just hands off
#   to install.sh next to it, passing through any args you give it
#   (--update, --skip-mcp-prewarm, --vault-dir "...", --no-vault).
#
# Usage:
#   double-click in Finder            ← lazy mode (skip what's present)
#   ./install.command --update        ← upgrade everything
#   ./install.command --skip-mcp-prewarm
#   ./install.command --no-vault
#
# See install.sh for the full description of phases / flags.
# ─────────────────────────────────────────────────────────────
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/install.sh"

if [ ! -f "$SCRIPT" ]; then
  echo "[FAIL] install.sh not found next to this .command"
  echo "       expected at: $SCRIPT"
  exit 1
fi

bash "$SCRIPT" "$@"
RC=$?

# Keep the window readable when launched by double-click. Skip in CI with
# TAKKUB_INSTALL_NO_PAUSE=1.
if [ -z "${TAKKUB_INSTALL_NO_PAUSE:-}" ] && [ -t 0 ]; then
  echo
  read -r -p "Press Return to close…" _
fi
exit "$RC"
