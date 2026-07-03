#!/usr/bin/env bash
# Isolated smoke test for the npm package — ZERO effect on a from-source dev
# checkout. Packs the tarball, installs it into a throwaway --prefix with a
# sandboxed AGENT_TAKKUB_HOME, launches once, then cleans everything up.
#
# What stays untouched: the repo's .venv, your global `takkub`, ~/.agent-takkub,
# ~/.takkub, ~/.claude. Everything the test creates lives under a mktemp dir and
# is removed on exit.
#
#   bash scripts/test-npm-install.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX="$(mktemp -d)"
PREFIX="$SANDBOX/prefix"
export AGENT_TAKKUB_HOME="$SANDBOX/home"

cleanup() { rm -rf "$SANDBOX"; echo "[test] cleaned $SANDBOX — dev setup untouched"; }
trap cleanup EXIT

echo "[test] sandbox:  $SANDBOX"
echo "[test] home:     $AGENT_TAKKUB_HOME (isolated)"
cd "$REPO"

# Build the wheel into dist/ if missing (no venv mutation — --no-deps).
if ! ls dist/*.whl >/dev/null 2>&1; then
  echo "[test] building wheel → dist/"
  python -m pip wheel . --no-deps -w dist/
fi

TGZ="$(npm pack --silent)"
echo "[test] packed:   $TGZ"

# Install into the isolated prefix — postinstall provisions the sandbox venv.
npm install -g "./$TGZ" --prefix "$PREFIX"

BIN="$PREFIX/bin/agent-takkub"
[ -x "$BIN" ] || BIN="$PREFIX/agent-takkub"      # Windows npm layout
echo "[test] launching $BIN  (close the window / Ctrl-C to finish)…"
"$BIN" || true

rm -f "$REPO/$TGZ"
echo "[test] done."
