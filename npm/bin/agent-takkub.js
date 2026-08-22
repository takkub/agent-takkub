#!/usr/bin/env node
'use strict';
// Launches the cockpit GUI from the provisioned (isolated) venv.
const { spawnSync } = require('child_process');
const {
  venvPythonIfExists,
  pythonLooksExecutable,
  brokenInterpreterMessage,
  agentTakkubHome,
} = require('../scripts/lib');

const py = venvPythonIfExists();
if (!py) {
  console.error('agent-takkub is not provisioned. Run: npm install -g agent-takkub');
  process.exit(1);
}
if (!pythonLooksExecutable(py)) {
  console.error(brokenInterpreterMessage(py, agentTakkubHome()));
  process.exit(1);
}
const r = spawnSync(py, ['-m', 'agent_takkub', ...process.argv.slice(2)], { stdio: 'inherit' });
if (r.error) {
  console.error(`[agent-takkub] failed to launch the cockpit interpreter: ${r.error.message}`);
  process.exit(1);
}
process.exit(r.status == null ? 1 : r.status);
