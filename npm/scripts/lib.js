'use strict';
// Shared helpers for the npm wrapper. Pure path logic — importing this file
// runs NOTHING and touches nothing on disk.

const os = require('os');
const path = require('path');
const fs = require('fs');

// The cockpit's per-install home. Deliberately SEPARATE from a git dev
// checkout's `.venv`, so `npm install -g agent-takkub` never collides with a
// from-source setup. Overridable via AGENT_TAKKUB_HOME so an isolated smoke
// test can point at a throwaway dir and leave ~/.agent-takkub untouched.
function agentTakkubHome() {
  return process.env.AGENT_TAKKUB_HOME || path.join(os.homedir(), '.agent-takkub');
}

function venvDir() {
  return path.join(agentTakkubHome(), 'venv');
}

// The venv's python, per-platform (Windows: Scripts\python.exe, else bin/python).
function venvPython() {
  const dir = venvDir();
  return process.platform === 'win32'
    ? path.join(dir, 'Scripts', 'python.exe')
    : path.join(dir, 'bin', 'python');
}

function venvPythonIfExists() {
  const p = venvPython();
  return fs.existsSync(p) ? p : null;
}

module.exports = { agentTakkubHome, venvDir, venvPython, venvPythonIfExists };
