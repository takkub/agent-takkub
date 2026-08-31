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
//
// The floor only needs to catch truncation (#341's 29-byte case), not judge
// what a "normal-sized" interpreter looks like — a pyenv/python.org
// framework build's `bin/pythonX.Y` is a tiny Mach-O stub that re-execs into
// Python.framework and can be well under 40KB while still being completely
// runnable (#446). Magic-byte sniffing is what actually distinguishes it
// from garbage; size only rules out an empty/near-empty file.
const MIN_PYTHON_EXE_BYTES = 1024;

const ELF_MAGIC = 0x7f454c46; // '\x7fELF', big-endian read of the 4 header bytes
const MACHO_MAGICS = new Set([
  0xfeedface, // MH_MAGIC (32-bit)
  0xcefaedfe, // MH_CIGAM (32-bit, byte-swapped)
  0xfeedfacf, // MH_MAGIC_64
  0xcffaedfe, // MH_CIGAM_64 (byte-swapped)
  0xcafebabe, // FAT_MAGIC (universal/fat binary)
  0xbebafeca, // FAT_CIGAM (universal/fat binary, byte-swapped)
]);

// Opens the path exactly once and stats/reads that same descriptor for the
// rest of the check — never stat a path and then separately open it, which
// leaves a window for the file at that name to change between the two
// syscalls (CodeQL js/file-system-race, alert #29).
//
// Returns null when `py` looks like a real, runnable interpreter, otherwise
// a short machine-readable reason so callers can explain what specifically
// failed instead of a single opaque "not a real executable".
function pythonExecutableProblem(py) {
  let fd;
  try {
    fd = fs.openSync(py, 'r');
  } catch (_e) {
    return 'missing';
  }
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile()) return 'not-a-file';
    if (stat.size < MIN_PYTHON_EXE_BYTES) return 'too-small';
    const buf = Buffer.alloc(4);
    const n = fs.readSync(fd, buf, 0, 4, 0);
    if (process.platform === 'win32') {
      // Windows PE executables always start with the 'MZ' DOS-header magic.
      return n >= 2 && buf[0] === 0x4d && buf[1] === 0x5a ? null : 'bad-magic';
    }
    // POSIX: accept a real ELF or Mach-O binary, or a shebang script — a
    // pyenv shim / venv `python` wrapper is a text file starting with `#!`,
    // not a compiled executable, and is just as valid an interpreter path.
    if (n >= 2 && buf[0] === 0x23 && buf[1] === 0x21) return null; // '#!'
    if (n < 4) return 'bad-magic';
    const magic = buf.readUInt32BE(0);
    if (magic === ELF_MAGIC || MACHO_MAGICS.has(magic)) return null;
    return 'bad-magic';
  } catch (_e) {
    return 'read-error';
  } finally {
    fs.closeSync(fd);
  }
}

function pythonLooksExecutable(py) {
  return pythonExecutableProblem(py) === null;
}

const PROBLEM_DESCRIPTIONS = {
  missing: "the file doesn't exist",
  'not-a-file': 'the path exists but is not a regular file (e.g. a directory)',
  'too-small': `the file is smaller than ${MIN_PYTHON_EXE_BYTES} bytes — too small to be a real interpreter`,
  'bad-magic': "the file's header does not match a known executable format (PE/ELF/Mach-O) or a shebang script",
  'read-error': 'the file could not be read to check its header',
};

function brokenInterpreterMessage(py, home, problem) {
  const venvDirPath = path.join(home, 'venv');
  const why = PROBLEM_DESCRIPTIONS[problem] || PROBLEM_DESCRIPTIONS['bad-magic'];
  return (
    `[agent-takkub] the cockpit interpreter looks broken (exists but is not a real executable):\n` +
    `    ${py}\n` +
    `    Reason: ${why}.\n` +
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
  pythonExecutableProblem,
  brokenInterpreterMessage,
};
