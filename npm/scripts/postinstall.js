'use strict';
// npm postinstall bootstrap for `npm install -g agent-takkub`.
//
// SAFETY MODEL — check first, never clobber:
//   • Detects what's already present (preflight.js, non-mutating) and REPORTS it.
//   • Provisions ONLY into the isolated AGENT_TAKKUB_HOME (default
//     ~/.agent-takkub) — never a repo `.venv`, never shared user state.
//   • REUSES an existing cockpit venv (upgrade in place) instead of wiping it.
//   • Does NOT touch the global claude CLI, ~/.claude plugins, or ~/.takkub
//     config. Anything shared/missing is left for an explicit, detect-first
//     `takkub doctor --fix` the user runs consciously.
// Cross-platform (win32 + darwin); idempotent; fails loudly on missing Python
// or a missing bundled wheel.

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const { agentTakkubHome, venvDir, venvPython } = require('./lib');
const preflight = require('./preflight');

function findWheel() {
  // Shipped alongside this package under dist/ (see package.json `files`).
  const dist = path.join(__dirname, '..', '..', 'dist');
  if (!fs.existsSync(dist)) return null;
  const whl = fs.readdirSync(dist).filter((f) => f.endsWith('.whl')).sort();
  return whl.length ? path.join(dist, whl[whl.length - 1]) : null;
}

function run(cmd, args) {
  return spawnSync(cmd, args, { stdio: 'inherit' }).status === 0;
}

// Install the claude CLI only when it's missing — never overwrite an existing
// (possibly version-pinned) global install. Returns true if claude is available
// afterward. Best-effort: a failure just falls back to a "install it yourself"
// hint in the next-steps.
function ensureClaudeCli(present) {
  if (present) return true;
  const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
  console.log('[agent-takkub] claude CLI not found — installing @anthropic-ai/claude-code…');
  return run(npm, ['install', '-g', '@anthropic-ai/claude-code']);
}

// A local (non-global) install leaves a package that cannot work: the `takkub`
// / `agent-takkub` bin shims never land on PATH, and the PATH provisioning
// below targets the npm GLOBAL bin dir. Without this guard that failure is
// silent — install "succeeds", then the command simply doesn't exist. npm sets
// npm_config_global=true for `-g` (npm 7+ also exposes it via npm_config_local
// being unset); treat an explicit "false" as the only definitive local signal
// so an unusual npm/pnpm/yarn env can't produce a false alarm.
function warnIfNotGlobal() {
  if (String(process.env.npm_config_global || '').toLowerCase() === 'true') return;
  const prefix = process.env.npm_config_prefix || '';
  // Heuristic fallback: a local install puts us under a project node_modules.
  const looksLocal =
    String(process.env.npm_config_global || '').toLowerCase() === 'false' ||
    (!prefix && __dirname.includes(`${path.sep}node_modules${path.sep}`) && !process.env.npm_config_global);
  if (!looksLocal) return;
  console.warn(
    '\n[agent-takkub] ⚠ This looks like a LOCAL install.\n' +
      '    agent-takkub must be installed globally or the `takkub` command\n' +
      '    will not be on your PATH:\n\n' +
      '        npm install -g agent-takkub\n'
  );
}

function main() {
  warnIfNotGlobal();
  const env = preflight.detect();
  preflight.report(env);

  if (!env.python.present) {
    console.error(
      '\n[agent-takkub] Python >=3.11 not found. Install it, then re-run ' +
        '`npm install -g agent-takkub`.'
    );
    process.exit(1);
  }
  const wheel = findWheel();
  if (!wheel) {
    console.error('\n[agent-takkub] bundled wheel missing under dist/ — package build is incomplete.');
    process.exit(1);
  }

  fs.mkdirSync(agentTakkubHome(), { recursive: true });

  // Reuse an existing venv (upgrade in place); create only when absent — never wipe.
  if (env.venv.present) {
    console.log('\n[agent-takkub] existing cockpit venv found — upgrading in place (not recreated).');
  } else {
    console.log(`\n[agent-takkub] creating venv at ${venvDir()}`);
    if (!run(env.python.cmd, [...env.python.args, '-m', 'venv', venvDir()])) {
      console.error('[agent-takkub] failed to create venv.');
      process.exit(1);
    }
  }

  const vpy = venvPython();
  run(vpy, ['-m', 'pip', 'install', '--upgrade', 'pip', '--quiet']);
  console.log('[agent-takkub] installing/upgrading cockpit + PyQt6 (may take a few minutes)…');
  // Step 1: install/upgrade the cockpit + its deps (PyQt etc.) normally.
  if (!run(vpy, ['-m', 'pip', 'install', '--upgrade', wheel])) {
    console.error('[agent-takkub] pip install failed.');
    process.exit(1);
  }
  // Step 2: force-refresh the app package itself. pip's --upgrade treats an
  // equal version as "already satisfied" and SKIPS it, so a same-version
  // reinstall (a repair, or a rebuilt wheel whose version didn't bump) would
  // otherwise keep stale code. --no-deps scopes this to the tiny app package —
  // PyQt was already handled in step 1, so this stays fast. Best-effort: step 1
  // already succeeded, so a hiccup here shouldn't fail the whole install.
  run(vpy, ['-m', 'pip', 'install', '--force-reinstall', '--no-deps', wheel]);

  const claudeOk = ensureClaudeCli(env.claudeCli.present);

  // Keep the npm global bin dir on the persistent PATH — otherwise a broken
  // PATH makes claude/takkub/agent-takkub "command not found" in new shells
  // (field incident 2026-07-04). Best-effort; never fails the install.
  let pathAdded = false;
  try {
    pathAdded = require('./pathfix').ensureGlobalBinOnPath();
    if (pathAdded) {
      console.log(
        '[agent-takkub] ✓ npm global bin dir added to your PATH (open a NEW terminal to use takkub/claude).'
      );
    }
  } catch (_e) {
    /* pathfix already printed its own hint */
  }

  console.log(`\n[agent-takkub] ✓ cockpit ready (isolated in ${agentTakkubHome()}).`);
  try {
    const sc = require('./shortcut').create();
    if (sc) console.log(`[agent-takkub] ✓ Desktop launcher created: ${sc}`);
  } catch (_e) {
    /* best-effort — a missing shortcut never fails the install */
  }
  console.log('   Left untouched: ~/.claude plugins, ~/.takkub config (nothing overwritten).');
  console.log('\n   Next steps:');
  if (!claudeOk) {
    console.log('     • install the claude CLI: npm i -g @anthropic-ai/claude-code');
  }
  console.log('     1) claude login          # authenticate (one-time, your account)');
  console.log('     2) takkub provision      # install recommended plugins + browser MCPs (idempotent)');
  console.log('     3) double-click "Takkub Cockpit" on the Desktop  (or run: agent-takkub)\n');
}

try {
  main();
} catch (e) {
  console.error('[agent-takkub] postinstall error:', e && e.message);
  process.exit(1);
}
