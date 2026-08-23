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

// A venv python that EXISTS but was overwritten/truncated by something
// outside agent-takkub (#341 — a stray tool once clobbered a live
// ~/.agent-takkub/venv/Scripts/python.exe with a 29-byte text file) must
// never be handed to spawnSync: every command built on top of it then fails
// to even start, with EMPTY stdout/stderr and no diagnostic — the cockpit
// "dies silently". This check is deliberately static (stat + a couple of
// header bytes, no attempt to execute the file) so a broken interpreter is
// never spawned at all — spawning it is exactly the risky step that can
// leave the file locked/hard to overwrite during recovery.
const MIN_PYTHON_EXE_BYTES = 40 * 1024; // a real venv python.exe/python is comfortably >90KB

// Opens the path exactly once and stats/reads that same descriptor for the
// rest of the check — never stat a path and then separately open it, which
// leaves a window for the file at that name to change between the two
// syscalls (CodeQL js/file-system-race, alert #29).
function pythonLooksExecutable(py) {
  let fd;
  try {
    fd = fs.openSync(py, 'r');
  } catch (_e) {
    return false;
  }
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.size < MIN_PYTHON_EXE_BYTES) return false;
    if (process.platform === 'win32') {
      // Windows PE executables always start with the 'MZ' DOS-header magic.
      const buf = Buffer.alloc(2);
      fs.readSync(fd, buf, 0, 2, 0);
      return buf[0] === 0x4d && buf[1] === 0x5a; // 'M', 'Z'
    }
    return true;
  } catch (_e) {
    return false;
  } finally {
    fs.closeSync(fd);
  }
}

function brokenInterpreterMessage(py, home) {
  const venvDirPath = path.join(home, 'venv');
  return (
    `[agent-takkub] the cockpit interpreter looks broken (exists but is not a real executable):\n` +
    `    ${py}\n` +
    '    Something outside agent-takkub overwrote or truncated this file — the cockpit\n' +
    '    did not do this to itself. Fix:\n' +
    '      1) close every running cockpit/takkub window and process for this install\n' +
    '      2) run: npm install -g agent-takkub --force   (reprovisions the venv in place)\n' +
    `      3) if step 2 still can't overwrite it, delete this folder by hand:\n` +
    `             ${venvDirPath}\n` +
    '         then re-run npm install -g agent-takkub\n' +
    '    `takkub doctor` (once the interpreter itself works again) also checks this.'
  );
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
  pythonLooksExecutable,
  brokenInterpreterMessage,
};
