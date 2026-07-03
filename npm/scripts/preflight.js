'use strict';
// Non-mutating environment detection. Runs ONLY `--version`-style probes and
// filesystem existence checks — nothing here installs, writes, or overwrites.
// This is what lets the installer "check first": report what's already present
// and provision ONLY the gaps, so a machine that already has Python / claude /
// a cockpit venv is never clobbered.

const { spawnSync } = require('child_process');
const fs = require('fs');
const { venvPython, agentTakkubHome } = require('./lib');

function probe(cmd, args) {
  const r = spawnSync(cmd, args, { encoding: 'utf8' });
  if (r.status !== 0) return null;
  return ((r.stdout || '') + (r.stderr || '')).trim();
}

function detectPython() {
  const cands = process.platform === 'win32'
    ? [['py', ['-3.11', '--version']], ['py', ['-3', '--version']], ['python', ['--version']]]
    : [['python3.11', ['--version']], ['python3', ['--version']], ['python', ['--version']]];
  for (const [cmd, args] of cands) {
    const out = probe(cmd, args);
    const m = out && out.match(/Python (\d+)\.(\d+)\.\d+/);
    if (m) {
      const maj = parseInt(m[1], 10);
      const min = parseInt(m[2], 10);
      if (maj > 3 || (maj === 3 && min >= 11)) {
        // Drop the trailing '--version' so callers can reuse (cmd, args) to run python.
        return { present: true, version: `${maj}.${min}`, cmd, args: args.slice(0, -1) };
      }
    }
  }
  return { present: false };
}

// Detect the whole environment without mutating anything.
function detect() {
  const claude = probe('claude', ['--version']);
  return {
    python: detectPython(),
    node: probe('node', ['--version']),
    claudeCli: claude ? { present: true, version: claude } : { present: false },
    venv: { present: fs.existsSync(venvPython()), path: venvPython() },
    home: agentTakkubHome(),
  };
}

function report(d) {
  console.log('[agent-takkub] environment check (nothing below is overwritten):');
  console.log(`    Python >=3.11 : ${d.python.present ? '✓ ' + d.python.version + ' (reuse existing)' : '· missing'}`);
  console.log(`    Node          : ${d.node ? '✓ ' + d.node : '· missing'}`);
  console.log(`    claude CLI    : ${d.claudeCli.present ? '✓ ' + d.claudeCli.version + ' (kept as-is)' : '· not found'}`);
  console.log(`    cockpit venv  : ${d.venv.present ? '✓ exists (reuse/upgrade)' : '· will create'} — ${d.home}`);
}

module.exports = { detect, report };
