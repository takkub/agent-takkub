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

// Pick the wheel matching an exact version out of a dist/ dir — never the
// lexically-last filename. A string sort puts "1.0.9" after "1.0.10" (#340),
// and dist/ is gitignored so a stale wheel from a previous release can sit
// there unnoticed. `version` is the SemVer string as read from package.json;
// the filename convention (setuptools normalizes the project name's `-` to
// `_`) is `agent_takkub-{version}-py3-none-any.whl`.
function findWheelForVersion(distDir, version) {
  if (!fs.existsSync(distDir)) return null;
  const prefix = `agent_takkub-${version}-`;
  const match = fs.readdirSync(distDir).find((f) => f.endsWith('.whl') && f.startsWith(prefix));
  return match ? path.join(distDir, match) : null;
}

// Windowless python for GUI launchers (Windows: pythonw.exe = no console
// window pops up behind the cockpit). macOS has no separate pythonw.
function venvPythonw() {
  const dir = venvDir();
  return process.platform === 'win32'
    ? path.join(dir, 'Scripts', 'pythonw.exe')
    : path.join(dir, 'bin', 'python');
}

module.exports = {
  agentTakkubHome,
  venvDir,
  venvPython,
  venvPythonIfExists,
  venvPythonw,
  findWheelForVersion,
};
