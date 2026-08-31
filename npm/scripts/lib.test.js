'use strict';
// node --test npm/scripts/lib.test.js — no framework, Node's built-in runner
// (engines floor is node >=18, which ships node:test).

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { findWheelForVersion, pythonLooksExecutable, pythonExecutableProblem } = require('./lib');

function tmpDist(files) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-takkub-wheel-'));
  for (const f of files) fs.writeFileSync(path.join(dir, f), '');
  return dir;
}

// Same order-of-magnitude as lib.js's MIN_PYTHON_EXE_BYTES (1KB) without
// importing an unexported constant — big enough to clear the threshold, tiny
// enough to keep the test fast.
const BIG_ENOUGH_BYTES = 2 * 1024;

function tmpFile(size, header) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-takkub-pyexe-'));
  const file = path.join(dir, 'python');
  const buf = Buffer.alloc(size, 0x90);
  if (header) header.copy(buf, 0);
  fs.writeFileSync(file, buf);
  return file;
}

// Real Mach-O headers pyenv/python.org framework builds actually ship —
// covers both endianness and the fat/universal wrapper (#446).
const MACHO_MAGIC = Buffer.from([0xfe, 0xed, 0xfa, 0xce]); // MH_MAGIC (32-bit)
const MACHO_CIGAM = Buffer.from([0xce, 0xfa, 0xed, 0xfe]); // MH_CIGAM (32-bit swapped)
const MACHO_MAGIC_64 = Buffer.from([0xfe, 0xed, 0xfa, 0xcf]); // MH_MAGIC_64
const MACHO_CIGAM_64 = Buffer.from([0xcf, 0xfa, 0xed, 0xfe]); // MH_CIGAM_64
const FAT_MAGIC = Buffer.from([0xca, 0xfe, 0xba, 0xbe]); // universal binary
const FAT_CIGAM = Buffer.from([0xbe, 0xba, 0xfe, 0xca]); // universal binary, swapped
const ELF_MAGIC = Buffer.from([0x7f, 0x45, 0x4c, 0x46]); // \x7fELF
const SHEBANG = Buffer.from('#!/usr/bin/env python3\n');

function withPlatform(value, fn) {
  const orig = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value, configurable: true });
  try {
    fn();
  } finally {
    Object.defineProperty(process, 'platform', orig);
  }
}

test('picks the wheel matching the exact version', () => {
  const dir = tmpDist(['agent_takkub-1.0.82-py3-none-any.whl', 'agent_takkub-1.0.84-py3-none-any.whl']);
  const found = findWheelForVersion(dir, '1.0.84');
  assert.equal(path.basename(found), 'agent_takkub-1.0.84-py3-none-any.whl');
});

test('does not let string sort pick 1.0.9 over 1.0.10', () => {
  const dir = tmpDist(['agent_takkub-1.0.10-py3-none-any.whl', 'agent_takkub-1.0.9-py3-none-any.whl']);
  const found = findWheelForVersion(dir, '1.0.9');
  assert.equal(path.basename(found), 'agent_takkub-1.0.9-py3-none-any.whl');
});

test('returns null when no wheel matches the version', () => {
  const dir = tmpDist(['agent_takkub-1.0.82-py3-none-any.whl']);
  assert.equal(findWheelForVersion(dir, '1.0.84'), null);
});

test('returns null when dist dir does not exist', () => {
  assert.equal(findWheelForVersion(path.join(os.tmpdir(), 'no-such-dir-xyz'), '1.0.84'), null);
});

test('pythonLooksExecutable: false when the file does not exist', () => {
  withPlatform('linux', () => {
    assert.equal(pythonLooksExecutable(path.join(os.tmpdir(), 'no-such-python-xyz')), false);
  });
});

test('pythonLooksExecutable: false when the file is smaller than the minimum size', () => {
  const file = tmpFile(10);
  withPlatform('linux', () => {
    assert.equal(pythonLooksExecutable(file), false);
  });
  withPlatform('win32', () => {
    assert.equal(pythonLooksExecutable(file), false);
  });
});

test('pythonLooksExecutable: false when the path is a directory', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-takkub-pyexe-dir-'));
  withPlatform('linux', () => {
    assert.equal(pythonLooksExecutable(dir), false);
  });
  withPlatform('win32', () => {
    assert.equal(pythonLooksExecutable(dir), false);
  });
});

test('pythonLooksExecutable: false on win32 when big enough but header is not MZ', () => {
  const file = tmpFile(BIG_ENOUGH_BYTES, Buffer.from('XX'));
  withPlatform('win32', () => {
    assert.equal(pythonLooksExecutable(file), false);
  });
});

test('pythonLooksExecutable: true on win32 when big enough with an MZ header', () => {
  const file = tmpFile(BIG_ENOUGH_BYTES, Buffer.from('MZ'));
  withPlatform('win32', () => {
    assert.equal(pythonLooksExecutable(file), true);
  });
});

test('pythonLooksExecutable: false on non-Windows when big enough but header matches no known executable magic', () => {
  const file = tmpFile(100 * 1024, Buffer.from('XX'));
  withPlatform('linux', () => {
    assert.equal(pythonLooksExecutable(file), false);
  });
  withPlatform('darwin', () => {
    assert.equal(pythonLooksExecutable(file), false);
  });
});

test('pythonLooksExecutable: true on POSIX with an ELF header', () => {
  const file = tmpFile(BIG_ENOUGH_BYTES, ELF_MAGIC);
  withPlatform('linux', () => {
    assert.equal(pythonLooksExecutable(file), true);
  });
});

test('pythonLooksExecutable: true on darwin with a thin Mach-O header (both endianness, 32/64-bit)', () => {
  withPlatform('darwin', () => {
    for (const header of [MACHO_MAGIC, MACHO_CIGAM, MACHO_MAGIC_64, MACHO_CIGAM_64]) {
      const file = tmpFile(BIG_ENOUGH_BYTES, header);
      assert.equal(pythonLooksExecutable(file), true, `header ${header.toString('hex')} should pass`);
    }
  });
});

test('pythonLooksExecutable: true on darwin with a fat/universal Mach-O header (both endianness)', () => {
  withPlatform('darwin', () => {
    for (const header of [FAT_MAGIC, FAT_CIGAM]) {
      const file = tmpFile(BIG_ENOUGH_BYTES, header);
      assert.equal(pythonLooksExecutable(file), true, `header ${header.toString('hex')} should pass`);
    }
  });
});

test('pythonLooksExecutable: true on POSIX with a shebang script (pyenv shim / venv wrapper)', () => {
  const file = tmpFile(BIG_ENOUGH_BYTES, SHEBANG);
  withPlatform('linux', () => {
    assert.equal(pythonLooksExecutable(file), true);
  });
  withPlatform('darwin', () => {
    assert.equal(pythonLooksExecutable(file), true);
  });
});

test('pythonLooksExecutable: false on win32 for a small pyenv-style framework stub (no MZ header)', () => {
  // Regression for #446: a real, runnable macOS Mach-O interpreter must not
  // be rejected just because it is smaller than an old, arbitrary floor.
  const file = tmpFile(BIG_ENOUGH_BYTES, MACHO_MAGIC_64);
  withPlatform('win32', () => {
    assert.equal(pythonLooksExecutable(file), false);
  });
});

test('pythonLooksExecutable: true via a symlink to a valid interpreter', () => {
  const target = tmpFile(BIG_ENOUGH_BYTES, ELF_MAGIC);
  const link = path.join(path.dirname(target), 'python-symlink');
  try {
    fs.symlinkSync(target, link);
  } catch (e) {
    // Creating a symlink can require elevated privileges on Windows —
    // skip rather than fail the suite when that's the case.
    if (e.code === 'EPERM') return;
    throw e;
  }
  withPlatform('linux', () => {
    assert.equal(pythonLooksExecutable(link), true);
  });
});

test('pythonExecutableProblem: distinguishes "too small" from "bad magic" for the error message', () => {
  const tooSmall = tmpFile(10, ELF_MAGIC);
  const badMagic = tmpFile(BIG_ENOUGH_BYTES, Buffer.from('XX'));
  withPlatform('linux', () => {
    assert.equal(pythonExecutableProblem(tooSmall), 'too-small');
    assert.equal(pythonExecutableProblem(badMagic), 'bad-magic');
  });
});

test('pythonExecutableProblem: null when the interpreter looks valid', () => {
  const file = tmpFile(BIG_ENOUGH_BYTES, ELF_MAGIC);
  withPlatform('linux', () => {
    assert.equal(pythonExecutableProblem(file), null);
  });
});
