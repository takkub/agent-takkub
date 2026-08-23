'use strict';
// node --test npm/scripts/lib.test.js — no framework, Node's built-in runner
// (engines floor is node >=18, which ships node:test).

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { findWheelForVersion, pythonLooksExecutable } = require('./lib');

function tmpDist(files) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-takkub-wheel-'));
  for (const f of files) fs.writeFileSync(path.join(dir, f), '');
  return dir;
}

// Same order-of-magnitude as lib.js's MIN_PYTHON_EXE_BYTES (40KB) without
// importing an unexported constant — big enough to clear the threshold, tiny
// enough to keep the test fast.
const BIG_ENOUGH_BYTES = 50 * 1024;

function tmpFile(size, header) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-takkub-pyexe-'));
  const file = path.join(dir, 'python');
  const buf = Buffer.alloc(size, 0x90);
  if (header) header.copy(buf, 0);
  fs.writeFileSync(file, buf);
  return file;
}

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

test('pythonLooksExecutable: true on non-Windows when big enough, regardless of header', () => {
  const file = tmpFile(BIG_ENOUGH_BYTES, Buffer.from('XX'));
  withPlatform('linux', () => {
    assert.equal(pythonLooksExecutable(file), true);
  });
});
