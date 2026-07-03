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

function main() {
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
  if (!run(vpy, ['-m', 'pip', 'install', '--upgrade', wheel])) {
    console.error('[agent-takkub] pip install failed.');
    process.exit(1);
  }

  console.log(`\n[agent-takkub] ✓ cockpit ready (isolated in ${agentTakkubHome()}).`);
  console.log('   Left untouched: global claude CLI, ~/.claude plugins, ~/.takkub config.');
  console.log('\n   Next steps:');
  if (!env.claudeCli.present) {
    console.log('     • claude CLI not found → install: npm i -g @anthropic-ai/claude-code');
  }
  console.log('     1) claude login          # authenticate (one-time, your account)');
  console.log('     2) agent-takkub          # launch the cockpit');
  console.log('     3) takkub doctor --fix   # (optional) top up missing plugins/MCPs — detect-first, never overwrites\n');
}

try {
  main();
} catch (e) {
  console.error('[agent-takkub] postinstall error:', e && e.message);
  process.exit(1);
}
