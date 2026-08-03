#!/usr/bin/env node
'use strict';
// The `takkub` CLI, run from the provisioned (isolated) venv.
const { spawnSync } = require('child_process');
const { venvPythonIfExists } = require('../scripts/lib');

const py = venvPythonIfExists();
if (!py) {
  console.error('takkub is not provisioned. Run: npm install -g agent-takkub');
  process.exit(1);
}
const r = spawnSync(py, ['-m', 'agent_takkub.cli', ...process.argv.slice(2)], { stdio: 'inherit' });
process.exit(r.status == null ? 1 : r.status);
