#!/usr/bin/env node
'use strict';
// The `takkub` CLI, run from the provisioned (isolated) venv.
const { spawnSync } = require('child_process');
const {
  venvPythonIfExists,
  pythonExecutableProblem,
  brokenInterpreterMessage,
  agentTakkubHome,
} = require('../scripts/lib');

const py = venvPythonIfExists();
if (!py) {
  console.error('takkub is not provisioned. Run: npm install -g agent-takkub');
  process.exit(1);
}
const problem = pythonExecutableProblem(py);
if (problem) {
  console.error(brokenInterpreterMessage(py, agentTakkubHome(), problem));
  process.exit(1);
}
const r = spawnSync(py, ['-m', 'agent_takkub.cli', ...process.argv.slice(2)], { stdio: 'inherit' });
if (r.error) {
  // spawnSync itself failed (e.g. the interpreter passed validation above
  // but the OS still refused to launch it) — never let this read as a
  // quiet success (#341).
  console.error(`[takkub] failed to launch the cockpit interpreter: ${r.error.message}`);
  process.exit(1);
}
process.exit(r.status == null ? 1 : r.status);
